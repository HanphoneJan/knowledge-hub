"""工作区邀请码（D4 · M1）：生成 / 列表 / 撤销 / 兑换。

## 为什么要有它（这是 M1 唯一缺的端点）

`POST /workspaces/{id}/members` 只能加**已注册**的邮箱，
而 D4 明确**不接邮件服务** —— 于是"邀请一个还没注册的人"这条路本来就不通
（端点里那句"邀请未注册用户需要待接受邀请表，当前没做"就是当时的记录）。

邀请码把它变成**自服务**：owner 生成一段码 → 对方注册后自己输码加入。

## 四个端点

| 端点 | 谁能调 | 说明 |
|---|---|---|
| `POST /workspaces/{id}/invites` | owner/admin | 生成，**明文只返回这一次**（D64 同一纪律） |
| `GET /workspaces/{id}/invites` | owner/admin | 列表（只有前缀 + 用了几次） |
| `DELETE /workspaces/{id}/invites/{id}` | owner/admin | 撤销（还没用掉的） |
| `POST /invites/redeem` | 登录用户 | 兑换 → 成为成员 |

## 🔑 兑换是"拿着码就能进"，所以它本身是凭据

兑换端点**不要求调用方是任何工作区的成员** —— 他就是凭这段码进来的。
所以：码的熵、过期、限次、撤销、只存哈希，全部是安全边界。
`invites.py`（纯逻辑）里那些判据不是"顺手加的校验"，是这条边界的全部。

⚠️ 兑换失败**统一一句话**（不存在/过期/用完/撤销都是同一句）：
区分开等于给攻击者一个"这个码存在但失效了"的探测信号。
"""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ...common.auth import (
    assert_workspace_access,
    assert_workspace_admin,
    can_admin_workspace,
)
from ...common.deps import CurrentUser, DbSession
from ...common.errors import InvalidRequest, ResourceNotFound
from ...common.invites import (
    CODE_LEN,
    REDEEM_FAIL,
    RedeemError,
    code_prefix,
    expires_at as invite_expires_at,
    expiry_text,
    generate_code,
    hash_code,
    normalize_code,
    validate_max_uses,
    validate_ttl_days,
    verify_redeem,
)
from ...common.models import Workspace, WorkspaceInvite, WorkspaceMember
from ...common.schemas import (
    CreateInviteReq,
    CreateInviteResp,
    InviteOut,
    RedeemInviteReq,
    RedeemInviteResp,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["invites"])


@router.post(
    "/workspaces/{ws_id}/invites",
    response_model=CreateInviteResp,
    status_code=201,
    summary="生成邀请码（明文只返回这一次）",
)
async def create_invite(
    ws_id: int, req: CreateInviteReq, session: DbSession, ctx: CurrentUser
) -> CreateInviteResp:
    """生成一段邀请码。

    🔑 **明文只出现在这一个响应里**（与 API Key 同一纪律，D64）：
    库里存 HMAC 哈希，列表页只看得到前 4 位。丢了就重新生成一段。
    """
    await assert_workspace_admin(session, ctx, ws_id)
    ttl = validate_ttl_days(req.ttl_days)
    uses = validate_max_uses(req.max_uses)

    now = dt.datetime.now(dt.UTC)
    # 最多试 5 次：极小概率撞上同前缀（同一个工作区里前缀必须唯一）
    for _ in range(5):
        code = generate_code()
        prefix = code_prefix(code)
        row = WorkspaceInvite(
            workspace_id=ws_id,
            code_hash=hash_code(code),
            code_prefix=prefix,
            role="member",          # 🔴 恒为 member：邀请码不给 owner
            max_uses=uses,
            created_by=ctx.user_id,
            created_at=now,
            expires_at=invite_expires_at(now, ttl),
        )
        session.add(row)
        try:
            await session.flush()
        except IntegrityError:
            # 撞前缀 → 换一段再试（不重试的话用户会看到
            # 一个和"并发冲突"完全一样的 500）
            await session.rollback()
            continue
        return CreateInviteResp(
            invite=InviteOut(
                id=row.id,
                workspace_id=ws_id,
                code_prefix=row.code_prefix,
                role=row.role,
                max_uses=row.max_uses,
                used=row.used,
                created_at=row.created_at,
                expires_at=row.expires_at,
                revoked_at=row.revoked_at,
                expiry_text=expiry_text(row.expires_at, now),
                state=_state_text(row, now),
            ),
            code=code,
            notice=(
                f"这段码**只显示这一次**（库里只存哈希）。\n"
                f"默认 {ttl} 天内有效、可用 {uses} 次。\n"
                f"把它发给对方（微信/邮件/当面都行），对方注册后自己输码加入。"
            ),
        )
    # ⚠️ 400 而不是 409：同上面的理由（错误词汇里没有 Conflict）
    raise InvalidRequest("生成邀请码失败（连续 5 次撞上重复前缀），请重试")


@router.get(
    "/workspaces/{ws_id}/invites",
    response_model=list[InviteOut],
    summary="邀请码列表（只有前缀，没有明文）",
)
async def list_invites(
    ws_id: int, session: DbSession, ctx: CurrentUser
) -> list[InviteOut]:
    await assert_workspace_admin(session, ctx, ws_id)
    rows = (
        await session.execute(
            select(WorkspaceInvite)
            .where(WorkspaceInvite.workspace_id == ws_id)
            .order_by(WorkspaceInvite.created_at.desc())
        )
    ).scalars().all()
    now = dt.datetime.now(dt.UTC)
    return [
        InviteOut(
            id=r.id,
            workspace_id=ws_id,
            code_prefix=r.code_prefix,
            role=r.role,
            max_uses=r.max_uses,
            used=r.used,
            created_at=r.created_at,
            expires_at=r.expires_at,
            revoked_at=r.revoked_at,
            expiry_text=expiry_text(r.expires_at, now),
            state=_state_text(r, now),
        )
        for r in rows
    ]


@router.delete(
    "/workspaces/{ws_id}/invites/{invite_id}",
    status_code=204,
    summary="撤销邀请码",
)
async def revoke_invite(
    ws_id: int, invite_id: int, session: DbSession, ctx: CurrentUser
) -> None:
    await assert_workspace_admin(session, ctx, ws_id)
    row = (
        await session.execute(
            select(WorkspaceInvite).where(
                WorkspaceInvite.id == invite_id,
                WorkspaceInvite.workspace_id == ws_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        # 🔴 与"工作区不可见"同码（fail-closed，§0.4）：不能靠错误码
        #   区分"这个工作区有邀请码但 id 不是这个"。
        raise ResourceNotFound(detail={"invite_id": invite_id})
    if row.revoked_at is not None:
        return                      # 幂等：重复撤销不报错
    row.revoked_at = dt.datetime.now(dt.UTC)
    await session.commit()


@router.post(
    "/invites/redeem",
    response_model=RedeemInviteResp,
    summary="兑换邀请码（加入工作区）",
)
async def redeem_invite(
    req: RedeemInviteReq, session: DbSession, ctx: CurrentUser
) -> RedeemInviteResp:
    """拿码加入工作区。

    🔑 **兑换失败统一一句话**（`invites.REDEEM_FAIL`）：不存在 / 过期 /
    用完 / 被撤销 —— 区分开等于给攻击者一个"这个码存在但失效了"的探测信号。
    而"兑换"本身是凭据入口，所以这段码的熵/过期/限次就是安全边界。
    """
    code = normalize_code(req.code)
    now = dt.datetime.now(dt.UTC)

    # 先按前缀定位（哈希带盐，不能按哈希查；前缀只有 4 位所以要全表比）
    # ⚠️ 候选通常只有 1~几条（一个工作区最多几十个码），
    #   但为了不依赖"前缀一定唯一"（虽然有唯一约束），仍然逐个 verify。
    candidates = (
        await session.execute(
            select(WorkspaceInvite).where(
                WorkspaceInvite.code_prefix == code[:4]
            )
        )
    ).scalars().all()

    # 🔑 **先验哈希、再看"是不是已经是成员"、最后才验状态**。
    #   我第一版把状态校验放在前面，于是**重复兑换拿到的是
    #   "邀请码无效"** —— 而那恰恰是最常见的情况（用户不记得自己已经加过了）。
    #   症状是"我明明用成功过，怎么码坏了"，比任何技术细节都让人困惑。
    #   ⚠️ 这么排**不泄露任何东西**：哈希验证已经证明"他拿着正确的码"，
    #   告诉他"你已经是成员"只是复述一个他自己就知道的事实。
    hashed = [c for c in candidates if _hash_ok(c.code_hash, code)]
    if not hashed:
        raise InvalidRequest(REDEEM_FAIL)

    for target in hashed:
        ws = await _live_workspace(session, target.workspace_id)
        if ws is None:
            continue                    # 工作区已软删 → 当这个码不存在
        already = (
            await session.execute(
                select(WorkspaceMember).where(
                    WorkspaceMember.workspace_id == ws.id,
                    WorkspaceMember.user_id == ctx.user_id,
                )
            )
        ).scalar_one_or_none()
        if already is not None:
            return RedeemInviteResp(
                workspace_id=ws.id,
                workspace_name=ws.name,
                role=already.role,
                already_member=True,
                message=f"你已经是「{ws.name}」的成员了（无需重复兑换）",
            )
        try:
            verify_redeem(
                stored_hash=target.code_hash, code=code, used=target.used,
                max_uses=target.max_uses, revoked=target.revoked_at is not None,
                expires_at_=target.expires_at, now=now,
            )
        except RedeemError:
            continue                    # 不区分原因（同上）
        break
    else:
        raise InvalidRequest(REDEEM_FAIL)

    session.add(
        WorkspaceMember(
            workspace_id=ws.id, user_id=ctx.user_id, role=target.role
        )
    )
    target.used += 1
    target.redeemed_by = ctx.user_id
    target.redeemed_at = now
    try:
        await session.commit()
    except IntegrityError as exc:
        # 🔑 并发兑换（两次请求都读到 used=0）：DB 的 `used <= max_uses` 兜住了，
        #   这里把它翻译成用户看得懂的话，而不是 500。
        #   ⚠️ 用 **400**（`InvalidRequest`）而不是 409：本项目的错误词汇里
        #   没有 Conflict（`errors.py` 只有 8 个类），为这一句话新增一个错误码
        #   要动契约 + `contract_check`，收益不抵成本。而"已经被别人用掉"
        #   对用户来说就是"这个码不行了，重新要一段"，400 足够。
        await session.rollback()
        logger.warning("并发兑换同一个邀请码被 DB 约束拦住：%s", exc)
        raise InvalidRequest("这个邀请码刚刚被别人用掉了，请让对方要一段新的") from exc

    return RedeemInviteResp(
        workspace_id=ws.id,
        workspace_name=ws.name,
        role=target.role,
        already_member=False,
        message=f"已加入「{ws.name}」",
    )


async def _live_workspace(session, ws_id: int) -> Workspace | None:
    """工作区存在且**没被软删**（D42）。软删的当不存在 —— 
    不然拿着旧码能把人拉进一个正在回收站里的工作区。"""
    return (
        await session.execute(
            select(Workspace).where(
                Workspace.id == ws_id, Workspace.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()


def _hash_ok(stored: str, code: str) -> bool:
    """哈希比对（`invites.verify_hash` 的薄包装，便于断点/单测）。"""
    from ...common.invites import verify_hash

    return verify_hash(code, stored)


def _state_text(row: WorkspaceInvite, now: dt.datetime) -> str:
    """邀请码当前状态（**列表**用，可以区分；兑换时不区分）。"""
    if row.revoked_at is not None:
        return "已撤销"
    if row.used >= row.max_uses:
        return "已用完"
    if row.expires_at is not None and row.expires_at <= now:
        return "已过期"
    return "可用"


__all__ = ["router", "CODE_LEN", "can_admin_workspace", "assert_workspace_access"]