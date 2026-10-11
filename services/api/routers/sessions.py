"""问答会话管理（契约 `docs/DESIGN-API.md` §7，D7 多轮）。

`1d` 的 SSE 端点已经把问答记录写进 `chat_sessions` / `chat_messages`，
这里补上读、改名、删。**只做 CRUD，检索与生成一律不在这里**（§0.3）。

## fail-closed

会话是**个人私产**（`chat_sessions.user_id` → users CASCADE，
`CONVENTIONS §1.3`）。所以判定只有一条：`row.user_id == ctx.user_id`。

⚠️ 不加这条检查的后果：传别人的 `session_id` 会把这一轮问答写进别人的会话 ——
既是数据泄漏，又会让人在别人的会话里看到自己的提问。`routers/ask.py::_assert_session`
是写入侧的同一道检查，**两边必须一致**。
"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select

from ...common.auth import assert_kb_access
from ...common.deps import CurrentUser, DbSession
from ...common.errors import ResourceNotFound
from ...common.models import ChatMessage, ChatSession
from ...common.pagination import run_paginated
from ...common.schemas import (
    CreateSessionReq,
    SessionDetail,
    SessionOut,
    UpdateSessionReq,
)

router = APIRouter(tags=["sessions"])


async def _load_session(session, ctx, session_id: int) -> ChatSession:
    """取会话并校验归属。失败抛 `ResourceNotFound`（同码，不泄漏存在性）。"""
    row = (
        await session.execute(
            select(ChatSession).where(
                ChatSession.id == session_id, ChatSession.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if row is None or row.user_id != ctx.user_id:
        raise ResourceNotFound(detail={"session_id": session_id})
    return row


@router.get("/kb/{kb_id}/sessions", summary="会话列表")
async def list_sessions(
    kb_id: int,
    ctx: CurrentUser,
    session: DbSession,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    await assert_kb_access(session, ctx, kb_id)
    rows, total = await run_paginated(
        session,
        select(ChatSession)
        .where(
            ChatSession.kb_id == kb_id,
            # ⚠️ 会话是个人私产：只看自己的（即使 KB 是公共的）
            ChatSession.user_id == ctx.user_id,
            ChatSession.deleted_at.is_(None),
        )
        .order_by(ChatSession.updated_at.desc()),
        page,
        page_size,
    )
    return {
        "items": [SessionOut.model_validate(r).model_dump(mode="json") for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post("/kb/{kb_id}/sessions", summary="新建会话")
async def create_session(
    body: CreateSessionReq,
    kb_id: int,
    ctx: CurrentUser,
    session: DbSession,
) -> SessionOut:
    await assert_kb_access(session, ctx, kb_id)
    row = ChatSession(kb_id=kb_id, user_id=ctx.user_id, title=body.title)
    session.add(row)
    await session.flush()
    return SessionOut.model_validate(row)


@router.get("/sessions", summary="我的会话列表（跨库，D163）")
async def list_my_sessions(
    ctx: CurrentUser,
    session: DbSession,
    kb_id: int | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """当前用户的**全部**会话（不限某个库）。

    ⚠️ 归属判定只有一条：`user_id == ctx.user_id`（会话是个人私产，§1.3）。
    `kb_id` 只是可选过滤（"这个库的会话"）。
    """
    stmt = select(ChatSession).where(
        ChatSession.user_id == ctx.user_id,
        ChatSession.deleted_at.is_(None),
    )
    if kb_id is not None:
        stmt = stmt.where(ChatSession.kb_id == kb_id)
    rows, total = await run_paginated(
        session, stmt.order_by(ChatSession.updated_at.desc()), page, page_size
    )
    return {
        "items": [SessionOut.model_validate(r).model_dump(mode="json") for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post("/sessions", summary="新建会话（可指定库范围，D163）")
async def create_my_session(
    body: CreateSessionReq,
    ctx: CurrentUser,
    session: DbSession,
) -> SessionOut:
    row = ChatSession(
        user_id=ctx.user_id,
        kb_id=(body.kb_ids[0] if body.kb_ids else None),
        kb_ids=body.kb_ids,
        title=body.title,
    )
    session.add(row)
    await session.flush()
    return SessionOut.model_validate(row)


@router.get("/sessions/{session_id}", summary="会话详情（含消息历史）")
async def get_session(
    session_id: int, ctx: CurrentUser, session: DbSession
) -> SessionDetail:
    row = await _load_session(session, ctx, session_id)
    msgs = (
        (
            await session.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.id)
            )
        )
        .scalars()
        .all()
    )
    return SessionDetail(
        **SessionOut.model_validate(row).model_dump(),
        messages=[
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "citations": m.citations,
                "created_at": m.created_at,
            }
            for m in msgs
        ],
    )


@router.patch("/sessions/{session_id}", summary="重命名会话")
async def rename_session(
    session_id: int,
    body: UpdateSessionReq,
    ctx: CurrentUser,
    session: DbSession,
) -> SessionOut:
    row = await _load_session(session, ctx, session_id)
    if body.title is not None:
        row.title = body.title
    await session.flush()
    return SessionOut.model_validate(row)


@router.delete("/sessions/{session_id}", summary="删会话（软删除）")
async def delete_session(
    session_id: int, ctx: CurrentUser, session: DbSession
) -> dict:
    """软删除（D26/D42 同一套 30 天机制，与 KB/文档一致）。"""
    import datetime as dt

    row = await _load_session(session, ctx, session_id)
    row.deleted_at = dt.datetime.now(dt.UTC)
    await session.flush()
    return {"id": session_id, "deleted": True}