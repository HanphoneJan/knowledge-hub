"""分页工具。

契约：`page` 从 1 开始，`page_size` 默认 20 上限 100（`docs/DESIGN-API.md` 0.4）。
"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel
from sqlalchemy import Select, func, select

T = TypeVar("T")

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


def paginate(stmt: Select, page: int, page_size: int) -> Select:
    """给查询加 limit/offset。page 从 1 开始。"""
    size = min(page_size, MAX_PAGE_SIZE)
    return stmt.limit(size).offset((page - 1) * size)


def count_stmt(model: type) -> Select:
    """配套的 count 查询（注意要剥掉 order_by，否则 PG 会包一层子查询）。"""
    return select(func.count()).select_from(model)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int


async def run_paginated(
    session,  # noqa: ANN001 — AsyncSession，避免循环 import
    stmt: Select,
    page: int,
    page_size: int,
    scalars: bool = True,
) -> tuple[list, int]:
    """执行分页查询，返回 `(items, total)`。

    count 与 items 一次往返拿（两个 execute，不额外开事务）。
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    assert isinstance(session, AsyncSession)
    size = min(page_size, MAX_PAGE_SIZE)
    total = (
        await session.execute(
            select(func.count()).select_from(stmt.order_by(None).subquery())
        )
    ).scalar_one()
    result = await session.execute(paginate(stmt, page, size))
    items = result.scalars().all() if scalars else result.all()
    return list(items), total
