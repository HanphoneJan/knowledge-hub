"""FastAPI 依赖：认证、DB 会话、授权断言。

所有路由统一从这里拿上下文，**不要**在路由里自己解析 Authorization 头 ——
那是漏加鉴权的高发区。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import UserContext, assert_kb_access, load_user_context
from .db import get_async_sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """请求级 DB 会话（一个事务边界）。"""
    factory = get_async_sessionmaker()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


DbSession = Annotated[AsyncSession, Depends(get_session)]


async def get_current_user(
    request: Request,
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> UserContext:
    """校验 Bearer Token 并推导权限范围。

    注意：**请求体里不接受任何租户字段**，一切从登录态推导（D8）。
    """
    if not authorization:
        from .errors import InvalidToken

        raise InvalidToken("缺少 Authorization 头")

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        from .errors import InvalidToken

        raise InvalidToken("Authorization 格式应为 'Bearer <api_key>'")

    return await load_user_context(session, token.strip())


CurrentUser = Annotated[UserContext, Depends(get_current_user)]


def request_id(request: Request) -> str:
    """给错误响应打 trace id。优先用上游传来的（网关/前端）。"""
    return (
        request.headers.get("x-request-id")
        or getattr(request.state, "request_id", "")
        or "req_local"
    )


async def ensure_kb_access(
    session: AsyncSession, ctx: UserContext, kb_id: int
) -> None:
    """FastAPI 依赖形式的资源断言。"""
    await assert_kb_access(session, ctx, kb_id)


__all__ = [
    "CurrentUser",
    "DbSession",
    "UserContext",
    "ensure_kb_access",
    "get_current_user",
    "get_session",
    "request_id",
]
