"""工作区端点（D5：工作区 → 知识库 → 文档 的第一层）。

契约见 `docs/DESIGN-API.md` 第 2 节。

权限模型（D4 暂不做 RBAC）：
- **owner**：改设置、增删成员、删工作区
- **member**：只读（列 KB、检索）

⚠️ 两处 fail-closed（`CONVENTIONS.md` §0.4）：
1. 非成员访问工作区 → `KB_NOT_FOUND`（403），与"不存在"同码
2. 是 member 但不是 owner → **同样** `KB_NOT_FOUND`。
   不能让调用方通过"错误码不同"推断"我确实是成员但权限不够"，
   那等于确认了资源存在。
"""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, status
from sqlalchemy import func, or_, select

from ...common.auth import assert_workspace_access, assert_workspace_admin
from ...common.deps import CurrentUser, DbSession
from ...common.errors import InvalidRequest, ResourceNotFound
from ...common.models import KnowledgeBase, User, Workspace, WorkspaceMember
from ...common.pagination import run_paginated
from ...common.schemas import (
    AddMemberReq,
    CreateWorkspaceReq,
    MemberOut,
    PageMeta,
    UpdateWorkspaceReq,
    WorkspaceOut,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/workspaces", tags=["workspaces"])


@router.get("", response_model=list[WorkspaceOut], summary="我加入的工作区")
async def list_workspaces(
    session: DbSession, ctx: CurrentUser, page: int = 1, page_size: int = 50
) -> list[WorkspaceOut]:
    """只返回我**是成员**的（不是"我创建的"）。

    `ctx.workspace_ids` 是登录时从 `workspace_members` 推导的，
    直接用它做过滤 —— 不重新查成员表，省一次往返，
    也保证过滤逻辑与登录态一致。
    """
    if not ctx.workspace_ids:
        return []
    stmt = (
        select(Workspace)
        .where(
            Workspace.id.in_(ctx.workspace_ids),
            Workspace.deleted_at.is_(None),
        )
        .order_by(Workspace.created_at)
    )
    rows, _ = await run_paginated(session, stmt, page, page_size)
    return [WorkspaceOut.model_validate(r) for r in rows]


@router.get(
    "/trash", response_model=list[WorkspaceOut], summary="工作区回收站（我删掉的）"
)
async def list_workspaces_trash(
    session: DbSession, ctx: CurrentUser
) -> list[WorkspaceOut]:
    """**软删**的工作区列表（D129）。

    🔑 与 `POST /workspaces/{id}/restore` 配对：恢复端点 D125 补上了，
    但**没有地方能列出"我删过哪些工作区"** —— 只能凭 id 调恢复。
    D42 承诺的是"30 天反悔窗口"，看不见的窗口等于没有。

    ⚠️ **只列 `owner_id == 我` 的**：恢复也只允许 owner
    （`restore_workspace` 的判据，`delete_workspace` 是 D42 的最重操作）。
    ⚠️ 而普通**成员**看不到这个列表 —— 他们从来没被允许删它，
    给他们一个看得见恢复不了的条目毫无意义（点了就是 403）。
    ⚠️ 不能用 `ctx.workspace_ids` 过滤：软删的行**不在**那个集合里
    （它由未软删的成员关系推导）→ 恒为空。与 D125 的 restore 同一堵墙。
    """
    rows = (
        await session.execute(
            select(Workspace)
            .where(
                Workspace.owner_id == ctx.user_id,
                Workspace.deleted_at.isnot(None),
            )
            .order_by(Workspace.deleted_at.desc())
        )
    ).scalars().all()
    return [WorkspaceOut.model_validate(r) for r in rows]


@router.post(
    "",
    response_model=WorkspaceOut,
    status_code=status.HTTP_201_CREATED,
    summary="创建工作区",
)
async def create_workspace(
    req: CreateWorkspaceReq, session: DbSession, ctx: CurrentUser
) -> WorkspaceOut:
    ws = Workspace(
        name=req.name,
        description=req.description,
        owner_id=ctx.user_id,
    )
    session.add(ws)
    await session.flush()
    # 创建者自动成为 owner 成员 —— 少了这行，
    # `assert_workspace_access` 会因为"不是成员"把自己拒之门外
    session.add(WorkspaceMember(workspace_id=ws.id, user_id=ctx.user_id, role="owner"))
    await session.commit()
    return WorkspaceOut.model_validate(ws)


@router.get("/{ws_id}", response_model=WorkspaceOut, summary="工作区详情")
async def get_workspace(
    ws_id: int, session: DbSession, ctx: CurrentUser
) -> WorkspaceOut:
    ws = await assert_workspace_access(session, ctx, ws_id)
    return WorkspaceOut.model_validate(ws)


@router.patch("/{ws_id}", response_model=WorkspaceOut, summary="改工作区设置")
async def update_workspace(
    ws_id: int, req: UpdateWorkspaceReq, session: DbSession, ctx: CurrentUser
) -> WorkspaceOut:
    ws = await assert_workspace_admin(session, ctx, ws_id)
    if req.name is not None:
        ws.name = req.name
    if req.description is not None:
        ws.description = req.description
    await session.commit()
    return WorkspaceOut.model_validate(ws)


@router.delete(
    "/{ws_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除工作区（软删除）"
)
async def delete_workspace(
    ws_id: int, session: DbSession, ctx: CurrentUser
) -> None:
    """软删除（D42：与 KB/文档同一套 30 天机制）。

    只标记 `deleted_at`，不立刻删数据 —— 误删可恢复（`POST /{ws_id}/restore`）。
    ⚠️ 其下的 KB / 文档 / 向量**不在这里级联删**：
    30 天后由 cleanup 任务统一处理，那时才真正删。
    """
    ws = await assert_workspace_admin(session, ctx, ws_id)
    ws.deleted_at = dt.datetime.now(dt.UTC)
    await session.commit()


@router.post("/{ws_id}/restore", response_model=WorkspaceOut, summary="恢复工作区")
async def restore_workspace(
    ws_id: int, session: DbSession, ctx: CurrentUser
) -> WorkspaceOut:
    """恢复被软删的工作区（D42 的"反悔窗口"，D125 补齐）。

    🔑 只翻 `workspaces.deleted_at` **一行**：删除时**故意没有**级联软删
    其下的 KB / 文档（见 `delete_workspace`），所以恢复也是一行 ——
    数据一直在，只是对外不可见（`load_user_context` 过滤 `deleted_at IS NULL`）。

    ⚠️ 鉴权同样要**先按行取、再判权限**：软删的工作区不在
    `ctx.workspace_ids` 里（它由未软删的成员关系推导），
    所以 `assert_workspace_admin` 会永远 403 —— 恢复功能等于不存在。
    仍然要求 **owner**（删工作区比删文档严重得多），
    失败与不存在**同码**（§0.4）。
    """
    ws = (
        await session.execute(
            select(Workspace).where(
                Workspace.id == ws_id, Workspace.deleted_at.isnot(None)
            )
        )
    ).scalar_one_or_none()
    if ws is None or ws.owner_id != ctx.user_id:
        raise ResourceNotFound(detail={"workspace_id": ws_id})
    ws.deleted_at = None
    logger.info("恢复工作区 %s", ws_id)
    await session.commit()
    return WorkspaceOut.model_validate(ws)


# ============================================================
# 成员
# ============================================================


@router.get(
    "/{ws_id}/members",
    response_model=list[MemberOut],
    summary="成员列表",
)
async def list_members(
    ws_id: int, session: DbSession, ctx: CurrentUser
) -> list[MemberOut]:
    await assert_workspace_access(session, ctx, ws_id)
    rows = (
        await session.execute(
            select(WorkspaceMember, User)
            .join(User, User.id == WorkspaceMember.user_id)
            .where(WorkspaceMember.workspace_id == ws_id)
            .order_by(WorkspaceMember.joined_at)
        )
    ).all()
    return [
        MemberOut(
            user_id=m.user_id,
            email=u.email,
            display_name=u.display_name,
            role=m.role,
            joined_at=m.joined_at,
        )
        for m, u in rows
    ]


@router.post(
    "/{ws_id}/members",
    response_model=MemberOut,
    status_code=status.HTTP_201_CREATED,
    summary="邀请成员",
)
async def add_member(
    ws_id: int, req: AddMemberReq, session: DbSession, ctx: CurrentUser
) -> MemberOut:
    ws = await assert_workspace_admin(session, ctx, ws_id)

    user = (
        await session.execute(select(User).where(User.email == req.email))
    ).scalar_one_or_none()
    if user is None:
        # 邀请未注册用户需要"待接受邀请"表，当前没做（D4 暂不做完整 RBAC）。
        # 明说做不到，别让调用方以为发出去了。
        raise InvalidRequest(
            f"用户 {req.email} 尚未注册，无法直接加入工作区"
        )

    if req.role == "owner":
        # ⚠️ 不能把 owner 转让给第二个用户：那会让"谁是 owner"变得不明确，
        #   而 owner 是删工作区/管成员的唯一权限来源。
        #   要转让请先 PATCH 改 owner（该端点在后续 RBAC 阶段做）。
        raise InvalidRequest("暂不支持授予 owner 角色")

    existing = (
        await session.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == ws_id,
                WorkspaceMember.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise InvalidRequest(f"{req.email} 已经是工作区成员")

    m = WorkspaceMember(
        workspace_id=ws_id,
        user_id=user.id,
        role=req.role,
        invited_by=ctx.user_id,
    )
    session.add(m)
    await session.commit()
    return MemberOut(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=m.role,
        joined_at=m.joined_at,
    )


@router.delete(
    "/{ws_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="移除成员",
)
async def remove_member(
    ws_id: int, user_id: int, session: DbSession, ctx: CurrentUser
) -> None:
    """移除成员。

    owner 不能被移除 —— 否则工作区变成没人能管的孤儿
    （`assert_workspace_admin` 会把所有人拒之门外，且没法自救）。
    """
    ws = await assert_workspace_admin(session, ctx, ws_id)
    if user_id == ws.owner_id:
        raise InvalidRequest("不能移除工作区 owner")

    row = (
        await session.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == ws_id,
                WorkspaceMember.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise ResourceNotFound(detail={"user_id": user_id})
    await session.delete(row)
    await session.commit()
