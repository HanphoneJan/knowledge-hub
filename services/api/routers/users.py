"""超管：用户管理（D173 · 契约 §12）。

只有 `role='superadmin'` 的用户能调这些端点。能力：列全部用户、直接建号
（自助注册关掉时的 UI 入口）、改角色 / 停用 / 改昵称显示名。

## 两条防自锁的守卫（别删）

1. **不能把最后一个超级管理员降级/停用** —— 否则实例再没有任何人能管用户，
   只能手写 SQL 救回来。
2. **不能停用自己** —— 与"吊销当前登录 key"同一个道理（D64 已在 key 那边挡了）。

⚠️ 角色与 API Key 一样是**服务端推导**的（`load_user_context` 从用户行读），
请求体里不接受 `user_id`/`role` 之外的越权字段 —— 这里的 `role` 是"给谁设成什么"，
不是"我是谁"。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, status
from sqlalchemy import func, select

from ...common.auth import assert_superadmin, hash_password
from ...common.deps import CurrentUser, DbSession
from ...common.errors import InvalidRequest, ResourceNotFound
from ...common.models import User, Workspace, WorkspaceMember
from ...common.schemas import AdminUserOut, CreateUserReq, UpdateUserReq

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/users", tags=["admin"])

_SUPERADMIN = "superadmin"


async def _active_superadmin_count(session) -> int:  # noqa: ANN001
    return (
        await session.execute(
            select(func.count(User.id)).where(
                User.role == _SUPERADMIN, User.is_active.is_(True)
            )
        )
    ).scalar_one()


@router.get("", response_model=list[AdminUserOut], summary="列出全部用户（超管）")
async def list_users(ctx: CurrentUser, session: DbSession) -> list[AdminUserOut]:
    assert_superadmin(ctx)
    rows = (
        await session.execute(select(User).order_by(User.id))
    ).scalars().all()
    return [AdminUserOut.model_validate(r) for r in rows]


@router.post(
    "",
    response_model=AdminUserOut,
    status_code=status.HTTP_201_CREATED,
    summary="建号（超管；自助注册关闭时的入口）",
)
async def create_user(
    req: CreateUserReq, ctx: CurrentUser, session: DbSession
) -> AdminUserOut:
    assert_superadmin(ctx)

    if (
        await session.execute(select(User.id).where(User.email == req.email))
    ).scalar_one_or_none() is not None:
        raise InvalidRequest("该邮箱已注册")
    if req.nickname and (
        await session.execute(select(User.id).where(User.nickname == req.nickname))
    ).scalar_one_or_none() is not None:
        raise InvalidRequest("该昵称已被占用")

    user = User(
        email=req.email,
        password_hash=hash_password(req.password),
        display_name=req.display_name,
        nickname=req.nickname or None,
        role=req.role,
    )
    session.add(user)
    await session.flush()

    # 与 register / create_user 脚本一致：同时建一个个人工作区并设为 owner。
    ws = Workspace(
        name=f"{user.display_name} 的工作区", description=None, owner_id=user.id
    )
    session.add(ws)
    await session.flush()
    session.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="owner"))
    await session.commit()

    logger.info(
        "超管 %s 建号：id=%s email=%s role=%s",
        ctx.user_id, user.id, user.email, user.role,
    )
    return AdminUserOut.model_validate(user)


@router.patch(
    "/{user_id}", response_model=AdminUserOut, summary="改用户角色/状态/昵称（超管）"
)
async def update_user(
    user_id: int, req: UpdateUserReq, ctx: CurrentUser, session: DbSession
) -> AdminUserOut:
    assert_superadmin(ctx)

    user = await session.get(User, user_id)
    if user is None:
        raise ResourceNotFound(detail={"user_id": user_id})

    # ---- 防自锁守卫（见模块 docstring）----
    would_drop_super = (
        user.role == _SUPERADMIN and user.is_active
        and (req.role == "user" or req.is_active is False)
    )
    if would_drop_super and await _active_superadmin_count(session) <= 1:
        raise InvalidRequest("不能降级或停用最后一个超级管理员")
    if user.id == ctx.user_id and req.is_active is False:
        raise InvalidRequest("不能停用自己")

    if req.display_name is not None:
        user.display_name = req.display_name
    if req.clear_nickname:
        user.nickname = None
    elif req.nickname is not None:
        taken = (
            await session.execute(
                select(User.id).where(
                    User.nickname == req.nickname, User.id != user_id
                )
            )
        ).scalar_one_or_none()
        if taken is not None:
            raise InvalidRequest("该昵称已被占用")
        user.nickname = req.nickname
    # 🔑 D175：超管可改邮箱 / 重置密码（"修改任意用户信息"的一部分）。
    if req.email is not None and req.email != user.email:
        taken = (
            await session.execute(
                select(User.id).where(User.email == req.email, User.id != user_id)
            )
        ).scalar_one_or_none()
        if taken is not None:
            raise InvalidRequest("该邮箱已注册")
        user.email = req.email
    if req.new_password:
        user.password_hash = hash_password(req.new_password)
    if req.role is not None:
        user.role = req.role
    if req.is_active is not None:
        user.is_active = req.is_active

    await session.commit()
    logger.info(
        "超管 %s 改用户 %s：role=%s active=%s",
        ctx.user_id, user.id, user.role, user.is_active,
    )
    return AdminUserOut.model_validate(user)
