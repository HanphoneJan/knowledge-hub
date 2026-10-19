"""路由层共用的小工具。

单独抽出来是因为 `documents.py` 与后续的 `search.py` / `ask.py` 都要用，
而 `common/` 是给运行时核心用的，不该塞路由层的便利函数。

⚠️ 这里全部是**异步 session** 版本。`common/jobs.py` 里的同名函数是同步版
（给 worker 用）。两者行为必须一致，别改了一个忘了另一个。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from ...common.auth import assert_kb_access
from ...common.errors import ResourceNotFound
from ...common.jobs import enqueue_async as _jobs_enqueue_async
from ...common.models import Document, Job


def guess_mime(name: str) -> str | None:
    import mimetypes

    mt, _ = mimetypes.guess_type(name)
    return mt


def enqueue_async(session: AsyncSession, kind: str, **kw: Any):
    """入队（异步 session 版）。返回 `Job`（已 flush，拿到 id）。

    直接转调 `common.jobs.enqueue_async`，**不要**在这里包一层同步的
    `enqueue` —— 那只是把错误藏得更深：`common.jobs.enqueue` 是给 worker 的
    同步版，在 `AsyncSession` 上 `session.execute(...)` 返回 coroutine，
    紧接着的 `.scalar_one_or_none()` 会炸，而报错完全指不到真正原因。
    """
    return _jobs_enqueue_async(session, kind, **kw)


async def cancel_jobs_for_doc_async(session: AsyncSession, doc_id: int) -> int:
    """取消文档未完成的任务。返回取消数量。"""
    res = await session.execute(
        sa_update(Job)
        .where(Job.doc_id == doc_id, Job.status.in_(("pending", "running")))
        .values(status="cancelled", updated_at=dt.datetime.now(dt.UTC))
    )
    return res.rowcount or 0


async def load_document(session: AsyncSession, ctx, doc_id: int) -> Document:
    """取文档 + 校验 KB 访问权。

    只查 `is_current=TRUE` —— 回收站里的文档要走 `/restore`，
    不该从普通详情端点拿到（否则用户会以为它还在生效）。
    """
    doc = (
        await session.execute(
            select(Document).where(
                Document.id == doc_id, Document.is_current.is_(True)
            )
        )
    ).scalar_one_or_none()
    if doc is None:
        raise ResourceNotFound(detail={"doc_id": doc_id})
    await assert_kb_access(session, ctx, doc.kb_id)
    return doc
