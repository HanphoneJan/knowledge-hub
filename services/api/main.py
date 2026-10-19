"""FastAPI 应用入口（进程 1）。

D50：API 与 Worker 分两个独立进程。API 常驻轻量（bge-small 约 100MB），
Worker 独立吃 CPU（colSmol 编码 15s/图，会吃满 12 核）。

启动方式：
    uv run uvicorn services.api.main:app --host 127.0.0.1 --port 8002
"""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..common.config import get_settings
from ..common.db import check_db, dispose_engines
from ..common.errors import PkhError
from ..common.qdrant import check_qdrant, close_client

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动/关闭钩子。"""
    # 模型懒加载（D29）：这里只做核心预热，不加载 colSmol（2.5GB）
    from ..retrieval.pool import preload_core

    try:
        preload_core()
    except Exception as exc:  # noqa: BLE001 — 预热失败不应阻止服务启动
        app.state.model_warmup_error = str(exc)

    yield

    close_client()
    dispose_engines()


def docs_routes(expose: bool) -> dict[str, str | None]:
    """`/docs`、`/redoc`、`/openapi.json` 的开关（D138）。

    🔑 抽成函数是为了能**单测**"关掉就真的没有路由"——
    直接在 `FastAPI(...)` 里写三个 `if` 的话，唯一能验的方式是起一个进程、
    改环境、重启、再打一次 HTTP（`eval` 做不到，pytest 也不该这么干）。
    ⚠️ 三个都要置 `None`：只关 `docs_url` 的话 `/openapi.json` 还在，
    而那才是**信息泄露的主体**（完整 API 面 + MCP 工具名）。
    ⚠️ 关掉**不影响** `eval/contract_check.py`：它读 `app.openapi()` 这个
    **方法**，与 `openapi_url` 这个**路由**在 FastAPI 里是独立的。
    """
    if expose:
        return {"docs_url": "/docs", "redoc_url": "/redoc",
                "openapi_url": "/openapi.json"}
    return {"docs_url": None, "redoc_url": None, "openapi_url": None}


app = FastAPI(
    title="Knowledge Hub",
    version="0.1.0",
    description="多租户知识库 · RAG · MCP/A2A 对外供给",
    lifespan=lifespan,
    **docs_routes(get_settings().expose_docs),
)

# CORS：开发期前端在 5173，生产同源部署后可以收窄
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["x-request-id"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):  # noqa: ANN001, ANN202
    """给每个请求打 trace id，回写到响应头和错误体。"""
    rid = request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex[:16]}"
    request.state.request_id = rid
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["x-request-id"] = rid
    response.headers["x-elapsed-ms"] = str(
        round((time.perf_counter() - started) * 1000)
    )
    return response


@app.exception_handler(PkhError)
async def pkh_error_handler(request: Request, exc: PkhError) -> JSONResponse:
    """把业务异常转成契约格式（`docs/DESIGN-API.md` 0.2）。"""
    rid = getattr(request.state, "request_id", None)
    return JSONResponse(
        status_code=exc.status_code, content=exc.to_dict(rid),
        headers=getattr(exc, "headers", None) or None,
    )


# ============================================================
# 路由注册
# ============================================================

from .routers import (
    a2a,
    admin,
    ask,
    audit,
    auth,
    documents,
    health,
    invites,
    pii,
    imports,
    jobs,
    kb,
    llm,
    mcp,
    search,
    sessions,
    timeline,
    users,
    workspaces,
)  # noqa: E402

app.include_router(health.public_router)
app.include_router(health.router, prefix="/api/v1", tags=["health"])
app.include_router(auth.router, prefix="/api/v1")
app.include_router(workspaces.router, prefix="/api/v1")
app.include_router(kb.router, prefix="/api/v1")
app.include_router(documents.router, prefix="/api/v1")
app.include_router(jobs.router, prefix="/api/v1")
app.include_router(audit.router, prefix="/api/v1")
app.include_router(admin.router, prefix="/api/v1")
app.include_router(llm.router, prefix="/api/v1")
app.include_router(users.router, prefix="/api/v1")
app.include_router(imports.router, prefix="/api/v1")
app.include_router(timeline.router, prefix="/api/v1")
# MCP 与 A2A 按契约挂在 /api/v1 之外（MCP 的规范端点就是 /mcp，
# 客户端按固定路径找；A2A 的 agent-card.json 更是规范里写死的发现路径）
app.include_router(mcp.router, prefix="")
app.include_router(a2a.router, prefix="")
app.include_router(invites.router, prefix="/api/v1")
# PII 报告挂在 /api/v1 下（与 timeline 同一个 /kb 前缀的独立 router）
app.include_router(pii.router, prefix="/api/v1")
app.include_router(search.router, prefix="/api/v1")
app.include_router(ask.router, prefix="/api/v1")
app.include_router(sessions.router, prefix="/api/v1")

# 🔴 D136：前端构建产物（**必须**在所有路由之后挂 —— FastAPI 按声明顺序匹配，
#   挂前面会把 `/api/v1/*` 全吃掉）。深链由 `static_site.spa` 回退到
#   `index.html`；`/api` `/mcp` `/a2a` `/health` 保持真 404。
from .static_site import mount_frontend as _mount_frontend  # noqa: E402

_FRONTEND_MOUNTED = _mount_frontend(app)


@app.get("/", summary="服务信息（有前端构建产物时是界面本身）")
async def root():
    """🔑 有前端构建产物时，`/` 返回**界面**；没有时返回服务信息。

    ⚠️ 为什么不"两个都留在 `/`"：同一个路径只能有一个响应。
    界面对外才是主入口（D136），而服务信息在**没有构建产物**时仍然有用
    （探活脚本能看出"这是 API 而不是界面"）。
    服务信息的字段保持不变（`service`/`version`/`docs`/`api`），
    没有构建产物时行为与 D136 之前**完全一致**。
    """
    if _FRONTEND_MOUNTED:
        from .static_site import dist_dir

        from fastapi.responses import FileResponse

        return FileResponse(
            dist_dir() / "index.html",
            headers={"Cache-Control": "no-cache"},
            media_type="text/html",
        )
    return {
        "service": "knowledge-hub",
        "version": "0.1.0",
        "docs": "/docs",
        "api": "/api/v1",
    }
