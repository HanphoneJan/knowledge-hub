"""任务查询（阶段 2 补齐，契约 `docs/DESIGN-API.md` §12）。

worker 一直往 `jobs.progress` 写（D14 的进度回传，1d 里就通了），
但没有任何端点能读 —— 前端只能轮询 `documents.status` 看粗粒度状态，
**看不到百分比**。这个模块把那根管子接上。

## 租户口径

能看到的任务 = `jobs.kb_id ∈ ctx.readable_kb_ids`。

- **同源**：`ctx.readable_kb_ids` 由 `auth.load_user_context` 从 API Key 推导
  （CONVENTIONS §0.5），这里**只读不重算**可见性 —— 与 `build_qdrant_filter` /
  `build_kb_scope_sql` 同一真相源。
- **`kb_id IS NULL` 的任务自动看不见**：`cleanup` / `reindex` 这类运维任务
  不属于任何知识库，`IN (…)` 对 NULL 恒不匹配。这是 fail-closed 的免费收益，
  不需要额外白名单（加了反而要判断"该放行哪些"，那是新的越权面）。
- 无权限的 `job_id` 与不存在的 `job_id` 返回**同一个错误码**
  （`ResourceNotFound` → `KB_NOT_FOUND` / 403），见 CONVENTIONS §0.4。

## 为什么不做的事

- **不做 job 级 retry**：要重试文档摄取用 `POST /documents/{id}/retry`，
  它会重新入队 `KIND_INGEST`。给 `job_id` 再开一条重试路径，等于让
  "同一次摄取有两个入口"，而它们的去重语义（`jobs.enqueue(dedup=True)`）
  只能覆盖其中一个。
- **不做写操作**：本模块只读。任务的产生与终结都在 worker 侧。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select

from ...common.auth import UserContext, assert_kb_access
from ...common.deps import CurrentUser, DbSession
from ...common.errors import InvalidRequest, ResourceNotFound
from ...common.jobs import ALL_KINDS
from ...common.models import Job
from ...common.pagination import Page, run_paginated
from ...common.schemas import JobOut

router = APIRouter(tags=["jobs"])

#: `jobs.status` 的 CHECK 约束里的值。放在这里而不是让 pydantic 的
#: `Literal` 兜底 —— 端点自己要先校验，查错值时给的是
#: `INVALID_REQUEST`（400，参数错）而不是 pydantic 的 422。
JOB_STATUSES = ("pending", "running", "done", "failed", "cancelled")


def _visible_job_ids(ctx: UserContext) -> list[int]:
    """我有权限的 kb_id 列表（排序只为让 SQL 稳定，便于排查）。

    **不在这里重算可见性** —— `readable_kb_ids` 是唯一真相源（§0.5）。
    """
    return sorted(ctx.readable_kb_ids)


async def _load_job(session, ctx: UserContext, job_id: int) -> Job:
    """取任务 + 校验租户范围。失败抛 `ResourceNotFound`（同码，不泄漏存在性）。"""
    stmt = select(Job).where(
        Job.id == job_id,
        # ⚠️ 这条与 `kb_id` 查询参数是**同一个 `and_` 里的两个条件**。
        # 写成两个条件"追加"成另一条 `or` 会让限定完全无效 —— 那是本项目
        # 唯一一次真实越权的二次错误形态（CONVENTIONS §0.6）。
        Job.kb_id.in_(_visible_job_ids(ctx)),
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        # 无权限与不存在**同码同消息**（§0.4 fail-closed）
        raise ResourceNotFound(detail={"job_id": job_id})
    return row


@router.get(
    "/jobs",
    response_model=Page[JobOut],
    summary="任务列表（我有权访问的知识库范围内）",
)
async def list_jobs(
    ctx: CurrentUser,
    session: DbSession,
    kb_id: int | None = None,
    doc_id: int | None = None,
    kind: str | None = None,
    job_status: Annotated[
        str | None, Query(alias="status", description="pending/running/done/failed/cancelled")
    ] = None,
    page: int = 1,
    page_size: int = 20,
) -> Page[JobOut]:
    """按知识库 / 文档 / 类型 / 状态查任务。

    `kb_id` 传了就先校验访问权（`assert_kb_access`）——
    **这样"传了别人的 kb_id"与"传了不存在的 kb_id"返回同一个错误码**，
    与本项目其他端点一致（§0.4）。
    """
    if kb_id is not None:
        await assert_kb_access(session, ctx, kb_id)

    if kind is not None and kind not in ALL_KINDS:
        raise InvalidRequest(
            f"未知的任务类型 {kind!r}；可用：{', '.join(ALL_KINDS)}"
        )
    if job_status is not None and job_status not in JOB_STATUSES:
        raise InvalidRequest(
            f"未知的任务状态 {job_status!r}；可用：{', '.join(JOB_STATUSES)}"
        )

    visible = _visible_job_ids(ctx)
    if not visible:
        # 空授权集合 → 恒空。提前返回，**不**拼 `IN ()`（SQLAlchemy 会对空 IN
        # 发一条警告，且不同方言行为不一致：PG 报语法错、MySQL 恒假）。
        return Page[JobOut](items=[], total=0, page=page, page_size=page_size)

    stmt = select(Job).where(Job.kb_id.in_(visible))
    if kb_id is not None:
        stmt = stmt.where(Job.kb_id == kb_id)
    if doc_id is not None:
        stmt = stmt.where(Job.doc_id == doc_id)
    if kind is not None:
        stmt = stmt.where(Job.kind == kind)
    if job_status is not None:
        stmt = stmt.where(Job.status == job_status)

    rows, total = await run_paginated(
        session, stmt.order_by(Job.id.desc()), page, page_size
    )
    return Page[JobOut](
        items=[JobOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/jobs/{job_id}", response_model=JobOut, summary="任务详情")
async def get_job(job_id: int, ctx: CurrentUser, session: DbSession) -> JobOut:
    """单个任务。前端轮询这个端点拿 `progress`（0~1）。

    ⚠️ `progress` 是在 worker 的**独立事务**里写的（`worker/main.py::_progress`
    每次心跳开一个 `session_scope`），所以主任务还在跑时这里就读得到。
    `status` 才是"这件事成没成"的判据，`progress` 只是辅助 ——
    `finish_job` 只在成功时把它置 1.0，失败时它可能停在 0.7。
    """
    return JobOut.model_validate(await _load_job(session, ctx, job_id))


__all__ = ["router", "JOB_STATUSES"]