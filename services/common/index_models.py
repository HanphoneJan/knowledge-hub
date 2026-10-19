"""🔑 Embedding 模型一致性守卫（D191）。

文章《RAG 知识库文档如何更新》把"索引与查询必须用同一个 Embedding 模型"
列为**硬规则**：不同模型的向量空间不通用，混用等于在两个空间里算相似度，
结果不是"随机"，而是**整个排序基础坏了**，且**不报错**。

本模块把"谁建的索引"记成元数据（`index_models` 表），提供：

| 函数 | 用途 | 谁调 |
|---|---|---|
| `record_model` / `record_model_async` | 入库 / 重建时写下当前配置的模型 | `ingest/pipeline.py`、`worker/handlers` |
| `get_models` / `get_models_async` | 读回记录 | `/health`、巡检 |
| `text_model_mismatch` / `_async` | **查询前校验**：不一致就返回原因 | `retrieval/dense.py` |

## 判据与保守方向

- 记录**不存在**（空表 / 加这一列之前建的索引）⇒ 返回 `None`（**放行**）。
  没记录不等于"不一致"——在未知上拦查询会把正常部署拦死。
- 不一致 ⇒ 返回一句**可操作**的原因（含"怎么修"），dense 腿据此 `skipped`。
  这是"降级而不是静默返回垃圾"：其它腿照常跑，检索仍 200（与视觉腿同款纪律）。

## 同步 / 异步各一份（CONVENTIONS §0.9）

写入路径在 worker（同步 `Session`），查询路径在 API（`AsyncSession`）——
在 `AsyncSession` 上调同步版**不报错**，只是返回一个从未 await 的 coroutine。
所以两个名字必须都存在，且语义逐字一致。
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from .config import get_settings
from .models import IndexModel
from .qdrant import COLL_DENSE, COLL_VISUAL, VDR_DIM


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def model_mismatch(
    recorded: IndexModel | None, model: str, dim: int
) -> str | None:
    """比对一条记录与"当前配置的模型"。**纯函数**，好单测。

    @return None = 一致、或**没有记录**（未知，放行）；否则一句可操作的原因。
    """
    if recorded is None:
        return None
    if recorded.model == model and recorded.dim == dim:
        return None
    return (
        f"索引是用 `{recorded.model}`（{recorded.dim} 维）建的，"
        f"当前配置是 `{model}`（{dim} 维）—— 向量空间不通用，"
        "对应检索腿已降级（不会静默返回垃圾）。修法：把配置改回原模型，"
        "或重建索引（`POST /api/v1/admin/reindex`）让索引跟上新模型。"
    )


# ============================================================
# 写入（worker 同步 Session / 少数异步入口）
# ============================================================


def record_model(
    session: Session,
    collection: str,
    model: str,
    dim: int,
    *,
    model_version: str | None = None,
) -> None:
    """记下"这条腿是用哪个模型建的"。UPSERT，可重复调用。"""
    values = {
        "collection": collection,
        "model": model,
        "dim": dim,
        "model_version": model_version,
        "updated_at": _now(),
    }
    session.execute(
        pg_insert(IndexModel)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[IndexModel.collection],
            set_={
                "model": model,
                "dim": dim,
                "model_version": model_version,
                "updated_at": _now(),
            },
        )
    )


async def record_model_async(
    session: AsyncSession,
    collection: str,
    model: str,
    dim: int,
    *,
    model_version: str | None = None,
) -> None:
    """`record_model` 的异步版（语义逐字一致，§0.9）。"""
    await session.execute(
        pg_insert(IndexModel)
        .values(
            collection=collection,
            model=model,
            dim=dim,
            model_version=model_version,
            updated_at=_now(),
        )
        .on_conflict_do_update(
            index_elements=[IndexModel.collection],
            set_={
                "model": model,
                "dim": dim,
                "model_version": model_version,
                "updated_at": _now(),
            },
        )
    )


def record_current_text_model(session: Session) -> None:
    """把**当前配置**的文本模型记成 dense 腿的建索引模型。

    🔑 调用点是"真的写了 dense 向量"的地方（`_index_text_legs`）——
    只有那里能保证"记录"与"实际编码"用的是同一个模型。
    """
    s = get_settings()
    record_model(session, COLL_DENSE, s.text_embed_model, s.text_embed_dim)


def record_current_visual_model(session: Session) -> None:
    """把当前配置的视觉模型记成 `kb_visual` 腿的建索引模型。"""
    s = get_settings()
    record_model(session, COLL_VISUAL, s.vdr_model, VDR_DIM)


# ============================================================
# 读取与校验
# ============================================================


def get_models(session: Session) -> dict[str, IndexModel]:
    """所有腿的记录，按 collection 名索引。"""
    rows = session.execute(select(IndexModel)).scalars().all()
    return {row.collection: row for row in rows}


async def get_models_async(session: AsyncSession) -> dict[str, IndexModel]:
    """`get_models` 的异步版（§0.9）。"""
    rows = (await session.execute(select(IndexModel))).scalars().all()
    return {row.collection: row for row in rows}


def text_model_mismatch(session: Session) -> str | None:
    """dense 腿的查询前校验（同步）。见 `model_mismatch` 的语义。"""
    s = get_settings()
    return model_mismatch(
        get_models(session).get(COLL_DENSE), s.text_embed_model, s.text_embed_dim
    )


async def text_model_mismatch_async(session: AsyncSession) -> str | None:
    """`text_model_mismatch` 的异步版（§0.9）。查询路径用它。"""
    s = get_settings()
    recorded = (await get_models_async(session)).get(COLL_DENSE)
    return model_mismatch(recorded, s.text_embed_model, s.text_embed_dim)


def visual_model_mismatch(session: Session) -> str | None:
    """视觉腿的模型一致性（巡检 / 汇报用；视觉腿的降级在它自己的判断里）。"""
    s = get_settings()
    return model_mismatch(
        get_models(session).get(COLL_VISUAL), s.vdr_model, VDR_DIM
    )


__all__ = [
    "get_models",
    "get_models_async",
    "model_mismatch",
    "record_current_text_model",
    "record_current_visual_model",
    "record_model",
    "record_model_async",
    "text_model_mismatch",
    "text_model_mismatch_async",
    "visual_model_mismatch",
]
