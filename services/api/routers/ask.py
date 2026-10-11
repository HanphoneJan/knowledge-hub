"""问答端点（契约 `docs/DESIGN-API.md` §5.1，SSE）。

`POST /api/v1/kb/{kb_id}/ask` → `text/event-stream`

事件序列固定为 `sources` → `delta`* → `done`。**`sources` 必须最先发** ——
它是前端区分"还在检索"与"在生成"的唯一依据，也是 D46
（首 token < 2 秒，检索段 < 500ms）的可观测点。

## ⚠️ 这里不能用请求级 session 做流式期间的事

FastAPI ≥0.106 在 `yield` 依赖上把退出码放在**发送响应之前**执行
（本机 fastapi 0.142.2）。所以 `DbSession` 在第一个 SSE 帧之前就已经关闭了。
`retrieval/ask.py::stream_answer` 里只做 LLM 的 HTTP 往返，落库自己开
`session_scope()` —— 详见那个模块的 docstring（违反的后果与 PROGRESS §5.28
第 3 例同源：不报错、不告警、消息静默丢失）。

## 审计写在开流之前

SSE 一旦开始就没法改 HTTP 状态码，审计必须在 `StreamingResponse` 之前提交，
否则检索失败时审计会跟着回滚，而"检索失败"恰恰是最需要留痕的。
"""

from __future__ import annotations

import time

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from ...common.audit import ACTION_ASK, ACTION_SEARCH, write_audit
from ...common.auth import assert_kb_access
from ...common.deps import CurrentUser, DbSession
from ...common.errors import InvalidRequest, ModelUnavailable
from ...common.schemas import AskReq, CrossAskReq
from ...retrieval import ask as ask_core
from ...retrieval.engine import search

router = APIRouter(tags=["ask"])

#: SSE 帧之间不缓冲。nginx 之类反向代理默认会缓冲响应体，
#: 那样 `delta` 会被攒到全部生成完才吐给前端 —— D46 的"逐字输出掩盖慢速"就废了。
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    # 告诉 nginx 不要缓冲（X-Accel-Buffering 是 nginx 的约定头）
    "X-Accel-Buffering": "no",
}


@router.post("/kb/{kb_id}/ask", summary="RAG 问答（SSE）")
async def ask_kb(
    body: AskReq,
    kb_id: int,
    ctx: CurrentUser,
    session: DbSession,
) -> StreamingResponse:
    """检索 → 组织证据 → 流式生成 → 引用溯源。

    @param body.stream false 时也返回 SSE（契约统一），只是没有增量帧 ——
        不改成 JSON 是为了让 MCP / A2A 复用同一条代码路径（§0.3）。
    """
    await assert_kb_access(session, ctx, kb_id)

    # session_id 必须属于当前用户 + 当前 KB，否则是多轮串台
    if body.session_id is not None:
        await _assert_session(session, ctx, kb_id, body.session_id)

    t0 = time.perf_counter()
    try:
        plan = await ask_core.prepare(
            session,
            ctx,
            body.question,
            kb_id=kb_id,
            session_id=body.session_id,
            top_k=body.top_k,
        )
    except RuntimeError as exc:
        # 生成器配置缺失（`llm.py` 的隐私红线检查）。这是 503 而不是 500：
        # 服务端本身没坏，是配置没到位。
        raise ModelUnavailable(str(exc)) from exc

    if not body.stream:
        # 非流式：一次性跑完生成器，只发 sources + done 两帧
        pass  # 由 stream_answer(one_shot=...) 处理

    # 审计在开流之前提交（见模块 docstring）
    await write_audit(
        session,
        action=ACTION_ASK,
        user_id=ctx.user_id,
        api_key_id=ctx.api_key_id,
        kb_ids=[kb_id],
        detail={
            "question": body.question[:200],
            # 🔑 D141：用户问了什么与**我们检索了什么**分开记。
            #   指代式追问时两者不同（检索并上了上一轮问句）——
            #   只记一个的话，"为什么这次召回了奇怪的东西"就没法查了。
            "retrieval_query": plan.retrieval_query[:200],
            "query_expanded": plan.retrieval_query != body.question,
            "history_turns": len(plan.history),
            "session_id": body.session_id,
            "evidences": len(plan.evidences),
            "generator": plan.generator.name,
            "retrieval_ms": plan.outcome.total_ms,
            "legs": {k: len(v.hits) for k, v in plan.outcome.legs.items()},
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        },
    )

    return StreamingResponse(
        ask_core.stream_answer(plan, ctx=ctx, one_shot=not body.stream),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post("/ask", summary="RAG 问答（跨库 / 多库，SSE）· D163")
async def ask_cross(
    body: CrossAskReq,
    ctx: CurrentUser,
    session: DbSession,
) -> StreamingResponse:
    """与 `/kb/{id}/ask` 同一套内核，只是范围可以是**任意数量的库**（D163）。

    `kb_ids=None` = 全部有权限的库（D8 服务端推导）。
    ⚠️ 续问（带 `session_id`）时**沿用会话自己的范围** —— 不让范围中途变化，
    否则同一段对话前后的召回范围不同，答案会自相矛盾且无法解释。
    """
    if body.kb_ids is not None and not body.kb_ids:
        raise InvalidRequest("kb_ids 传了空列表：请传 null 表示全部有权限的库")

    scope: list[int] | None = body.kb_ids
    kb_id: int | None = None
    if body.session_id is not None:
        row = await _load_own_session(session, ctx, body.session_id)
        scope = row.kb_ids if row.kb_ids is not None else ([row.kb_id] if row.kb_id else None)
    elif scope is not None:
        kb_id = scope[0]

    t0 = time.perf_counter()
    try:
        plan = await ask_core.prepare(
            session,
            ctx,
            body.question,
            kb_id=kb_id,
            kb_ids=scope,
            session_id=body.session_id,
            top_k=body.top_k,
        )
    except RuntimeError as exc:
        raise ModelUnavailable(str(exc)) from exc

    await write_audit(
        session,
        action=ACTION_ASK,
        user_id=ctx.user_id,
        api_key_id=ctx.api_key_id,
        kb_ids=sorted(scope) if scope is not None else sorted(ctx.readable_kb_ids),
        detail={
            "question": body.question[:200],
            "retrieval_query": plan.retrieval_query[:200],
            "query_expanded": plan.retrieval_query != body.question,
            "history_turns": len(plan.history),
            "session_id": body.session_id,
            "evidences": len(plan.evidences),
            "generator": plan.generator.name,
            "retrieval_ms": plan.outcome.total_ms,
            "cross_kb": True,
            "legs": {k: len(v.hits) for k, v in plan.outcome.legs.items()},
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        },
    )

    return StreamingResponse(
        ask_core.stream_answer(plan, ctx=ctx, one_shot=not body.stream),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


async def _load_own_session(session, ctx, session_id: int):
    """取会话并校验**归属**（跨库问答用；范围由会话自己带）。"""
    from sqlalchemy import select

    from ...common.models import ChatSession

    row = (
        await session.execute(
            select(ChatSession).where(
                ChatSession.id == session_id, ChatSession.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if row is None or row.user_id != ctx.user_id:
        raise InvalidRequest("会话不存在或不属于当前用户")
    return row


async def _assert_session(session, ctx, kb_id: int, session_id: int) -> None:
    """会话必须属于当前用户且属于同一个 KB。

    ⚠️ 不加这条检查的话，传别人的 `session_id` 会把这一轮问答写进
    别人的会话里 —— 那既是数据泄漏（会话是个人私产，D7），
    又会让人在别人的会话里看到自己的提问。
    """
    from sqlalchemy import select

    from ...common.models import ChatSession

    row = (
        await session.execute(
            select(ChatSession).where(
                ChatSession.id == session_id, ChatSession.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if row is None or row.user_id != ctx.user_id or row.kb_id != kb_id:
        # 与"不存在"同一个错误码（fail-closed，§0.4）
        raise InvalidRequest("会话不存在或不属于当前知识库")


# ============================================================
# 检索调试：不走 SSE 的纯检索（契约 §5.4 在 search 路由里，这里只是薄封装）
# ============================================================


@router.get("/kb/{kb_id}/ask/preview", summary="只检索不生成")
async def ask_preview(
    kb_id: int,
    ctx: CurrentUser,
    session: DbSession,
    q: str,
    top_k: int = 8,
) -> dict:
    """拿到证据但**不生成答案**。

    前端"换个说法再问"之前先看证据列表用；也便于回答"为什么它这么答"。

    ⚠️ 这里调的是同一个 `engine.search`（§0.3），不是另写一套。
    """
    await assert_kb_access(session, ctx, kb_id)
    t0 = time.perf_counter()
    outcome = await search(session, ctx, q, kb_id=kb_id, top_k=top_k)

    # 🔴 它把**正文片段**（`text[:400]`）返回给调用方，而 M8 写的是
    #   "每次检索/问答记录 …"。此前不记审计 ⇒ "谁看过我的内容"查不到（D139）。
    #   `action` 用 `ACTION_SEARCH` 而不是 `ACTION_ASK`：它没有生成任何东西，
    #   记成 ask 会让"生成过答案"这个统计虚高。
    await write_audit(
        session,
        action=ACTION_SEARCH,
        user_id=ctx.user_id,
        api_key_id=ctx.api_key_id,
        kb_ids=[kb_id],
        detail={
            "query": q[:200],
            "hits": len(outcome.items),
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "surface": "ask_preview",
        },
    )
    return {
        "question": q,
        "retrieval_ms": outcome.total_ms,
        "evidences": [
            {
                "idx": i,
                "doc_id": h.doc_id,
                "chunk_index": h.chunk_index,
                "score": h.score,
                "legs": h.legs,
                "ranks": h.ranks,
                "title": h.payload.get("file_name"),
                "text": h.text[:400],
                "locations": h.locations,
            }
            for i, h in enumerate(outcome.items, start=1)
        ],
    }