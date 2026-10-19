"""🔑 「同名旧版本进回收站」的唯一实现（增量索引 · D28/D114）。

## 为什么这个函数存在

**同一件事有两个地方在做，而且其中一个是错的**：

- `POST /kb/{id}/documents`（上传）：同名旧版本置 `is_current=False` + 软删 ✅
- `handle_import_dir` → `_create_doc`（目录导入）：**只在"同名**且**同内容"时跳过**，
  同名但内容变了就**再建一行 Document** ❌

症状是同一个库里出现**两个 `is_current=True` 的同名文档**：
- 搜索返回同一张图/同一份文件的**两份内容**（用户看到"重复结果"）
- 旧版本的 chunk 与 Qdrant 点**永远不清**（DF 与一致性对账开始漂移）
- 上传端点那一句 `scalar_one_or_none()` 会抛 `MultipleResultsFound`
  → 那个库之后**再也传不了同名文件**（500）

第三点最阴：它是**先前那次错误导入留下的**，于是"我上周导了一次照片，今天
传同名的文件就 500"—— 症状与原因隔了一周。

## 为什么是"软删 + is_current=False"而不是真的删

D26 的 30 天反悔窗口：旧版本进回收站，用户可以恢复。
DF 与向量点的清理由 `KIND_CLEANUP` 在 30 天后统一做 ——
**不要**在这里顺手 unregister（那会让"恢复旧版本"变成不可能）。

## 为什么要抽成函数（而不是各写各的）

CONVENTIONS §0.1 的精神：一份行为只能有一处实现。这里两处行为**本该相同**
却已经分叉了，所以把「判据」（哪个算当前版本）与「动作」（置软删）都收进来，
两边只负责各自的查询（一个 AsyncSession、一个同步 Session）。

⚠️ 判据必须包含 `deleted_at IS NULL` 之外的 `is_current IS TRUE`：
软删的旧版本**不该**被再次 supersede（它已经是历史了）。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import func, select

from ..common.models import Document


def current_by_name(kb_id: int, file_name: str) -> tuple[Any, ...]:
    """`select(Document)` 的 where 条件（唯一判据）。

    ⚠️ **不要**写成 `scalar_one_or_none()` 的用法：历史上已经存在两个
    同名 current 文档的数据（见模块 docstring），那条查询会抛
    `MultipleResultsFound` → 整个上传/导入路径 500。
    调用方必须用 `first()` 并自行处理"多于一个"的情况。
    """
    return (
        Document.kb_id == kb_id,
        Document.file_name == file_name,
        Document.is_current.is_(True),
    )


def supersede(doc: Document | None, *, now: dt.datetime | None = None) -> bool:
    """把旧版本置为"非当前 + 已删除"。返回是否真的动过手。

    ⚠️ `now` 统一用 `dt.datetime.now(dt.UTC)`：之前那两份实现一处用了
    带时区的、一处没用，而 PG 列是 `timestamptz` ——
    naive datetime 会被当成**本地时间**，这台机器是 +08:00，
    于是"删除于"会差 8 小时，且回收站 30 天的到期时间也跟着偏。

    @return False 表示 `doc` 是 None 或**已经**是软删状态（幂等）。
    """
    if doc is None:
        return False
    if not doc.is_current and doc.deleted_at is not None:
        return False                      # 已经是历史版本，别重复动
    doc.is_current = False
    doc.deleted_at = now or dt.datetime.now(dt.UTC)
    return True


def find_current(session, kb_id: int, file_name: str) -> Document | None:
    """**同步** Session 版：查当前版本（找不到或有多于一个时取第一个）。"""
    rows = session.execute(
        select(Document).where(*current_by_name(kb_id, file_name))
    ).scalars().all()
    if len(rows) > 1:
        # ⚠️ 历史脏数据（见模块 docstring）。取第一个继续跑，但**说出来**——
        #   静默挑一个会让"哪个才是当前版本"变成猜谜。
        import logging

        logging.getLogger(__name__).warning(
            "库 %s 里有 %d 个同名 current 文档（%s），取 id=%s 继续；"
            "请清掉多余的那些（旧版本应在回收站里）",
            kb_id, len(rows), file_name, rows[0].id,
        )
    return rows[0] if rows else None


def next_version_id(session, kb_id: int, file_name: str) -> int:
    """这个 `(kb_id, file_name)` 的**下一个**版本号（同步 Session，D192）。

    从 1 起。取**现有最大版本 + 1** 而不是"当前版本 + 1"：脏数据里可能已有
    多个同名 current 文档（见模块 docstring），按 max 才不会把新版本号撞回旧值。
    没有历史就是第 1 版。
    """
    return 1 + int(
        session.execute(
            select(func.coalesce(func.max(Document.version_id), 0)).where(
                Document.kb_id == kb_id, Document.file_name == file_name
            )
        ).scalar_one()
    )


async def next_version_id_async(session, kb_id: int, file_name: str) -> int:
    """`next_version_id` 的异步版（上传端点用，CONVENTIONS §0.9）。语义逐字一致。"""
    return 1 + int(
        (
            await session.execute(
                select(func.coalesce(func.max(Document.version_id), 0)).where(
                    Document.kb_id == kb_id, Document.file_name == file_name
                )
            )
        ).scalar_one()
    )


__all__ = [
    "current_by_name",
    "find_current",
    "next_version_id",
    "next_version_id_async",
    "supersede",
]