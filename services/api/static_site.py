"""🔴 D136：让 FastAPI 托管前端**构建产物**（否则外网只有 API，没有界面）。

## 为什么需要它

对外只暴露了 8010（API），而仓库里**没有任何东西托管
`web/dist`**：`main.py` 没有 `StaticFiles`，也没有反代配置。
隧道配置里 `www` 指向 `127.0.0.1:5173` —— 那是 **vite 开发服务器**。

开发服务器挂公网不合适（HMR 客户端、明文源码与 source map、无缓存策略）。
所以正确做法是：**构建一次，由 FastAPI 托管**，隧道指 8010，
一条 Access 策略同时覆盖 API 与界面。

## 三个容易做错的地方

1. 🔑 **挂载必须在所有路由之后**。FastAPI 按声明顺序匹配，
   挂在前面会把 `/api/v1/*` 全吃掉。
2. 🔴 **SPA 回退不能吞掉真正的 404**。深链（`/kb/123`）要回 `index.html`，
   但 `/api/...`、`/mcp`、`/a2a`、`/health` 这些**打错就应该是 404**。
   判据用**前缀白名单**而不是"没有匹配到就回首页" ——
   后者会把"调错了端点"变成"给你一个 HTML"，客户端完全无法区分。
3. ⚠️ **`dist` 不存在时不能挂**。挂一个不存在的目录会让**每个** GET 都 500
   （连 `/api/v1/health` 都活不了，因为异常发生在中间件之后）。
   正确做法：不挂 + 记一条日志说清"要先 `npm run build`"。

## 缓存策略

- `index.html`：`no-cache` —— 它引用带哈希的资源名，新构建必须立刻生效
- `/assets/*`（文件名带内容哈希）：`max-age=31536000, immutable` —— 可以永久缓存
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)

#: 这些前缀**不**做 SPA 回退（打错就该是 404，而不是一段 HTML）
NON_SPA_PREFIXES = (
    "/api/", "/health", "/mcp", "/a2a",
    "/docs", "/redoc", "/openapi.json",
)

#: 允许长期缓存的目录（文件名带内容哈希）
IMMUTABLE_PREFIX = "/assets/"


def dist_dir() -> Path:
    """`web/dist` 的位置（仓库内相对路径，容器里换个 WORKDIR 也对得上）。"""
    return Path(__file__).resolve().parents[2] / "web" / "dist"


class _ImmutableAssets(StaticFiles):
    """`/assets/*` 的文件名带内容哈希 ⇒ 可以永久缓存。"""

    async def get_response(self, path: str, scope):  # noqa: ANN001, ANN202
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def mount_frontend(app: FastAPI, directory: Path | None = None) -> bool:
    """把前端构建产物挂到 `/`。返回是否真的挂了。"""
    target = directory if directory is not None else dist_dir()
    index = target / "index.html"
    if not index.exists():
        logger.warning(
            "没找到前端构建产物（%s）—— 界面不会被托管，"
            "只暴露 API。要托管界面请先 `cd web && npm run build`。",
            index,
        )
        return False

    assets = target / "assets"
    if assets.is_dir():
        app.mount(IMMUTABLE_PREFIX, _ImmutableAssets(directory=assets),
                  name="web-assets")

    # ⚠️ 显式带上 HEAD：FastAPI 的 `@app.get` **不会**自动补 HEAD，
    #   于是 `curl -I https://…/kb/123` 回 405 —— 排查时第一眼像是"路由没挂"。
    #
    # 🔴 D147：必须覆盖**所有**方法。只挂 GET/HEAD 时，"路径打错 + 写方法"
    #   会被 Starlette 在**进到这里之前**就判成 **405** ——
    #   而 405 的字面意思是"路径存在、方法不对"，于是排查的人会去改
    #   HTTP 方法，而真正要改的是**路径**。
    #   实测（都打错前缀，真身都在根路径）：`POST /api/v1/a2a/tasks` → 405、
    #   `POST /api/v1/mcp` → 405、`POST /health` → 405。
    #   这对 MCP/A2A 接入方尤其误导：契约里写的是 `/mcp`，写成
    #   `/api/v1/mcp` 得到 405 而不是 404，看起来像"方法不对"，
    #   实际上只是**多写了一段前缀**。
    #   ⇒ 全部方法都进这个 handler，非 GET/HEAD 一律回 JSON 404。
    #   （路由注册在所有 `include_router` **之后**，所以不会遮蔽真路由；
    #   CORS 预检由外层中间件拦下，也不受影响。）
    @app.api_route("/{full_path:path}",
                   methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
                   include_in_schema=False)
    async def spa(full_path: str, request: Request):  # noqa: ANN202
        """SPA 回退：深链回 `index.html`，其余前缀保持 404。"""
        # ⚠️ 路径参数**不带前导斜杠**（`"api/v1/nope"`），而白名单是按
        #   "/api/" 这种带斜杠的形式写的 —— 不补上就等于白名单失效，
        #   症状是「打错的 API 端点返回 200 + 一段 HTML」。
        path = f"/{full_path.lstrip('/')}"
        if (
            request.method not in ("GET", "HEAD")
            or path.startswith(NON_SPA_PREFIXES)
            or path in {
                "/health", "/mcp", "/a2a", "/docs", "/redoc", "/openapi.json",
            }
        ):
            return JSONResponse({"error": {"code": "NOT_FOUND",
                                           "message": f"没有这个端点: {path}"}},
                                status_code=404)
        # 真实文件优先（favicon.ico、robots.txt …）
        candidate = (target / full_path).resolve()
        if (
            full_path
            and candidate.is_file()
            and str(candidate).startswith(str(target.resolve()))
        ):
            return FileResponse(candidate)
        return FileResponse(
            index,
            headers={"Cache-Control": "no-cache"},
            media_type="text/html",
        )

    logger.info("前端已托管：%s（深链回 index.html，/api 与 /mcp 仍按路由）", target)
    return True