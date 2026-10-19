"""视觉查询服务（进程 3，D158）—— **只做编码**。

## 它是什么，不是什么

| 是 | 不是 |
|---|---|
| 常驻 colSmol（+2.65GB），把 `POST /encode` 的文本 / `POST /encode_image` 的图片编码成 multi-vectors | ❌ **不查 Qdrant** |
| 提供 `GET /health` 供 API 决定 `available_legs()` 里有没有 `visual` | ❌ **不做租户过滤** |
| worker 的**写**路径在 `visual_write_via_service=true` 时调它（D178；默认仍本地编码） | ❌ 不做 rerank、不做融合 |

🔑 **后两条不是省事，是架构性的**：Qdrant 查询与 `build_qdrant_filter`
都已经在 API 进程里做完了（租户过滤的唯一入口之一，CONVENTIONS §0.2）。
复制一份到新进程 ⇒ **两份过滤逻辑迟早漂移，而漂移的方向是"越权看到别人的照片"**
（那是本项目唯一一次真实越权的形状，§0.6）。

## 为什么必须是独立进程

`CONVENTIONS.md` §4.1 禁止 API 进程加载 colSmol，实测依据在 §4.1b/c：
常驻 **+2.65GB**、加载 **13s**、**6 路并发吃 8.7 个核**、单条编码 p95 **905ms**。
放进 API 进程 ⇒ 用户提问期间 8.7 个核被编码占着，剩下的核要同时扛
FastAPI + PG 池 + 三条文本腿。**2C4G 的机器上 2.65GB 常驻直接 OOM。**

⇒ D158 的解法就是"再加一个进程"，而 §4.1 **一个字都不用改**。

## 启动

```bash
uv run uvicorn services.visual_service.main:app --host 127.0.0.1 --port 8020
```

`deploy/dev.sh` 会连同 API / worker 一起起它。

## 协议（`DESIGN-HISTORY.md §1` §4）

```
POST /encode  {"text": "红色的那张照片"}
  → 200 {"vectors": [[0.012, ...], ...], "n_tokens": 17, "ms": 118}
  → 400 {"code": "BAD_REQUEST", "detail": "text 不能为空"}
  → 503 {"code": "VISUAL_UNAVAILABLE", "detail": "模型未加载完：…"}
POST /encode_image  <原始 PNG/JPEG 字节>（Content-Type: application/octet-stream）
  → 200 {"vectors": [[...], ...], "n_tokens": 13, "ms": 15000}   # 写侧，D178
GET  /health  {"status": "ok", "model_loaded": true, "pid": …, "rss_gb": …,
               "encode_p95_ms": …, "encode_calls": …}
```

⚠️ **文本查询编码不要传 `images`** —— 那是 colSmol README 的写法，
transformers 5.18 会抛 `AttributeError: 'list' object has no attribute 'device'`
（D124 实测）。**图片**编码走专门的 `POST /encode_image`（收原始字节），
它在服务内部用 `encode_images`（`encode_document`，自己补 `<image>` 占位）。
"""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from typing import Any

import anyio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..common.config import get_settings
from ..common.qdrant import VDR_DIM
from ..retrieval.pool import (
    enable_visual,
    encode_images,
    encode_vdr_query,
    get_visual_model,
    unload_all,
)

logger = logging.getLogger(__name__)

settings = get_settings()

#: 编码并发上限。⚠️ colSmol 在 CPU 上并行度 8.7x（CONVENTIONS §4.1c 实测），
#: 更高并发不会更快，只会让每条更慢 ⇒ 超限就在这里排队，
#: 排队等不到就 503（调用方会降级成 `skipped`，绝不 500）。
_MAX_ENCODE_CONCURRENCY = 4

#: 🔑 D178：**图片**（写侧）编码的并发上限。与查询闸 `_sem` **分开**：
#:   一页图前向实测 ~15s，共用闸会把查询（预算只有 2s）堵在它后面。
#:   取 1 ⇒ 图片串行，查询随时可插进来。
_MAX_IMAGE_CONCURRENCY = 1
_img_sem: Any = None

#: `/health` 里 `encode_p95_ms` 的样本窗口。⚠️ 至少 20 个样本才算 p95 ——
#: 少于 20 时直接报 `null`，别拿 3 个样本编一个"p95"出来。
_P95_WINDOW = 64
_P95_MIN_SAMPLES = 20

_sem: Any = None  # anyio.Semaphore，进程内单例
_durations: deque[float] = deque(maxlen=_P95_WINDOW)
#: 🔑 只有 `model_loaded` 一个真值来源 —— "服务活着"与"能用"在同一个
#: 字段上分开判（`status` 恒 ok、模型状态在这里）。曾想用两个布尔
#: （loaded + ready）表示"加载完了"与"预热跑过了"，但它们**永远同时翻转**
#: ⇒ 两个字段只多一处能不一致的地方。
_state: dict[str, Any] = {"model_loaded": False, "load_error": None}


class EncodeReq(BaseModel):
    """`POST /encode` 的请求体。

    ⚠️ **刻意没有 `images` 字段** —— 见模块 docstring。
    少一个字段比"收下然后报错"好：那会让调用方以为可以走图片编码。
    """

    text: str = Field(min_length=1, max_length=2000)


def percentile(values: list[float], q: float) -> float | None:
    """百分位数（最近邻法）。样本不够就给 `None`，不给一个假的数字。"""
    if len(values) < _P95_MIN_SAMPLES:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[idx]


def _rss_gb() -> float:
    """本进程 RSS（GiB）。⚠️ 读不到就给 0.0 —— 诊断字段不许变成故障。"""
    try:
        with open(f"/proc/{os.getpid()}/statm", encoding="utf-8") as fh:
            pages = int(fh.read().split()[1])
        return round(pages * os.sysconf("SC_PAGE_SIZE") / (1024**3), 3)
    except Exception:  # noqa: BLE001
        return 0.0


def _error(code: str, detail: str, status: int) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "detail": detail})


def model_state() -> dict[str, Any]:
    """模型加载状态的只读视图（`/health` 与单元测试都用它，不各自抄一份）。"""
    return dict(_state)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动：加载 colSmol 并**预热一次真实编码**。

    🔑 预热不是优化，是**正确性**：模型是懒加载的（实测 12.8~15.0s），
    不预热的话第一个用户的查询会等那 13s —— 而客户端超时只有 2s，
    于是那一次请求必然降级，且用户看到的是"视觉腿时好时坏"。
    ⇒ 未加载完时 `/encode` 明确 503，并说清"模型未加载完"，
    而不是让它撞上客户端超时（错误信息会指不到真正的原因）。

    ⚠️ 加载失败**不阻止服务启动**：起来了的 503 比"连不上"可诊断得多
    （`/health` 能看到 `load_error` 的原文）。
    """
    global _sem
    _sem = anyio.Semaphore(_MAX_ENCODE_CONCURRENCY)

    global _img_sem
    _img_sem = anyio.Semaphore(_MAX_IMAGE_CONCURRENCY)

    enable_visual(True)
    t0 = time.perf_counter()
    try:
        get_visual_model()          # 触发懒加载
        encode_vdr_query("预热")     # 跑一次真实前向（把图编译进内存）
        _state.update(model_loaded=True, load_error=None)
        logger.info(
            "视觉查询服务就绪：模型加载+预热 %.1fs，RSS %.2f GiB",
            time.perf_counter() - t0, _rss_gb(),
        )
    except Exception as exc:  # noqa: BLE001 — 见上面：起来了更好诊断
        _state.update(model_loaded=False, load_error=str(exc)[:500])
        logger.error("视觉模型加载/预热失败（服务仍启动，/encode 会 503）：%s", exc)

    yield

    unload_all()
    logger.info("视觉查询服务退出")


app = FastAPI(
    title="PKH Visual Encode Service",
    version="0.1.0",
    description="文本 → colSmol multi-vectors。只做编码，不查 Qdrant、不做租户过滤（D158）",
    lifespan=lifespan,
    # ⚠️ 这个服务**只在回环地址上**（`deploy/dev.sh` 用 --host 127.0.0.1），
    #   它没有鉴权也不该有：暴露它等于让任何人都能加载 2.65GB 的模型并刷 CPU。
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.post("/encode", summary="文本 → multi-vectors")
async def encode(body: EncodeReq) -> Any:
    """colSmol 查询编码 → `[[128], [128], ...]`，形状 `(n_tokens, 128)`。

    🔑 失败一律用 503 而不是 500：调用方（API 进程）的契约是
    "**任何非 200 都降级成 `skipped`**"，而 500 在调用方那边会被当成
    "服务端 bug" —— 两种都降级，但 503 的文案能说清是模型没就绪。
    """
    text = (body.text or "").strip()
    if not text:
        return _error("BAD_REQUEST", "text 不能为空", 400)
    if not _state["model_loaded"]:
        # 🔑 "还要 13s" 这个数字**必须**写进去：调用方只有 2s 预算，
        #   看到裸的 503 会以为服务坏了而反复重试；看到"模型在加载"
        #   才知道该等。实测 12.8~15.0s（CONVENTIONS §4.1b）。
        return _error(
            "VISUAL_UNAVAILABLE",
            f"模型未加载完（首次加载约 13s）："
            f"{_state.get('load_error') or '仍在加载中，稍后重试即可'}",
            503,
        )

    assert _sem is not None, "lifespan 没跑（不该发生）"
    t0 = time.perf_counter()
    async with _sem:   # 🔴 并发上限在这里排队，不是靠调用方自觉
        try:
            # colSmol 前向是**阻塞**的 CPU 调用（实测 119~128ms，
            # 6 路并发时 p95 905ms）⇒ 必须丢进线程，不能堵住事件循环。
            vecs = await anyio.to_thread.run_sync(encode_vdr_query, text)
        except Exception as exc:  # noqa: BLE001
            return _error("VISUAL_UNAVAILABLE", f"编码失败：{exc}", 503)
    ms = int((time.perf_counter() - t0) * 1000)
    _durations.append(float(ms))
    return {
        "vectors": vecs,
        "n_tokens": len(vecs),
        "ms": ms,
        "dim": len(vecs[0]) if vecs else 0,
    }


@app.post("/encode_image", summary="图片 → multi-vectors（写侧，D178）")
async def encode_image(request: Request) -> Any:
    """一页图（原始 PNG/JPEG 字节）→ multi-vectors，形状 `(n_tokens, 128)`。

    给 worker 的**写**路径用（`visual_write_via_service=true`）：于是 worker
    不必自己加载 colSmol（2.65GB），多开 worker 才不会把内存乘 N（D178）。

    ⚠️ 与 `/encode` 同一契约：**任何非 200 都是可重试失败**（调用方 worker
    按 `jobs` 重试），绝不返回空向量 —— 空向量会让一份任务"成功"却永远召回不到。
    """
    if not _state["model_loaded"]:
        return _error(
            "VISUAL_UNAVAILABLE",
            f"模型未加载完（首次加载约 13s）："
            f"{_state.get('load_error') or '仍在加载中，稍后重试即可'}",
            503,
        )
    raw = await request.body()
    if not raw:
        return _error("BAD_REQUEST", "空的图片字节", 400)

    def _run() -> list[list[float]]:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(raw)) as im:
            return encode_images([im.convert("RGB")])[0]

    assert _img_sem is not None, "lifespan 没跑（不该发生）"
    t0 = time.perf_counter()
    async with _img_sem:   # 图片闸独立于查询闸（见 _MAX_IMAGE_CONCURRENCY）
        try:
            vecs = await anyio.to_thread.run_sync(_run)
        except Exception as exc:  # noqa: BLE001
            return _error("VISUAL_UNAVAILABLE", f"图片编码失败：{exc}", 503)
    return {
        "vectors": vecs,
        "n_tokens": len(vecs),
        "ms": int((time.perf_counter() - t0) * 1000),
        "dim": len(vecs[0]) if vecs else 0,
    }


@app.get("/health", summary="健康检查")
async def health() -> dict:
    """API 用它决定 `available_legs()` 里**有没有** `visual` —— 要如实汇报。

    ⚠️ **免鉴权**：它在这台机器的回环地址上，而 API 进程是唯一调用方
    （`deploy/dev.sh` 只绑 127.0.0.1）。给 API 加鉴权只会让"加了一次握手
    就多一处会漂移的配置"。
    """
    return {
        "service": "pkh-visual-encode",
        # ⚠️ `status` 与 `model_loaded` 分开：服务进程活着（ok）
        #   与"能用"（模型就绪）是两件事，混成一个字段就分不出
        #   "视觉服务挂了" 和 "视觉服务还在但模型没加载完"。
        "status": "ok",
        "model_loaded": bool(_state["model_loaded"]),
        "load_error": _state.get("load_error"),
        "pid": os.getpid(),
        "rss_gb": _rss_gb(),
        "model": settings.vdr_model,
        # 🔑 与 `model_status()` 同源取 `VDR_DIM` —— **不写死 128**：
        # 健康检查报 128 而 collection 是另一个维度，就是那种没法对账的
        # 状态（D124 第一版正是拿裸 AutoModel 的 960 维去写 128 的 collection）。
        "vdr_dim": VDR_DIM,
        "encode_p95_ms": percentile(list(_durations), 0.95),
        "encode_samples": len(_durations),
        "max_concurrency": _MAX_ENCODE_CONCURRENCY,
        "max_image_concurrency": _MAX_IMAGE_CONCURRENCY,
    }


__all__ = ["app", "encode", "encode_image", "health", "model_state", "percentile"]
