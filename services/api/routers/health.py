"""健康检查。

两个端点：
- `GET /health` —— **不需鉴权**。给 Docker healthcheck / 反代探活用
- `GET /api/v1/health` —— 需鉴权。返回更详细的模型状态与**队列状态**

设计取舍：模型加载状态放在鉴权版本里，因为"bge 加载失败"这类信息
不该暴露给公网探活端点（会泄露部署细节）。D133 把**队列状态**按同一条
理由放进鉴权版本 —— 队列有多深、卡了多久、24 小时失败了多少，
也是部署细节，而且比模型状态更敏感（能看出这个实例在忙什么）。
"""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter

from ...common.db import check_db
from ...common.deps import CurrentUser
from ...common.qdrant import check_qdrant
from ...common.schemas import HealthResp

logger = logging.getLogger(__name__)
router = APIRouter()
public_router = APIRouter()


def _worker_hint() -> dict:
    """worker 是独立进程（D50），这里只能靠"有没有待处理任务"间接推断。

    不做心跳表：那需要一个常写 PG 的进程，而 worker 空闲时本来就不该写库。
    🔴 所以"worker 活着吗"这个问题**这里答不了** —— 别把队列状态说成心跳
    （D133）：队列状态回答的是"任务堆了多深、最老的还在跑多久"。
    """
    return {"note": "独立进程，见 deploy/docker-compose.yml 的 worker 服务"}


async def queue_status() -> dict:
    """队列状态（`jobs` 表的一次聚合，**不需要** worker 配合）。

    🔑 为什么**不需要心跳表**就能回答"队列健康吗"：
    我们不需要知道 worker 的心跳，只需要知道**任务堆了多深**与
    **最老的任务跑了多久** —— 两者都在 `jobs` 表里，一次聚合就有。
    心跳表要求一个空闲时也常写 PG 的进程，而空闲 worker 本来就不该写库。

    ⚠️ **绝不抛异常**：健康检查因为一个附加查询而 500，
    等于"因为想多报一点信息而让探针报不健康" —— 那是把诊断变成故障。
    查不到就返回 `{"error": ...}`，由读的人决定怎么解释。
    """
    from sqlalchemy import func, select

    from ...common.db import get_async_sessionmaker
    from ...common.models import Job

    now = dt.datetime.now(dt.UTC)
    day_ago = now - dt.timedelta(days=1)
    try:
        async with get_async_sessionmaker()() as session:
            row = (
                await session.execute(
                    select(
                        func.count(Job.id).filter(Job.status == "pending"),
                        func.count(Job.id).filter(Job.status == "running"),
                        func.count(Job.id).filter(
                            Job.status == "failed", Job.updated_at >= day_ago
                        ),
                        func.min(Job.locked_at).filter(Job.status == "running"),
                    )
                )
            ).one()
        pending, running, failed_24h, oldest = row
        out: dict = {
            "pending": int(pending or 0),
            "running": int(running or 0),
            "failed_24h": int(failed_24h or 0),
        }
        # 🔑 `oldest_running_seconds` 只在**真的有任务在跑**时给出：
        #   没有 running 时给 0 会被读成"刚跑完一个 0 秒的任务"。
        out["oldest_running_seconds"] = (
            max(0, int((now - oldest).total_seconds())) if oldest else None
        )
        # ⚠️ 队列深度的"危险线"要**写出来**，否则读的人得自己猜多少算多。
        #   monitor 的第 10 项用的是"卡住 > 30 分钟"，这里给的是**深度**，
        #   两者互补：深度看堆积，最长运行时间看卡死。
        out["stuck_after_seconds"] = 1800
        return out
    except Exception as exc:  # noqa: BLE001 — 诊断信息不能变成故障
        logger.warning("队列状态读不到：%s", exc)
        return {"error": type(exc).__name__}


async def index_model_status() -> dict:
    """🔑 D191：每条向量腿"建索引用的模型 vs 当前配置"（文章「Embedding 一致性」）。

    三态，与全仓"未知 ≠ 通过"的纪律一致：
    `matched=True` 一致 · `False` 不一致（dense 腿会降级）· `None` 未知（尚无记录）。

    ⚠️ **绝不抛异常**（与 `queue_status` 同一条理由）：健康检查因为一个附加查询
    而 500，等于把诊断变成故障。
    """
    from ...common.config import get_settings
    from ...common.db import get_async_sessionmaker
    from ...common.index_models import get_models_async
    from ...common.qdrant import COLL_DENSE, COLL_VISUAL, VDR_DIM

    s = get_settings()
    expected = {
        COLL_DENSE: (s.text_embed_model, s.text_embed_dim),
        COLL_VISUAL: (s.vdr_model, VDR_DIM),
    }
    try:
        async with get_async_sessionmaker()() as session:
            recorded = await get_models_async(session)
    except Exception as exc:  # noqa: BLE001
        logger.warning("索引模型状态读不到：%s", exc)
        return {"error": type(exc).__name__}

    out: dict = {}
    for coll, (model, dim) in expected.items():
        rec = recorded.get(coll)
        out[coll] = {
            "recorded": rec.model if rec else None,
            "expected": model,
            "dim": dim,
            "matched": (
                None if rec is None else (rec.model == model and rec.dim == dim)
            ),
        }
    return out


async def _collect(include_models: bool) -> dict:
    db = await check_db()
    qdrant = await check_qdrant()

    payload: dict = {
        "status": "ok" if db.get("ok") and qdrant.get("ok") else "degraded",
        "db": db,
        "qdrant": qdrant,
    }

    if include_models:
        from ...retrieval.engine import available_legs
        from ...retrieval.pool import model_status

        payload["models"] = model_status()
        payload["worker"] = _worker_hint()
        # 🔑 D158：视觉腿接通后"这个部署能跑哪几条腿"不再固定 ——
        #   装没装视觉服务、`kb_visual` 有没有点，答案都不一样。
        #   所以它**如实**汇报，而不是硬编码三条（那是接通前的状态）。
        payload["available_legs"] = await available_legs()
        # 🔴 D133：队列状态（`jobs` 表聚合）。**只在鉴权版**里给 ——
        #   "堆了 200 个任务、24 小时失败 37 个"是部署细节，
        #   不该出现在免鉴权的公网探活端点上（与 models 同一条理由）。
        payload["queue"] = await queue_status()
        # 🔑 D191：Embedding 一致性（建索引的模型 vs 当前配置）。同一条理由只在鉴权版。
        payload["index_models"] = await index_model_status()

    return payload


@public_router.get("/health", response_model=HealthResp, summary="存活探针（免鉴权）")
async def health() -> HealthResp:
    data = await _collect(include_models=False)
    return HealthResp(**data)


@router.get("/health", response_model=HealthResp, summary="详细健康检查")
async def health_detail(_ctx: CurrentUser) -> HealthResp:
    data = await _collect(include_models=True)
    return HealthResp(**data)