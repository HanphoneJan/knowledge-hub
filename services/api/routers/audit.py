"""🔑 审计查询 `GET /api/v1/audit`（M8：审计"只写不查"补上只读那一半）。

## 为什么这个端点比看起来敏感

审计表记的是 `api_key_id → user_id → kb_ids → query 摘要`，
**query 摘要本身就是用户的数据**（"张三问了一句私事"这种）。
所以：

- **必须鉴权到工作区级**：契约写"需 workspace owner"。D4 暂不做 RBAC，
  owner 判据用现成的 `auth.can_admin_workspace`（§0.6：可见性判定只在那两处）。
- **无权限与不存在同码**（`KB_NOT_FOUND` / 403）。审计端点尤其不能泄漏
  "这个 workspace 有没有审计记录" —— 那本身也是信息。
- **`action` 先校验再查**：拼错一个值静默返回空列表 =骗人
  （用户会以为"这段时间没人用过"，其实是自己打错字）。

## 为什么筛选要 join `knowledge_bases`

`audit_log.kb_ids` 是 **数组**（一条记录可能涉及多个库，例如跨库检索），
不能直接 `WHERE kb_id = :ws`。所以走 `kb_ids && ARRAY[:ws]`（数组重叠），
再 join `knowledge_bases` 兜底 —— 后者是为了那些 `kb_ids` 已被删除的记录：
**审计不能因为库没了就查不到**（那正是审计的意义）。
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter
from sqlalchemy import select

from ...common.auth import can_admin_workspace
from ...common.deps import CurrentUser, DbSession
from ...common.errors import InvalidRequest, ResourceNotFound
from ...common.models import AuditLog, KnowledgeBase, Workspace
from ...common.pagination import Page, run_paginated
from ...common.schemas_ops import AuditOut

router = APIRouter(tags=["audit"])

#: 契约 §0.3 列过的动作 + 本项目实际写入的。
#: 写成常量而不是 `SELECT DISTINCT action FROM audit_log` ——
#: 那会让"打错字"与"没有记录"长得一模一样。
KNOWN_ACTIONS = (
    "search",
    "ask",
    "upload",
    "delete",
    "restore",
    "purge",
    "reindex",
    "mcp_call",
    "a2a_call",
)


@router.get("/audit", response_model=Page[AuditOut], summary="审计查询（工作区 owner）")
async def query_audit(
    ctx: CurrentUser,
    session: DbSession,
    workspace_id: int,
    user_id: int | None = None,
    action: str | None = None,
    date_from: dt.datetime | None = None,
    date_to: dt.datetime | None = None,
    page: int = 1,
    page_size: int = 50,
) -> Page[AuditOut]:
    """查某个工作区的审计记录（分页包，契约 §0.4）。

    ⚠️ **只能查自己有权管理的 workspace** —— 审计里有 query 摘要（用户的数据），
    跨租户可查等于开了个数据泄漏面。
    """
    if action is not None and action not in KNOWN_ACTIONS:
        raise InvalidRequest(
            f"未知动作 {action!r}；可用：{', '.join(KNOWN_ACTIONS)}"
        )
    if date_from is not None and date_to is not None and date_from > date_to:
        # 反过来给区间会返回空列表 = 骗人（"没有记录"其实是自己把日期写反了）
        raise InvalidRequest(
            f"date_from({date_from.isoformat()}) 晚于 date_to({date_to.isoformat()})"
        )

    ws = (
        await session.execute(
            select(Workspace).where(
                Workspace.id == workspace_id, Workspace.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    # 不存在与无权限同码（§0.4）
    if ws is None or not can_admin_workspace(ctx, ws):
        raise ResourceNotFound(detail={"workspace_id": workspace_id})

    # ⚠️ `audit_log.kb_ids` 存的是 **kb_id**，不是 workspace_id ——
    #   拿 workspace_id 去 overlap 会**永远 0 条**，而且 200 + 空列表
    #   看起来像"这段时间没人用过这个库"（实测踩过）。
    kb_ids = list(
        (
            await session.execute(
                select(KnowledgeBase.id).where(
                    KnowledgeBase.workspace_id == workspace_id,
                    KnowledgeBase.deleted_at.is_(None),
                )
            )
        ).scalars().all()
    )
    if not kb_ids:
        return Page[AuditOut](items=[], total=0, page=page, page_size=page_size)

    stmt = select(AuditLog).where(AuditLog.kb_ids.overlap(kb_ids))
    if user_id is not None:
        stmt = stmt.where(AuditLog.user_id == user_id)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    if date_from is not None:
        stmt = stmt.where(AuditLog.created_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(AuditLog.created_at <= date_to)

    rows, total = await run_paginated(
        session, stmt.order_by(AuditLog.id.desc()), page, page_size
    )
    return Page[AuditOut](
        items=[AuditOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


__all__ = ["router", "KNOWN_ACTIONS"]