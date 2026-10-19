"""审计日志（M8 / D8）。

**审计三元组**：api_key → user → kb_ids。每次 MCP / A2A / API 调用记一条。

为什么不能只在应用层记：worker 也会触碰数据（解析、索引），
将来若有后台任务也要能追责。这里提供两种写入方式：
- `write_audit` 异步 —— API 请求路径用，不阻塞响应
- `write_audit_sync` 同步 —— worker 用（本来就是同步进程）
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from .models import AuditLog

# 常用 action 常量，避免各处拼错字符串
ACTION_SEARCH = "search"
ACTION_ASK = "ask"
ACTION_MCP_CALL = "mcp_call"
ACTION_A2A_CALL = "a2a_call"
ACTION_UPLOAD = "upload"
ACTION_DELETE = "delete"


def build_record(
    *,
    action: str,
    user_id: int | None,
    api_key_id: int | None,
    kb_ids: list[int],
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "action": action,
        "user_id": user_id,
        "api_key_id": api_key_id,
        "kb_ids": kb_ids or [],
        "detail": detail,
        "created_at": dt.datetime.now(dt.UTC),
    }


async def write_audit(
    session: AsyncSession,
    *,
    action: str,
    user_id: int | None,
    api_key_id: int | None,
    kb_ids: list[int],
    detail: dict[str, Any] | None = None,
) -> None:
    """API 请求路径写入。

    **不单独开事务**：跟业务操作用同一个 session，业务回滚时审计也回滚 ——
    这是想要的语义（审计"发生过的事"，不该记录没提交的操作）。
    """
    session.add(AuditLog(**build_record(
        action=action, user_id=user_id, api_key_id=api_key_id,
        kb_ids=kb_ids, detail=detail,
    )))


def write_audit_sync(
    session: Session,
    *,
    action: str,
    user_id: int | None = None,
    api_key_id: int | None = None,
    kb_ids: list[int] | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """worker 路径写入（同步）。"""
    session.add(AuditLog(**build_record(
        action=action, user_id=user_id, api_key_id=api_key_id,
        kb_ids=kb_ids or [], detail=detail,
    )))


async def purge_old_audit(session: AsyncSession, keep_days: int = 180) -> int:
    """清理旧审计。审计表会无限增长（每次检索一条），需要定期删。

    保留 180 天是法律与排查的常见折中：D23 团队 5~20 人，
    每天几百次调用，180 天也就几万行。
    """
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=keep_days)
    res = await session.execute(
        delete(AuditLog).where(AuditLog.created_at < cutoff)
    )
    return res.rowcount or 0


async def count_audit(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(AuditLog))).scalar_one()



