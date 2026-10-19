"""数据库连接管理。

两套引擎，共享同一份模型：
- **异步**（asyncpg）→ API 进程
- **同步**（psycopg）→ worker 进程与 alembic

为什么分开：worker 要跑 colSmol 这类吃满 12 核的同步计算（实测 15s/图），
塞进 asyncio 事件循环会把整个 worker 卡死。API 全是 IO，用 async 更合适。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings

_settings = get_settings()

# ---------- 异步引擎（API）----------

_async_engine: AsyncEngine | None = None
_async_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_async_engine() -> AsyncEngine:
    global _async_engine
    if _async_engine is None:
        _async_engine = create_async_engine(
            _settings.pg_async_dsn,
            pool_size=_settings.pg_pool_size,
            max_overflow=_settings.pg_max_overflow,
            pool_pre_ping=True,
            echo=_settings.log_level.upper() == "DEBUG",
        )
    return _async_engine


def get_async_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _async_sessionmaker
    if _async_sessionmaker is None:
        _async_sessionmaker = async_sessionmaker(
            get_async_engine(), expire_on_commit=False
        )
    return _async_sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """异步事务边界。

    ⚠️ 装饰器必须是 **`asynccontextmanager`** 而不是 `contextmanager`。
    这是阶段 0 写错、1d 写集成测试时才暴露的一个 bug：
    `@contextmanager` 包一个 async 生成器，产出的是
    `_GeneratorContextManager`，它只支持 `with`，不支持 `async with` ——
    于是 `async with session_scope() as s:` 直接抛
    `TypeError: '_GeneratorContextManager' object does not support the
    asynchronous context manager protocol`。

    为什么之前没人踩到：FastAPI 依赖是**生成器依赖**，
    `deps.py::get_session` 自己写了一遍 `yield`，根本没调这个函数。
    全项目第一个真正的调用方是 1d 的 SSE 落库 ——
    也就是"只有在流式路径上才会用到、且只在新代码里用到"的那种坑。
    """
    factory = get_async_sessionmaker()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ---------- 同步引擎（worker / alembic）----------

_sync_engine: Engine | None = None
_sync_sessionmaker: sessionmaker[Session] | None = None


def get_sync_engine() -> Engine:
    global _sync_engine
    if _sync_engine is None:
        _sync_engine = create_engine(
            _settings.pg_dsn,
            pool_size=3,
            max_overflow=3,
            pool_pre_ping=True,
        )
    return _sync_engine


def get_sync_sessionmaker() -> sessionmaker[Session]:
    global _sync_sessionmaker
    if _sync_sessionmaker is None:
        _sync_sessionmaker = sessionmaker(
            get_sync_engine(), expire_on_commit=False
        )
    return _sync_sessionmaker


@contextmanager
def sync_session_scope() -> Iterator[Session]:
    factory = get_sync_sessionmaker()
    with factory() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


# ---------- 健康检查 ----------


async def check_db() -> dict[str, Any]:
    """返回给 `/health` 的 DB 状态。故意不抛异常 —— 健康检查要能看到故障本身。"""
    try:
        engine = get_async_engine()
        async with engine.connect() as conn:
            res = await conn.execute(text("SELECT version()"))
            version = res.scalar_one()
        return {"ok": True, "version": version.split(",")[0]}
    except Exception as exc:  # noqa: BLE001 — 健康检查需要吞掉所有异常
        return {"ok": False, "error": str(exc)[:200]}


def dispose_engines() -> None:
    """进程退出前释放连接池。"""
    global _async_engine, _async_sessionmaker, _sync_engine, _sync_sessionmaker
    if _sync_engine is not None:
        _sync_engine.dispose()
    _async_engine = None
    _async_sessionmaker = None
    _sync_engine = None
    _sync_sessionmaker = None


__all__ = [
    "check_db",
    "dispose_engines",
    "get_async_engine",
    "get_async_sessionmaker",
    "get_sync_engine",
    "get_sync_sessionmaker",
    "session_scope",
    "sync_session_scope",
]
