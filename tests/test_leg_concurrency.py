"""四条检索腿的**真并发**守卫（无需 DB / Qdrant / 模型）。

## 钉住的性质

1. **三条 DB 腿各自拿到一个会话**（不是共用调用方的那个）——
   SQLAlchemy 的 `AsyncSession` 不允许并发 `execute`，共用就会静默降级。
2. **四条腿真的同时起跑**（不是一把锁串行）—— 用一个"四者都到齐才放行"
   的屏障来判：如果实现退回串行，第一个 DB 腿会永远等不到其余腿，
   `anyio.fail_after` 会把它变成测试失败，而不是悄悄变慢。
3. 视觉腿**不拿会话**（它不碰 DB）。

⚠️ 这是**纯逻辑**测试：腿函数与会话工厂都被替换掉，所以能进 CI 的
免基础设施集合。真实 DB 路径由 `test_retrieval_e2e.py` 的冷会话用例覆盖。
"""

from __future__ import annotations

import inspect

import anyio
import pytest

from services.retrieval import engine
from services.retrieval.types import LEG_DENSE, LEG_SPARSE, LEG_TRGM, LEG_VISUAL, LegResult


class _Session:
    def __init__(self, n: int) -> None:
        self.n = n


class _Ctx:
    def __init__(self, session: _Session) -> None:
        self._session = session

    async def __aenter__(self) -> _Session:
        return self._session

    async def __aexit__(self, *exc) -> bool:
        return False


class _Factory:
    """假的 `async_sessionmaker`：每次调用造一个**不同**的会话。"""

    def __init__(self) -> None:
        self.made: list[_Session] = []

    def __call__(self) -> _Ctx:
        s = _Session(len(self.made))
        self.made.append(s)
        return _Ctx(s)


def test_gather_legs_does_not_serialize_db_legs():
    """🔑 设计守卫：不许退回 `anyio.Lock` 串行，必须用会话工厂。"""
    src = inspect.getsource(engine._gather_legs)
    assert "anyio.Lock" not in src, (
        "`_gather_legs` 又用锁串行了 —— 三条 DB 腿应各自拿会话真并发（见模块 docstring）"
    )
    assert "get_async_sessionmaker" in src, (
        "`_gather_legs` 没有为每条 DB 腿创建独立会话"
    )


def test_all_four_legs_start_concurrently_and_db_legs_get_own_sessions(monkeypatch):
    factory = _Factory()
    monkeypatch.setattr(engine, "get_async_sessionmaker", lambda: factory)

    seen: dict[str, object] = {}
    started = 0
    all_started = anyio.Event()

    async def _record(leg: str, session: object) -> LegResult:
        nonlocal started
        seen[leg] = session
        started += 1
        if started == 4:
            all_started.set()
        # 串行实现会卡在这里：后来者根本起不来
        with anyio.fail_after(2):
            await all_started.wait()
        return LegResult(leg=leg, hits=[], elapsed_ms=0)

    async def fake_dense(ctx, query, *, session, **kw):  # noqa: ANN001
        return await _record(LEG_DENSE, session)

    async def fake_sparse(session, ctx, query, **kw):  # noqa: ANN001
        return await _record(LEG_SPARSE, session)

    async def fake_trgm(session, ctx, query, **kw):  # noqa: ANN001
        return await _record(LEG_TRGM, session)

    async def fake_visual(ctx, query, **kw):  # noqa: ANN001
        return await _record(LEG_VISUAL, None)

    monkeypatch.setattr(engine, "search_dense", fake_dense)
    monkeypatch.setattr(engine, "search_sparse", fake_sparse)
    monkeypatch.setattr(engine, "search_trgm", fake_trgm)
    monkeypatch.setattr(engine, "search_visual", fake_visual)

    async def _run():
        return await engine._gather_legs(
            ctx=None,  # 腿函数被替换了，ctx 不参与
            query="q",
            legs=(LEG_DENSE, LEG_SPARSE, LEG_TRGM),
            kb_id=1,
            kb_ids=None,
            per_leg=10,
        )

    results = anyio.run(_run)

    assert len(results) == 4, "视觉腿也必须参与（debug 要能显示 skipped）"
    # 三条 DB 腿 = 三个**不同**的会话（各占一条连接）
    db_sessions = [seen[leg] for leg in (LEG_DENSE, LEG_SPARSE, LEG_TRGM)]
    assert all(isinstance(s, _Session) for s in db_sessions)
    assert len({id(s) for s in db_sessions}) == 3, (
        "三条 DB 腿共用了会话 —— 并发 execute 会静默降级成 dense 单腿"
    )
    assert len(factory.made) == 3, f"应创建 3 个会话，实际 {len(factory.made)}"
    # 视觉腿不碰 DB
    assert seen[LEG_VISUAL] is None, "视觉腿不该拿 DB 会话"
