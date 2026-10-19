"""检索端点（契约 `docs/DESIGN-API.md` §5.2 / §5.3 / §5.4）。

三个端点：

| 端点 | 用途 |
|---|---|
| `POST /kb/{kb_id}/search` | 单库检索 |
| `POST /search` | 跨库检索（`kb_ids=null` = 有权限的全部） |
| `POST /kb/{kb_id}/search/debug` | 三路召回 + RRF 过程可视化（D15） |

## 路由层的职责边界

**只做四件事**：鉴权 → 调 `services/retrieval/` → 记审计 → 序列化。
检索逻辑**一行都不在这里**（CONVENTIONS §0.3：三方共用一个实现）。

⚠️ 特别地，**这里不出现任何 `{"key": "kb_id", ...}` 字面量** ——
租户过滤一律由 `retrieval` 内部调 `auth.build_qdrant_filter` /
`auth.build_kb_scope_sql` 完成（§0.2）。`tests/test_retrieval_filters.py`
会扫这个文件，出现即红。
"""

from __future__ import annotations

import time

from fastapi import APIRouter
from sqlalchemy import select

from ...common.audit import ACTION_SEARCH, write_audit
from ...common.auth import assert_kb_access
from ...common.deps import CurrentUser, DbSession
from ...common.errors import InvalidRequest
from ...common.models import KnowledgeBase
from ...common.schemas import (
    SEARCH_TEXT_CHARS,
    CrossKbSearchReq,
    HighlightSpan,
    SearchHit,
    SearchReq,
    SearchResp,
)
from ...retrieval import engine
from ...retrieval import rewrite as rewrite_mod
from ...retrieval.llm import load_llm_config
from ...retrieval.types import thumb_url_for
from ...retrieval.trgm import highlight_spans

router = APIRouter(tags=["search"])

#: 每条命中回多少字正文（D77）。


# ============================================================
# 序列化
# ============================================================



def _route_json(outcome) -> dict | None:
    """路由结论 → 响应。`None` 表示这次检索没带路由信息。"""
    r = getattr(outcome, "route", None)
    if r is None:
        return None
    return {
        "intent": r.intent,
        "weights": r.weights,
        "prefer_kinds": sorted(r.prefer_kinds) if r.prefer_kinds else None,
        "prefer_source_kinds": sorted(r.prefer_source_kinds)
        if r.prefer_source_kinds
        else None,
        "date_hint": r.date_hint,
        "confidence": r.confidence,
        "why": r.why,
        # ⚠️ `note` 是"运行时才知道的事"（库里有没有对应类型的库），
        #   所以与纯函数产物 `why` 分开两个字段（§0.9 的同源精神）
        "note": getattr(outcome, "route_note", ""),
    }


def _rerank_json(outcome) -> dict | None:
    """精排结论 → 响应。

    ⚠️ `scores` 是**未归一化**的 logits，不能当"置信度"展示
    （与 `SearchHit.score` 是 RRF 分同理，§5.2）。
    """
    rr = getattr(outcome, "rerank", None)
    if rr is None:
        return None
    return {
        "applied": rr.applied,
        "model": rr.model,
        "ms": rr.elapsed_ms,
        "skipped": rr.skipped,
        "scores": {k: round(v, 3) for k, v in rr.scores.items()},
    }


def _debug_block(outcome, top_k: int) -> dict:
    """四路召回 + RRF 融合过程的**唯一**构造处（D161）。

    `/search/debug` 与 `include_debug=true` 的普通检索共用它 ——
    两份拷贝迟早漂移，而"调试端点说 A、内联 debug 说 B"是最难查的一类
    （与 §0.6 的可见性、D140 的派发表同一个教训）。
    """
    rq = getattr(outcome, "rewrite_queries", None) or []
    q = getattr(outcome, "quality", None)
    return {
        "route": _route_json(outcome),
        "rerank": _rerank_json(outcome),
        # D196：查询改写结论（文章 §Query Rewrite）。`queries[0]` 是原始 query。
        "rewrite": {"queries": rq, "applied": len(rq) > 1},
        # D197：证据质量（拒答能力的信号，见 `retrieval/quality.py`）
        "quality": (
            {"level": q.level, "reason": q.reason, "max_legs": q.max_legs}
            if q is not None
            else None
        ),
        "legs": {
            leg: {
                "n": len(r.hits),
                "ms": r.elapsed_ms,
                # ⚠️ 如实返回跳过原因（D72）：视觉腿不可达时是 skipped 而不是 0 结果。
                "skipped": r.skipped,
                "items": [
                    {
                        "key": h.key,
                        "doc_id": h.doc_id,
                        "chunk_index": h.chunk_index,
                        "rank": h.rank,
                        "score": round(h.score, 5),
                        "file_name": h.payload.get("file_name"),
                        "text": h.text[:160],
                    }
                    for h in r.hits[:top_k]
                ],
            }
            for leg, r in outcome.legs.items()
        },
        "rrf": [
            {
                "key": h.key,
                "doc_id": h.doc_id,
                "chunk_index": h.chunk_index,
                "score": h.score,
                # 每腿排第几 —— 调试面板最想看的就是这个
                "ranks": h.ranks,
                "leg_scores": h.leg_scores,
                "legs": h.legs,
            }
            for h in outcome.items
        ],
        "elapsed": {**outcome.elapsed_ms, "total_ms": outcome.total_ms},
        "filtered_out_deleted": outcome.filtered_out,
    }


async def _kb_rerank(session, kb_id: int | None) -> str | None:
    """该库配的 Reranker 模型；**没配就返回 None**（= 不启用）。

    ⚠️ 跨库检索（`kb_id=None`）时**一律不启用**：不同库可能配了不同模型，
    而排序取决于"哪些库碰巧配了" —— 那不是用户能预期的行为。
    """
    if kb_id is None:
        return None
    row = (
        await session.execute(
            select(KnowledgeBase.rerank_model).where(KnowledgeBase.id == kb_id)
        )
    ).scalar_one_or_none()
    return row or None


async def _rewrite_extra(session, query: str, *, enabled: bool) -> list[str]:
    """查询改写（D196）：返回**额外**的 query（不含原始 query，原始由引擎保证）。

    @param enabled 请求级开关（`SearchReq.rewrite`）。false ⇒ 直接返回空，
        行为与引入前逐字一致。
    是否真的调用**外部 LLM** 由库配置决定：`rewrite_query(use_llm=True)` 内部
    会检查 `config.usable_for("rewrite")`，不满足就只做规则层变体（不外传）。
    """
    if not enabled:
        return []
    config = await load_llm_config(session)
    rr = await rewrite_mod.rewrite_query(query, config=config, use_llm=True)
    return rr.queries[1:]


def _time_kwargs(body) -> dict:
    """D199：把请求里的时间过滤转成引擎要的 RFC3339（Qdrant / PG 同一口径）。"""
    return {
        "updated_after": body.updated_after.isoformat() if body.updated_after else None,
        "updated_before": (
            body.updated_before.isoformat() if body.updated_before else None
        ),
    }


def _hit_to_schema(h, *, query: str) -> SearchHit:
    """`FusedHit` → 契约的 `SearchHit`。

    ⚠️ `score` 是 **RRF 分**（`1/60 + 1/62 ≈ 0.0326`），不是相似度。
    它只能用来**排序**，不能展示成"置信度 3%"。契约的 debug 端点里
    另有每腿的原始分数，那才是可比的。

    ⚠️ `text` 一定要给（D77）：`snippets` 只在 query 是原文**连续子串**时才有值，
    自然语言提问实测 100% 为空。没有 `text`，前端列表只剩文件名和 chunk 号 ——
    它得再发一轮请求去 chunks 里捞正文。
    """
    return SearchHit(
        doc_id=h.doc_id,
        file_name=h.payload.get("file_name") or "",
        chunk_index=h.chunk_index,
        text=(h.text or "")[:SEARCH_TEXT_CHARS],
        snippets=[HighlightSpan(**s) for s in highlight_spans(h.text, query)],
        score=h.score,
        kind=engine.citation_kind(h.kind, visual="visual" in h.legs),
        locations=h.locations,
        thumb_url=thumb_url_for(h),
    )



def _paginate(items: list[SearchHit], *, page: int, page_size: int, total: int):
    start = (page - 1) * page_size
    return items[start : start + page_size]


# ============================================================
# 端点
# ============================================================


@router.post("/kb/{kb_id}/search", response_model=SearchResp, summary="单库检索")
async def search_kb(
    body: SearchReq,
    kb_id: int,
    ctx: CurrentUser,
    session: DbSession,
) -> SearchResp:
    """三路 RRF 检索（`mode` 决定走哪几路，见 `retrieval/engine.py::MODE_LEGS`）。

    `page` / `page_size` 在**融合之后**切分：RRF 是全量排序，
    在融合前分页会拿不到跨腿的高分项。
    """
    await assert_kb_access(session, ctx, kb_id)

    t0 = time.perf_counter()
    # top_k 取到该页能覆盖的范围 —— 否则第 2 页永远是空的
    need = body.page * body.top_k
    outcome = await engine.search(
        session, ctx, body.query, kb_id=kb_id, mode=body.mode, top_k=need,
        rerank_model=await _kb_rerank(session, kb_id),
        extra_queries=await _rewrite_extra(session, body.query, enabled=body.rewrite),
        **_time_kwargs(body),
    )
    hits = [_hit_to_schema(h, query=body.query) for h in outcome.items]

    await write_audit(
        session,
        action=ACTION_SEARCH,
        user_id=ctx.user_id,
        api_key_id=ctx.api_key_id,
        kb_ids=[kb_id],
        detail={
            "query": body.query[:200],
            "mode": body.mode,
            "hits": len(hits),
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "legs": {k: len(v.hits) for k, v in outcome.legs.items()},
        },
    )

    page_size = body.top_k
    return SearchResp(
        items=_paginate(hits, page=body.page, page_size=page_size, total=len(hits)),
        total=len(hits),
        mode=body.mode,
        page=body.page,
        # D161：调试字段与结果同源，一次请求拿全（前端检索页不再并发调两个端点）
        debug=_debug_block(outcome, body.top_k) if body.include_debug else None,
    )


@router.post("/search", response_model=SearchResp, summary="跨库检索")
async def search_cross(
    body: CrossKbSearchReq,
    ctx: CurrentUser,
    session: DbSession,
) -> SearchResp:
    """`kb_ids=null` 表示"该用户有权限的所有 KB"（服务端推导，D8）。

    ⚠️ 请求体里**可以**带 `kb_ids`，但它只作为"想搜哪几个"的意图，
    最终范围仍然是 `ctx.readable_kb_ids ∩ kb_ids` ——
    `build_qdrant_filter` 只读 `readable_kb_ids`，所以**带一个无权限的
    kb_id 进来不会越权，只会少结果**（fail-closed）。
    """
    if body.kb_ids is not None and not body.kb_ids:
        raise InvalidRequest("kb_ids 传了空列表：请传 null 表示全部有权限的库")

    # 过滤一次不是"为了安全"（安全由 retrieval 内部的 build_* 保证），
    # 是为了让**审计里的 kb_ids 是真实访问过的**（D8 审计三元组）。
    wanted = (
        sorted(set(body.kb_ids) & set(ctx.readable_kb_ids))
        if body.kb_ids is not None
        else None
    )
    if wanted is not None and not wanted:
        # 全都不在授权范围内 → 空结果，**不报错**。
        # 报错会泄漏"这些 kb_id 里至少有一个存在"（§0.4 fail-closed
        # 要阻断的正是存在性探测）。
        return SearchResp(items=[], total=0, mode=body.mode, page=body.page)

    outcome = await engine.search(
        session,
        ctx,
        body.query,
        # ⚠️ 跨库一律不启用精排：不同库可能配了不同模型，而排序取决于
        #   "哪些库碰巧配了" —— 那不是用户能预期的行为（见 _kb_rerank）。
        rerank_model=await _kb_rerank(session, None),
        # ⚠️ 子集就传子集。不要图省事写"单库传 kb_id / 多库传 kb_id=None" ——
        # 那会让"搜 [1,2]"变成"搜所有有权限的库"，连用户没要求的 3 号库也读了。
        # 这是实现本端点时真实写出来过的 bug。**子集就是子集，别退回全集。**
        kb_ids=wanted,
        mode=body.mode,
        # D161：与单库端点一样按页取足（"top_k 取到该页能覆盖的范围"）
        top_k=body.page * body.top_k,
        extra_queries=await _rewrite_extra(session, body.query, enabled=body.rewrite),
        **_time_kwargs(body),
    )
    hits = [_hit_to_schema(h, query=body.query) for h in outcome.items]

    await write_audit(
        session,
        action=ACTION_SEARCH,
        user_id=ctx.user_id,
        api_key_id=ctx.api_key_id,
        kb_ids=sorted({h.payload.get("kb_id") for h in outcome.items if h.payload.get("kb_id")}),
        detail={
            "query": body.query[:200],
            "mode": body.mode,
            "cross_kb": True,
            "hits": len(hits),
        },
    )
    return SearchResp(
        items=_paginate(hits, page=body.page, page_size=body.top_k, total=len(hits)),
        total=len(hits),
        mode=body.mode,
        page=body.page,
        debug=_debug_block(outcome, body.top_k) if body.include_debug else None,
    )


@router.post("/kb/{kb_id}/search/debug", summary="检索调试面板")
async def search_debug(
    body: SearchReq,
    kb_id: int,
    ctx: CurrentUser,
    session: DbSession,
) -> dict:
    """把四路消融**可视化**（D15）。

    这是 `eval/retrieval_eval.py` 的交互版。给三类人看：
    - 用户：为什么这条排在前面
    - 前端：Reranker / routing 的调参依据（阶段 3）
    - 我自己：哪条腿拖后腿了（实测 photo 类 R@5=0.00 就是这么定位的）
    """
    await assert_kb_access(session, ctx, kb_id)
    # ⚠️ `rerank_model` 从**库里**读（不是请求参数）：精排改变排序，
    #   排序是库的属性（§0.6 的同源精神：可见性/配置都从库里来）
    t0 = time.perf_counter()
    outcome = await engine.search(
        session, ctx, body.query, kb_id=kb_id, mode=body.mode, top_k=body.top_k,
        rerank_model=(await _kb_rerank(session, kb_id)),
        extra_queries=await _rewrite_extra(session, body.query, enabled=body.rewrite),
        **_time_kwargs(body),
    )

    # 🔴 **它也是一次检索**（跑的是同一个 `engine.search`，还把四条腿的原始
    #   命中全返回了），而 M8 写的是"每次检索/问答记录
    #   api_key → user → kb_id → query"。此前这里**不记审计** ——
    #   于是"谁在拿我的库做召回探查"在审计里查不到（D139）。
    #   `legs` 一并记：调试面板最有用的就是"哪条腿召回了什么"。
    await write_audit(
        session,
        action=ACTION_SEARCH,
        user_id=ctx.user_id,
        api_key_id=ctx.api_key_id,
        kb_ids=[kb_id],
        detail={
            "query": body.query[:200],
            "mode": body.mode,
            "hits": len(outcome.items),
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "legs": {k: len(v.hits) for k, v in outcome.legs.items()},
            "surface": "search_debug",
        },
    )
    return {
        "query": body.query,
        "mode": outcome.mode,
        # 🔑 阶段 3：路由与精排的结论 + 四路召回/RRF。段与 `include_debug=true`
        #   的普通检索**共用 `_debug_block`**（D161），不在这里再写一版。
        **_debug_block(outcome, body.top_k),
    }