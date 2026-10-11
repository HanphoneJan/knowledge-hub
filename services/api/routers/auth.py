"""认证端点（D3：每用户 API Key，MCP/A2A 共用）。

契约见 `docs/DESIGN-API.md` 第 1 节。

## ⚠️ 一个必须知道的取舍：`login` 会**创建新 key**

契约写的是 `POST /auth/login` 返回 `api_key`。但我们**只存 key 的哈希**
（D3 明确"明文 key 只在创建时返回一次"），所以 login 时**不可能返回任何一把
已有 key 的明文** —— 那把 key 的明文已经丢了。

因此 login 的语义只能是：**校验密码 → 现铸一把新 key**。

代价是每次登录在 `api_keys` 表插一行。处理方式：

- 这类 key 统一命名 `web-login`，与用户自建的 key（`Cursor`、`Claude`）区分
- 用户可以随时 `DELETE /auth/keys/{id}` 吊销
- 运维端点 `POST /admin/prune-keys` 清理长期未使用的

**为什么不给浏览器发 JWT 会话**：那会让"浏览器会话"与"API Key"两套凭证并存，
MCP/A2A 侧的鉴权逻辑要写两遍。D3 定的"一把 key 通吃"更简单。
代价是前端要自己存 key（存在 localStorage），这是当前设计的取舍，
README 里要写明"key 等同密码，不要在共享机器上登录"。
"""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, Request, status
from sqlalchemy import func, or_, select

from ...common.auth import (
    UserContext,
    generate_api_key,
    hash_password,
    verify_password,
)
from ...common.config import get_settings
from ...common.deps import CurrentUser, DbSession
from ..rate_limit import (  # noqa: E402 — 同包路由
    LOCAL_REGISTER_LIMIT,
    REGISTER_LIMIT,
    REGISTER_WINDOW_SECONDS,
    check,
    client_key,
    login_fail_budget,
    record_login_fail,
)
from ...common.errors import (
    InvalidRequest,
    RateLimited,
    RegistrationClosed,
    ResourceNotFound,
)
from ...common.models import ApiKey, User, Workspace, WorkspaceMember
from ...common.schemas import (
    ApiKeyOut,
    CreateKeyReq,
    CreateKeyResp,
    LoginReq,
    LoginResp,
    RegisterReq,
    UpdateMeReq,
    UserOut,
)

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)

#: login 自动创建的 key 用这个名字，便于与用户自建的区分
LOGIN_KEY_NAME = "web-login"

#: 登录时默认给的作用域。D4 暂不做细粒度 RBAC，读权限是唯一必需的。
DEFAULT_SCOPES = ["kb:read", "chat:read"]


def _issue_key(session, user_id: int, name: str, scopes: list[str] | None = None) -> tuple[str, str]:
    """给用户发一把新 key。返回 `(明文, 前缀)`。

    明文只在这里出现一次，之后库里只有 `key_hash`。
    """
    plaintext, key_hash, prefix = generate_api_key()
    session.add(
        ApiKey(
            user_id=user_id,
            name=name,
            key_hash=key_hash,
            key_prefix=prefix,
            scopes=scopes if scopes is not None else DEFAULT_SCOPES,
        )
    )
    return plaintext, prefix


@router.post(
    "/register",
    response_model=LoginResp,
    status_code=status.HTTP_201_CREATED,
    summary="注册账号（自动建工作区 + 第一把 API Key）",
)
async def register(
    req: RegisterReq, session: DbSession, request: Request
) -> LoginResp:
    """注册。

    刻意**同时**建一个个人工作区并把用户设为 owner ——
    否则新用户注册后 `GET /workspaces` 返回空列表，
    前端进首页就看到一个空白页，连"建第一个 KB"都做不了
    （`POST /kb` 必填 `workspace_id`）。

    🔴 **限流**（D135）：这个端点**不需要 API Key**，而部署通常对公网可达
    （实测无凭证 `POST /auth/register` 能走到业务校验），所以它是
    "往库里塞垃圾账号"最省事的入口。桶键见 `rate_limit.client_key`
    （Cloudflare Tunnel 下真实 IP 在 `CF-Connecting-IP` 里）。
    """
    # 🔑 桶键先算：限流与"要不要给本机留后门"都用它
    bucket = client_key(request.client.host if request.client else None, request.headers)

    # 🔴 自助注册开关（D138）。**先于**限流：关了就是关了，
    #   不该在"限流配额"上跟人商量（限流是 429，关闭是 403，语义不同）。
    #   ⚠️ `local` 例外：只对**本机**放行，因为三个 eval 脚本与三套 e2e
    #   都要自助注册（它们从 loopback 来且没有转发头 ⇒ `client_key` 给出 "local"）。
    #   而公网路径**永远不是** `local`：隧道进来的请求带 `CF-Connecting-IP`
    #   ⇒ 键是 `ip:…`；即使哪天直连开端口，对端是公网地址 ⇒ 键也是 `ip:…`。
    #   ⇒ 「关掉自助注册」对公网是真的关掉了，本机脚本不受影响（实测见 eval §20）。
    if not get_settings().allow_register and bucket != "local":
        raise RegistrationClosed()

    # 🔑 限流**先于**任何业务判断 —— 连"该邮箱已注册"都不该在超限时泄露
    #   （那是一个资源探测信号：限流外的请求能问出"这个邮箱有没有账号"）。
    limit = LOCAL_REGISTER_LIMIT if bucket == "local" else REGISTER_LIMIT
    ok, retry_after = check(limit, REGISTER_WINDOW_SECONDS, bucket)
    if not ok:
        raise RateLimited(
            f"注册过于频繁，请 {retry_after // 60 or 1} 分钟后再试",
            headers={"Retry-After": str(retry_after)},
        )

    exists = (
        await session.execute(select(User.id).where(User.email == req.email))
    ).scalar_one_or_none()
    if exists is not None:
        # 不复用 INVALID_REQUEST —— 明确告诉调用方"邮箱已注册"，
        # 这是注册流程的正常反馈，不是资源探测（未登录用户拿不到任何数据）
        raise InvalidRequest("该邮箱已注册")

    # 🔑 D174：昵称唯一（DB 有唯一约束，这里先查一次给一句人话）。
    if req.nickname:
        nick_taken = (
            await session.execute(
                select(User.id).where(User.nickname == req.nickname)
            )
        ).scalar_one_or_none()
        if nick_taken is not None:
            raise InvalidRequest("该昵称已被占用")

    user = User(
        email=req.email,
        password_hash=hash_password(req.password),
        display_name=req.display_name,
        nickname=req.nickname or None,
    )
    session.add(user)
    await session.flush()

    ws = Workspace(
        name=f"{req.display_name} 的工作区",
        description="默认工作区",
        owner_id=user.id,
    )
    session.add(ws)
    await session.flush()
    session.add(
        WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="owner")
    )

    plaintext, prefix = _issue_key(session, user.id, "default")
    await session.commit()

    return LoginResp(
        user=UserOut.model_validate(user),
        api_key=plaintext,
        key_prefix=prefix,
    )


@router.post("/login", response_model=LoginResp, summary="登录（换一把新 key）")
async def login(
    req: LoginReq, session: DbSession, request: Request
) -> LoginResp:
    """校验密码并**新铸一把** key。

    见模块 docstring：库里只存哈希，返回不了旧 key 的明文。

    🔴 **失败限流**（D138）：这是全站唯一通往你数据的入口，而它**不需要
    任何凭证**。此前 `grep -rn RateLimited services/` 只命中注册端点 ——
    也就是说"注册限流做了、登录没做"，而后者才是真正值得防的那个。
    🔑 **只数失败**：成功登录不记录（否则主人会被自己的正常用法锁在门外，
    而它挡不住成功登录）。IP 桶 + **账号桶**都要有配额 ——
    账号桶才是"保护某个具体用户的密码"真正起作用的那个（换 IP 无效）。
    """
    bucket = client_key(request.client.host if request.client else None, request.headers)
    ok, retry_after, which = login_fail_budget(bucket, req.email)
    if not ok:
        # 🔴 不说"是哪个桶"：泄露"这个账号正在被人尝试"本身就是信息。
        raise RateLimited(
            f"登录尝试过于频繁，请 {retry_after // 60 or 1} 分钟后再试",
            headers={"Retry-After": str(retry_after)},
        )

    row = (
        await session.execute(
            select(User, User.password_hash).where(
                # 🔑 D174：邮箱**或**昵称都能登录。
                or_(User.email == req.email, User.nickname == req.email)
            )
        )
    ).first()

    # ⚠️ 用户不存在与密码错误必须返回**同一个**错误，
    # 否则可以通过"邮箱是否存在"枚举账号。
    if row is None or not verify_password(row[1], req.password):
        record_login_fail(bucket, req.email)
        raise InvalidRequest("账号或密码不正确")

    user = row[0]
    if not user.is_active:
        raise InvalidRequest("账号已停用")

    plaintext, prefix = _issue_key(session, user.id, LOGIN_KEY_NAME)
    await session.commit()

    return LoginResp(
        user=UserOut.model_validate(user),
        api_key=plaintext,
        key_prefix=prefix,
    )


@router.get("/me", response_model=UserOut, summary="当前用户")
async def me(ctx: CurrentUser) -> UserOut:
    return UserOut(
        id=ctx.user_id,
        email=ctx.email,
        display_name=ctx.display_name,
        nickname=ctx.nickname,
        role="superadmin" if ctx.is_superadmin else "user",
        created_at=dt.datetime.now(dt.UTC),
    )


@router.patch("/me", response_model=UserOut, summary="改自己的资料（D175）")
async def update_me(
    req: UpdateMeReq, session: DbSession, ctx: CurrentUser
) -> UserOut:
    """任何登录用户改**自己**的资料（显示名 / 昵称 / 邮箱 / 密码）。

    🔑 改 `email` 或 `new_password` **必须**带 `current_password` 并校验通过 ——
    否则一次 XSS 或借用的设备就能把账号锁走。改昵称/显示名不需要。
    """
    user = await session.get(User, ctx.user_id)
    if user is None:
        raise ResourceNotFound(detail={"user_id": ctx.user_id})

    changing_email = req.email is not None and req.email != user.email
    if (changing_email or req.new_password):
        if not req.current_password or not verify_password(
            user.password_hash, req.current_password
        ):
            raise InvalidRequest("当前密码不正确")

    if req.display_name is not None:
        user.display_name = req.display_name
    if req.clear_nickname:
        user.nickname = None
    elif req.nickname is not None:
        taken = (
            await session.execute(
                select(User.id).where(
                    User.nickname == req.nickname, User.id != user.id
                )
            )
        ).scalar_one_or_none()
        if taken is not None:
            raise InvalidRequest("该昵称已被占用")
        user.nickname = req.nickname
    if changing_email:
        taken = (
            await session.execute(
                select(User.id).where(User.email == req.email, User.id != user.id)
            )
        ).scalar_one_or_none()
        if taken is not None:
            raise InvalidRequest("该邮箱已注册")
        user.email = req.email
    if req.new_password:
        user.password_hash = hash_password(req.new_password)

    await session.commit()
    logger.info("用户 %s 改了资料：email=%s nickname=%s", user.id, user.email, user.nickname)
    return UserOut.model_validate(user)


@router.get("/keys", response_model=list[ApiKeyOut], summary="列出我的 API Key")
async def list_keys(session: DbSession, ctx: CurrentUser) -> list[ApiKeyOut]:
    rows = (
        await session.execute(
            select(ApiKey)
            .where(ApiKey.user_id == ctx.user_id, ApiKey.revoked_at.is_(None))
            .order_by(ApiKey.created_at.desc())
        )
    ).scalars().all()
    return [ApiKeyOut.model_validate(r) for r in rows]


@router.post(
    "/keys",
    response_model=CreateKeyResp,
    status_code=status.HTTP_201_CREATED,
    summary="新建一把 API Key（明文只返回这一次）",
)
async def create_key(
    req: CreateKeyReq, session: DbSession, ctx: CurrentUser
) -> CreateKeyResp:
    """新建 key 并返回明文。

    用专门的 `CreateKeyResp`（而不是复用 `ApiKeyOut` + 响应头）——
    契约写的是"返回明文一次"，明文是**这次响应的一部分**，
    藏在 header 里会让调用方漏读，然后困惑"key 到底去哪了"。
    """
    plaintext, key_hash, prefix = generate_api_key()
    row = ApiKey(
        user_id=ctx.user_id,
        name=req.name,
        key_hash=key_hash,
        key_prefix=prefix,
        scopes=req.scopes if req.scopes is not None else DEFAULT_SCOPES,
    )
    session.add(row)
    await session.commit()

    return CreateKeyResp(
        key=ApiKeyOut.model_validate(row), api_key=plaintext
    )


@router.delete("/keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT, summary="吊销一把 Key")
async def revoke_key(key_id: int, session: DbSession, ctx: CurrentUser) -> None:
    row = (
        await session.execute(
            select(ApiKey).where(
                ApiKey.id == key_id,
                ApiKey.user_id == ctx.user_id,
                ApiKey.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise ResourceNotFound(detail={"key_id": key_id})
    row.revoked_at = dt.datetime.now(dt.UTC)
    await session.commit()
