"""检索内核编排 —— **API / MCP / A2A 三方共用的唯一入口**。

CONVENTIONS §0.3：检索只在这里实现。三个对外入口都调 `search()` /
`ask()`；任何一方想自己拼 Qdrant 查询或 SQL 子串查询，就是架构性错误 ——
半年后 API 搜得到的东西 MCP 搜不到，这类不一致极难排查。

## mode → 腿的映射（契约 §5.2）

| mode | 走的腿 | 能搜什么 |
|---|---|---|
| `semantic` | dense | 意思相近 |
| `exact` | trgm | 子串（`1381234`、`优秀学生奖学金`） |
| `hybrid`（默认） | dense + sparse + trgm | D39 三路 RRF |

`visual` 腿不在 mode 里 —— 它由 `visual_available()` 控制（D158）：
本地 `enable_visual`（worker / 视觉服务）或**远端视觉服务可达**。
API 进程本身**恒不加载 colSmol**（§4.1 未被推翻，一个字都没改）。
要按 modality 路由是阶段 3 `routing.py` 的事。

## 四条腿**真并发**：每条 DB 腿用自己的会话

D46 要求检索段 < 500ms。实测单腿：dense 56ms / sparse 153ms / trgm ~45ms。

🔴 **不能把调用方的同一个 `AsyncSession` 同时交给三条腿**：SQLAlchemy 的
`AsyncSession` 不允许并发 `execute`，冷会话下会抛 `This session is provisioning
a new connection; concurrent operations are not permitted`，sparse/trgm 被记成
`skipped`，于是 `hybrid` **静默降级成 dense 单腿**。

旧方案是加一把锁串行（≈254ms）。现在改成**每条 DB 腿从会话工厂各拿一个会话**
（各自的连接）⇒ 三条腿真正并发；视觉腿不碰 DB，也并发。代价是每次检索多占
2~3 条连接，连接池要按"并发检索数 × 4"估（见 `_gather_legs`）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

import anyio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..common.auth import UserContext
from ..common.db import get_async_sessionmaker
from ..common.models import Document
from .dense import search_dense
from .fusion import merge_fused, rrf
from .sparse import search_sparse
from .trgm import search_trgm
from .types import (
    ALL_LEGS,
    LEG_DENSE,
    LEG_SPARSE,
    LEG_TRGM,
    LEG_VISUAL,
    FusedHit,
    LegResult,
    citation_kind,
)
from .rerank import RerankOutcome, rerank
from .routing import Route, prefer, route
from .quality import EvidenceQuality, assess
from .visual import search_visual

SearchMode = Literal["semantic", "exact", "hybrid"]

#: 每个 mode 走哪几条腿。
MODE_LEGS: dict[str, tuple[str, ...]] = {
    "semantic": (LEG_DENSE,),
    "exact": (LEG_TRGM,),
    "hybrid": (LEG_DENSE, LEG_SPARSE, LEG_TRGM),
}

#: RRF 候选池深度取 `top_k` 的几倍。取浅了真值可能在第 11 名；
#: 取深了只多付"每腿的 limit"，而 query 编码只做一次 → 加深几乎免费。
CANDIDATE_MULTIPLIER = 3

#: 每腿候选的绝对上限。防 `top_k=100 × 3` 变 300 导致 payload 传输过大。
MAX_CANDIDATES = 100


@dataclass
class SearchOutcome:
    """一次检索的完整结果（含每腿明细，喂 `/search/debug`）。"""

    query: str
    mode: str
    items: list[FusedHit] = field(default_factory=list)
    legs: dict[str, LegResult] = field(default_factory=dict)
    elapsed_ms: dict[str, int] = field(default_factory=dict)
    total_ms: int = 0
    #: 被软删除二次过滤掉的命中数（见 `engine.search` 的说明）
    filtered_out: int = 0
    #: 查询类型路由的结论（阶段 3）。`/search/debug` 如实返回它 ——
    #: 路由是"猜的"，调参的人必须能看到系统为什么这么排（D72）。
    route: Route | None = None
    #: 路由偏好过滤后**没有生效**（库里没有对应类型）时的说明。
    #: ⚠️ 单独一个字段而不是塞进 `route.why`：那个对象是纯函数产物，
    #: 不该被"运行时才知道的事"污染（§0.9 的同源精神）。
    route_note: str = ""
    #: Reranker 的结果（阶段 3）。未启用/不可用时 `skipped` 有值、
    #: `items` 就是融合后的原顺序 —— `/search/debug` 据此如实显示。
    rerank: RerankOutcome | None = None
    #: D196：查询改写产出的 query 列表（第 0 个是原始 query）。
    #: 长度 1 = 没改写（默认）。`/search/debug` 如实回传。
    rewrite_queries: list[str] = field(default_factory=list)
    #: D197 证据质量（`retrieval/quality.py`）：给 LLM 生成器一个可依赖的
    #: "证据不足"信号，也让调试面板能一眼看出"这次排序有多可信"。
    quality: EvidenceQuality | None = None

    @property
    def total(self) -> int:
        return len(self.items)


async def _gather_legs(
    ctx: UserContext,
    query: str,
    legs: tuple[str, ...],
    kb_id: int | None,
    kb_ids: list[int] | None,
    per_leg: int,
    updated_after: str | None = None,
    updated_before: str | None = None,
) -> list[LegResult]:
    """并发跑选中的腿。

    视觉腿也在这里跑（而不是在 `MODE_LEGS` 里排除），因为它自己会立刻
    返回 `skipped` —— 这样 `/search/debug` 能**如实显示**这一腿存在但不可用，
    比悄悄不显示好：后者会让人以为"这个库没有照片"。

    ## 🔴 真并发：每条 DB 腿**用自己的会话**（不是加锁串行）

    三条文本腿都要做 DB 操作（dense 查索引模型一致性、sparse 查词表 /
    corpus_stats、trgm 查 Postgres）。把它们交给**同一个** `AsyncSession`
    并发 `execute` 会抛：

        This session is provisioning a new connection;
        concurrent operations are not permitted

    后果极隐蔽：sparse/trgm 被记成 `skipped`，`hybrid` **静默降级成 dense
    单腿**（检索仍 200、debug 也不报错）。API 的单库路径因为
    `assert_kb_access` 预热过会话而**碰巧**不触发，只有跨库 / MCP / A2A /
    直接调 `engine.search` 的冷会话会踩到。

    所以：每条 DB 腿从 `get_async_sessionmaker()` 各拿一个会话（各自的连接），
    四条腿一起 `anyio.gather` ⇒ 真正并发。视觉腿不碰 DB，不需要会话。

    ⚠️ **代价：连接数**。一次 `hybrid` 会占"请求会话 + 3 条腿"最多 4 条连接
    （`exact` 只跑 trgm，`semantic` 只跑 dense）。连接池
    （`pg_pool_size + pg_max_overflow`）要按 **并发检索数 × 4** 估；池不够时
    请求会排队（`pool_timeout`），不会算错，但延迟会退化。单机个人库默认
    5+5 足够；高并发部署要调大。
    """
    factory = get_async_sessionmaker()

    async def _dense():
        async with factory() as s:
            return await search_dense(
                ctx, query, kb_id=kb_id, kb_ids=kb_ids, limit=per_leg,
                session=s, updated_after=updated_after,
                updated_before=updated_before,
            )

    async def _sparse():
        async with factory() as s:
            return await search_sparse(
                s, ctx, query, kb_id=kb_id, kb_ids=kb_ids, limit=per_leg,
                updated_after=updated_after, updated_before=updated_before,
            )

    async def _trgm():
        async with factory() as s:
            return await search_trgm(
                s, ctx, query, kb_id=kb_id, kb_ids=kb_ids, limit=per_leg,
                updated_after=updated_after, updated_before=updated_before,
            )

    async def _visual():
        # 不碰 DB（Qdrant + 远端编码），不需要会话
        return await search_visual(
            ctx, query, kb_id=kb_id, kb_ids=kb_ids, limit=per_leg,
            updated_after=updated_after, updated_before=updated_before,
        )

    tasks: dict[str, Any] = {
        LEG_DENSE: _dense,
        LEG_SPARSE: _sparse,
        LEG_TRGM: _trgm,
        LEG_VISUAL: _visual,
    }
    wanted = list(legs) + ([LEG_VISUAL] if LEG_VISUAL not in legs else [])
    results = await anyio.gather(*[tasks[n]() for n in wanted])
    return list(results)


async def search(
    session: AsyncSession,
    ctx: UserContext,
    query: str,
    *,
    kb_id: int | None = None,
    kb_ids: list[int] | None = None,
    mode: SearchMode = "hybrid",
    top_k: int = 20,
    rerank_model: str | None = None,
    extra_queries: list[str] | None = None,
    updated_after: str | None = None,
    updated_before: str | None = None,
) -> SearchOutcome:
    """检索主入口。

    @param kb_id 单库检索。None = 跨库。
    @param rerank_model 该库的 `rerank_model`（阶段 3）。**None = 不启用**。
        ⚠️ 调用方必须**从库里读**（`kb.rerank_model`），不能从请求参数取 ——
        精排改变排序，排序是库的属性（同一个问题在不同库里答案不同）。
    @param kb_ids 跨库时的子集。⚠️ 它只是"意图"：实际范围是
        `ctx.readable_kb_ids ∩ kb_ids`（`build_qdrant_filter` / `build_kb_scope_sql`
        里求交，交集只会更小）。所以带无权限的 kb_id 进来**不会越权，只会少结果**。
    @param extra_queries D196 查询改写的变体（文章 §Query Rewrite）。**原始 query
        永远算一路**（`query` 本身），变体只做"多路共识"。空/None = 不改写，
        行为与引入前逐字一致。⚠️ 成本：#变体 × 三条 DB 腿（串行）直接乘进
        检索段延迟 —— 这也是它默认关闭、上限 `rewrite.DEFAULT_MAX_EXTRA` 的原因。

    ## 软删除二次过滤

    Qdrant 与 PG **允许**短暂不一致（D7：Qdrant 是可重建派生索引，
    `DELETE /documents/{id}` 先改 PG、Qdrant 由 cleanup 任务异步清）。
    所以向量腿可能召回已进回收站的文档。这里用一次批量查询把它挡掉：
    **绝不允许"PG 已删但 Qdrant 还能搜到"**（那才是真正的数据泄漏）。
    """
    t0 = time.perf_counter()
    legs = MODE_LEGS[mode]
    per_leg = min(MAX_CANDIDATES, max(top_k * CANDIDATE_MULTIPLIER, 30))

    # 阶段 3：先路由，拿权重（拿不准就是全 1.0 = 与今天完全一致）
    rte = route(query)
    queries = [query] + [q for q in (extra_queries or []) if q and q != query]

    if len(queries) == 1:
        # 默认路径：与引入查询改写前**逐字一致**
        leg_results = await _gather_legs(
            ctx, query, legs, kb_id, kb_ids, per_leg,
            updated_after, updated_before,
        )
        fused = rrf(leg_results, limit=top_k * 2, weights=rte.weights)
    else:
        # 多查询：逐条检索后融合。⚠️ **顺序**跑（不能并发）——
        #   共享 session 的并发 execute 会抛 "concurrent operations are not
        #   permitted"（见 `_gather_legs`）；而且这样每条 query 的 DB 腿
        #   仍各自串行，行为可预测。原始 query 是第一条。
        per_query: list[list[FusedHit]] = []
        leg_results: list[LegResult] = []
        for i, q in enumerate(queries):
            lr = await _gather_legs(
                ctx, q, legs, kb_id, kb_ids, per_leg,
                updated_after, updated_before,
            )
            if i == 0:
                leg_results = lr  # 调试面板显示**原始 query** 的每腿明细
            per_query.append(rrf(lr, limit=top_k * 2, weights=rte.weights))
        fused = merge_fused(per_query, limit=top_k * 2)

    filtered, dropped = await _drop_deleted(session, fused)

    # ⚠️ 顺序：软删过滤**在前面**，路由偏好在后面 —— 反过来的话，
    #   被路由滤掉的命中会先被算进 `filtered_out`，那个计数的含义就变了。
    kept, route_note = prefer(filtered, rte)
    kept = kept[:top_k]

    # ---- 阶段 3：cross-encoder 精排（默认不启用，按库开）----
    rr = await anyio.to_thread.run_sync(
        lambda: rerank(query, kept, limit=top_k, kb_rerank=rerank_model)
    )

    return SearchOutcome(
        query=query,
        mode=mode,
        items=rr.items[:top_k],
        legs={r.leg: r for r in leg_results},
        elapsed_ms={r.leg: r.elapsed_ms for r in leg_results},
        total_ms=int((time.perf_counter() - t0) * 1000),
        filtered_out=dropped,
        route=rte,
        route_note=route_note,
        rerank=rr,
        rewrite_queries=queries,
        quality=assess(rr.items),
    )


async def _drop_deleted(
    session: AsyncSession, hits: list[FusedHit]
) -> tuple[list[FusedHit], int]:
    """挡掉已软删除 / 非当前版本的文档。一次查询，不逐条查。

    只查 `hits` 里出现的 `doc_id`（`IN (...)`），所以代价与 top_k 成正比，
    与语料规模无关。
    """
    ids = {h.doc_id for h in hits if h.doc_id}
    if not ids:
        return hits, 0
    alive = {
        d
        for d in (
            await session.execute(
                select(Document.id).where(
                    Document.id.in_(ids),
                    Document.is_current.is_(True),
                    Document.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    }
    if not alive:
        return [], len(hits)
    kept = [h for h in hits if h.doc_id in alive]
    return kept, len(hits) - len(kept)


async def available_legs() -> list[str]:
    """本进程实际能跑的腿。`/health` 用它如实汇报，不假设四条都在。

    ⚠️ **不能是同步函数**（D158）：判据里的"远端视觉服务可达"要发 HTTP，
    在事件循环里做网络请求会堵住整个循环 —— `/health` 与
    `/kb/{id}/search/debug` 都在请求路径上，堵一次就是全站卡住。
    🔑 `visual_available()` 已是 `async`，这里**如实 await**。
    别把它包回 `anyio.to_thread.run_sync(visual_available)`：
    那等于在线程里跑事件循环，是本项目栽过四次的那一族
    （`tests/test_unawaited_coroutines.py`）。
    """
    from .visual import visual_available

    legs = [LEG_DENSE, LEG_SPARSE, LEG_TRGM]
    if await visual_available():
        legs.append(LEG_VISUAL)
    return legs


__all__ = [
    "ALL_LEGS",
    "MODE_LEGS",
    "SearchMode",
    "SearchOutcome",
    "available_legs",
    "citation_kind",
    "search",
]