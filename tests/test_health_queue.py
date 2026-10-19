"""🔑 D133：`/health` 的**队列状态**（M9 写着「含 DB/Qdrant/**队列**状态 ✅」）。

## 缺口

M9 那行是这么写的：

```
| 健康检查 | `/health` 含 DB/Qdrant/队列状态 | ✅ |
```

而 `routers/health.py` 里原来只有：

```python
def _worker_hint() -> dict:
    return {"note": "独立进程，见 deploy/docker-compose.yml 的 worker 服务"}
```

也就是说"队列状态"是**一句静态说明** —— 无论队列里堆了 200 个任务、
还是卡着一个跑了 3 小时的任务，返回的 JSON **一模一样**。
于是"worker 卡住了"这件事**在健康检查里看不出来**（只有 monitor 的第 10 项能看），
而 `/health` 是反代与 Docker healthcheck 唯一会看的地方。

## 这一组钉的是"它真的在说队列的事"

1. 🔑 **数字 == 直接查 `jobs` 表**（自己写一份 SQL 比对，不复用被测代码）。
2. 🔑 **卡住看得见**：造一个 `running` 且 `locked_at` 在 40 分钟前的任务 →
   `oldest_running_seconds ≥ 2400`；**没有** running 时是 `null` 而不是 `0`
   （`0` 会被读成"刚跑完一个 0 秒的任务"）。
3. 🔑 **不泄露到免鉴权端点**：队列深度与失败数是部署细节。
4. 🔑 **诊断不许变成故障**：查询失败时返回 `{"error": …}`，健康检查仍然 200 ——
   "因为想多报一点信息而让探针报不健康"是本末倒置。
"""
from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_LOOP = asyncio.new_event_loop()


def _run(coro):  # noqa: ANN001, ANN201
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module", autouse=True)
def _own_the_async_engine():
    """进出都销毁 async 引擎单例（CONVENTIONS.md §9；否则连接池绑在别的 loop 上）。"""
    from services.common.db import dispose_engines

    dispose_engines()
    yield

    async def _close() -> None:
        from services.common.db import get_async_engine

        await get_async_engine().dispose()

    _LOOP.run_until_complete(_close())
    dispose_engines()


def _pg_alive() -> bool:
    from sqlalchemy import text

    from services.common.db import sync_session_scope

    try:
        with sync_session_scope() as s:
            s.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


pg = pytest.mark.skipif(not _pg_alive(), reason="Postgres 不在线")


# ============================================================
# 1. 静态：字段与"不泄露"这条设计
# ============================================================


def test_health_resp_has_a_queue_field():
    from services.common.schemas import HealthResp

    assert "queue" in HealthResp.model_fields, (
        "HealthResp 没有 queue 字段 —— M9 的「队列状态」在契约层就没有落点"
    )


def test_queue_is_collected_only_in_the_authenticated_endpoint():
    """🔑 队列状态**只在鉴权版**里收集（与 `models` 同一条理由）。

    "堆了 200 个任务、24 小时失败 37 个"是部署细节；
    免鉴权的 `/health` 是给公网探活用的。
    """
    from services.api.routers import health as mod

    src = (ROOT / "services" / "api" / "routers" / "health.py").read_text(
        encoding="utf-8"
    )
    assert src.count("await queue_status()") == 1, (
        "queue_status 只应在 `_collect(include_models=True)` 那一条路径上调用"
    )
    # 免鉴权端点必须显式传 False
    assert "_collect(include_models=False)" in src, (
        "免鉴权端点居然也在收详细状态"
    )
    assert mod._worker_hint()["note"], "worker 说明丢了"


def test_queue_status_never_raises():
    """🔑 查询失败时返回 `{"error": …}`，**不抛**。

    ⚠️ 健康检查因为一个附加查询而 500，等于"因为想多报一点信息
    而让探针报不健康" —— 那是把诊断变成故障。
    """
    from services.api.routers import health as mod

    async def boom():  # noqa: ANN202
        raise RuntimeError("PG 连不上")

    class _FakeSessionmaker:  # noqa: N801
        def __call__(self):  # noqa: ANN204
            raise RuntimeError("PG 连不上")

    import services.common.db as db_mod

    orig = db_mod.get_async_sessionmaker
    db_mod.get_async_sessionmaker = lambda: _FakeSessionmaker()  # type: ignore[assignment]
    try:
        out = _run(mod.queue_status())
    finally:
        db_mod.get_async_sessionmaker = orig  # type: ignore[assignment]
    assert isinstance(out, dict) and "error" in out, out
    assert out["error"], out
    del boom


# ============================================================
# 2. 行为（真 PG）
# ============================================================


@pg
def test_queue_numbers_match_a_direct_query():
    """🔑 数字 == 直接查 `jobs` 表（**自己写一份 SQL**，不复用被测代码）。"""
    from sqlalchemy import func, select, text

    from services.api.routers.health import queue_status
    from services.common.db import sync_session_scope
    from services.common.models import Job

    out = _run(queue_status())
    assert "error" not in out, out
    with sync_session_scope() as s:
        row = s.execute(
            select(
                func.count(Job.id).filter(Job.status == "pending"),
                func.count(Job.id).filter(Job.status == "running"),
                func.count(Job.id).filter(
                    Job.status == "failed",
                    Job.updated_at >= dt.datetime.now(dt.UTC) - dt.timedelta(days=1),
                ),
            )
        ).one()
        direct = {"pending": int(row[0]), "running": int(row[1]),
                  "failed_24h": int(row[2])}
        # 顺带确认"stuck_after_seconds"与 monitor 第 10 项的口径一致
        stuck = s.execute(text("SELECT 1800")).scalar_one()
    for k, v in direct.items():
        assert out[k] == v, f"{k}={out[k]} 与直接查的 {v} 不一致"
    assert out["stuck_after_seconds"] == stuck


@pg
def test_oldest_running_seconds_reports_a_stuck_job():
    """🔑 卡住的任务**看得见**：`running` 且锁了 40 分钟 → ≥2400 秒。

    ⚠️ 没有 running 时必须是 `None` 而不是 `0` ——
    `0` 会被读成"刚跑完一个 0 秒的任务"，而真相是"现在没有任务在跑"。
    """
    from sqlalchemy import select

    from services.api.routers.health import queue_status
    from services.common.db import sync_session_scope
    from services.common.models import Job

    now = dt.datetime.now(dt.UTC)
    before = _run(queue_status())
    assert (before["running"] == 0) == (before["oldest_running_seconds"] is None), (
        f"没有 running 任务时 oldest 必须是 None：{before}"
    )

    with sync_session_scope() as s:
        kb_id = s.execute(select(Job.kb_id).where(Job.kb_id.isnot(None)).limit(1)
                          ).scalar_one_or_none()
        job = Job(
            kind="thumbnail", status="running", kb_id=kb_id, progress=0.5,
            attempts=1, max_attempts=3, locked_at=now - dt.timedelta(minutes=40),
            locked_by="pytest-d133", created_at=now - dt.timedelta(minutes=41),
            updated_at=now - dt.timedelta(minutes=40),
        )
        s.add(job)
        s.flush()
        job_id = job.id
    try:
        out = _run(queue_status())
        assert out["running"] >= 1, out
        assert out["oldest_running_seconds"] is not None, (
            "有一个跑了 40 分钟的 running 任务，却报 oldest=None"
        )
        assert out["oldest_running_seconds"] >= 2400, (
            f"只报 {out['oldest_running_seconds']} 秒 —— 40 分钟的任务看不出来"
        )
        assert out["oldest_running_seconds"] > out["stuck_after_seconds"], (
            "它已经超过 monitor 的 30 分钟红线，却没被这个数字暴露出来"
        )
    finally:
        with sync_session_scope() as s:
            s.execute(Job.__table__.delete().where(Job.id == job_id))


@pg
def test_public_health_does_not_expose_queue_values():
    """🔑 免鉴权 `/health` 里 queue 是空的（部署细节不外泄）。"""
    from services.api.routers.health import health, health_detail

    pub = _run(health())
    assert pub.queue == {}, f"免鉴权端点泄露了队列状态：{pub.queue}"
    assert pub.models == {}, f"免鉴权端点泄露了模型状态：{pub.models}"

    det = _run(health_detail(None))     # 端点签名里 ctx 没被用到
    assert det.queue, "鉴权版没有 queue —— D133 白做了"
    assert "error" not in det.queue, det.queue
    assert det.models, "鉴权版应该带 models"
    # 同一个模型 ⇒ 反代与监控脚本一个解析器读两个端点
    assert det.status in ("ok", "degraded")