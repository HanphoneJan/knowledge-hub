"""物理删除软删文档（D26 回收站到期 / `DELETE /trash` 手工清空）。

## 为什么单独一个模块

删除链有**六步**，顺序不能换，而且**每一步漏掉都不报错**：

```
1. 词表 df 减回去            ← 漏了 → idf 被压平 → 检索质量悄悄变差（§5.29）
2. 删 Qdrant 三个 collection 的点  ← 漏了 → 索引里留着已删内容（还能搜到）
3. 删 documents 行           ← chunks / jobs 靠 ON DELETE CASCADE 跟着走
4. blob 引用计数 -1          ← 漏了 → 磁盘只增不减（ref_count 永远 >0）
5. 引用计数归零才物理删 blob 文件
6. （不做）审计日志          ← M8 保留 180 天，"人走了记录还在"
```

这个链原来只长在 worker 的 `handle_cleanup` 里。现在有第二条删除路径
（`DELETE /api/v1/trash`），**再复制一遍六步就是等一个新的 §5.29** ——
所以把"链"收在这里，两个入口共用：

| 入口 | 谁调 | session | 选谁 |
|---|---|---|---|
| `KIND_CLEANUP`（worker，到期自动） | `worker/handlers/__init__.py` | 同步 `Session` | `deleted_at < now - 30 天` |
| `DELETE /api/v1/trash`（API，手工清空） | `api/routers/documents.py` | **异步** `AsyncSession` | 调用方指定的范围 |

同步 / 异步各一份驱动，但**选择条件的 SQL 构造与 df 算法只有一份**
（`purgeable_stmt` / `chunk_texts_stmt` 与 `terms._df_delta`），两边不会漂。

⚠️ 同步驱动在 `AsyncSession` 上调用**不报错**，只是返回未 await 的 coroutine
（CONVENTIONS §0.9 / §5.28）。`purge_documents` 与 `purge_documents_async`
必须成对存在，`tests/test_purge.py` 有断言盯着。

## 不做的事

- **不碰 `is_current=TRUE` 的文档**。它们不是"回收站里的东西"，
  误清它们等于删活数据，而且不可撤销（D26 的 30 天窗口对它们不成立）。
- **不删 blob 文件本身，除非引用计数归零**（D28：同一份文件可能被多个版本引用）。
- **不删审计日志**（M8 保留 180 天）。

## 库与工作区（`purge_containers`，D125）

`DELETE /kb/{id}` 与 `DELETE /workspaces/{id}` 是**软删**，D26/D42 承诺
"保留 30 天后物理清理" —— 而这一半**从来没实现**：本模块原先只看 `documents`，
于是软删的库/工作区**永远不会被清理**，行与 blob 一直占着，
而界面上又没有它们的回收站（看得见的垃圾被清了，看不见的没有）。

🔑 **容器清理只由 worker 的 30 天到期路径触发**，不新增 API 端点 ——
D79 定了 `DELETE /trash` 是**唯一**不可撤销的端点，
再加一个"连库带文档一起清空"的入口就是第二个不可撤销入口，
而它的语义比清回收站重得多。

⚠️ **顺序不能反**：必须**先**把该库/工作区下的文档走完六步链
（df 减回去 → 清 Qdrant 三腿 → 删行 → 放 blob），
**再**删容器行（chunks / jobs / knowledge_bases 靠 `ON DELETE CASCADE` 跟着走）。
反过来先删容器，文档的 df 就永远减不回去 ——
而 df 虚高是**静默**的质量下降（§5.29）。

🔑 为什么"先文档后容器"不能省：`purgeable_stmt` 的两条恒定条件是
`is_current=false AND deleted_at IS NOT NULL`。
`delete_kb` 把文档也标成软删，所以它们**本来就会**被
`purge_documents` 收走 —— 容器清理要做的只是"在文档都清完之后删容器行"。
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field

from sqlalchemy import Select, select

from ..common.models import Chunk, Document, KnowledgeBase, Workspace
from .pipeline import remove_from_indexes, tokenize_row

logger = logging.getLogger(__name__)

#: 一批删多少份文档。太大 → 一个事务里锁的行太多（df 那一段是
#: `SELECT … FOR UPDATE`）；太小 → Qdrant 往返次数多。
#: 20 的依据：一份 74 chunk 的文档，20 份一批时整批删除不到 1 秒。
PURGE_BATCH = 20


@dataclass
class PurgeStats:
    """一次清空的实际结果。**响应体与测试断言都用它** ——
    另造一份口径就会有两个"删了多少"的答案。"""

    docs: int = 0
    chunks: int = 0
    blobs_dropped: int = 0
    bytes_freed: int = 0
    doc_ids: list[int] = field(default_factory=list)
    #: Qdrant 清失败但 PG 已删的文档 id。**不吞掉**：调用方要能报警，
    #: 对账任务（阶段 4 的 `GET /admin/consistency`）也会捡到它们。
    qdrant_failed: list[int] = field(default_factory=list)
    #: `doc_ids` 是否被截断
    ids_truncated: bool = False

    #: 响应体里最多回多少个 id —— 一次清空可能删几百份，全量回 id
    #: 会让响应变成一串没用的大数字。前端只需要"删了多少 / 多大"。
    ID_LIST_CAP = 500

    def add(
        self,
        doc_id: int,
        *,
        chunks: int,
        blob_dropped: bool,
        size_bytes: int,
    ) -> None:
        self.docs += 1
        self.chunks += chunks
        if blob_dropped:
            self.blobs_dropped += 1
            self.bytes_freed += size_bytes
        if len(self.doc_ids) < self.ID_LIST_CAP:
            self.doc_ids.append(doc_id)
        else:
            self.ids_truncated = True


def purgeable_stmt(
    *,
    kb_ids: list[int] | None = None,
    older_than: dt.datetime | None = None,
    doc_ids: list[int] | None = None,
) -> Select:
    """构造"哪些文档该被物理删除"的查询。**纯 SQL 构造，不碰 session。**

    两条恒定条件（**任何调用方都不许省**，它们是"回收站"的定义）：

    ```sql
    is_current = false          -- 只清旧版本；当前版本是活数据
    AND deleted_at IS NOT NULL  -- 必须真的软删过
    ```

    ⚠️ `is_current = false` 最容易被当成多余而删掉。后果：一次
    `DELETE /trash` 把正在被检索的文档一起删掉，**且不可撤销**。

    @param kb_ids 限定知识库；None = 不限 —— ⚠️ **调用方必须自己保证已鉴权**，
        这个函数不做任何租户判定（它没有 `ctx`，也没法有）
    @param older_than 只清在这个时间点之前软删的（D26 的 30 天）
    @param doc_ids 精确指定；给了就以它为准
    """
    stmt = select(Document).where(
        Document.is_current.is_(False),
        Document.deleted_at.isnot(None),
    )
    if doc_ids is not None:
        stmt = stmt.where(Document.id.in_(doc_ids))
    if kb_ids is not None:
        stmt = stmt.where(Document.kb_id.in_(kb_ids))
    if older_than is not None:
        stmt = stmt.where(Document.deleted_at < older_than)
    return stmt


def chunk_texts_stmt(doc_ids: Sequence[int]) -> Select:
    """取这些文档的 chunk 正文（减 df 用）。**只 SELECT，不删**。

    用 `select(两列)` 而不是 `select(Chunk)`：这里只需要正文，
    而把 ORM 实体拉进内存会在长文档上白占一份对象图。
    """
    return select(Chunk.doc_id, Chunk.content_norm).where(
        Chunk.doc_id.in_(list(doc_ids))
    )


def collect_chunk_tokens(rows: Iterable[tuple[int, str]]) -> list[list[str]]:
    """`(doc_id, content_norm)` 行 → token 列表（进 BM25 词表的那套切法）。

    ⚠️ 必须用 `tokenize_row` —— 与入库时**同一个**切法。换成别的
    （比如直接 `.split()`）会导致减掉的 df 与加进去的对不上，
    而那同样不报错，只是 idf 慢慢偏掉（§5.29）。
    """
    return [tokenize_row(text) for _doc_id, text in rows if text]


def chunk_counts(rows: Iterable[tuple[int, str]]) -> Counter:
    """每份文档有多少 chunk —— 用来在响应里如实报告，不猜。"""
    return Counter(doc_id for doc_id, _text in rows)


def _batched(items: Sequence, size: int = PURGE_BATCH) -> Iterator[Sequence]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def purge_documents(
    session,
    *,
    kb_ids: list[int] | None = None,
    older_than: dt.datetime | None = None,
    doc_ids: list[int] | None = None,
    progress: Callable[[float], None] | None = None,
) -> PurgeStats:
    """同步版（worker 的 `KIND_CLEANUP` 用）。

    ⚠️ **只能**在同步 `Session` 上调。在 `AsyncSession` 上调它不报错，
    只是一切都没发生（§0.9 / §5.28）。API 请用 `purge_documents_async`。
    """
    from ..common.storage import release_blob
    from ..retrieval.terms import unregister_chunks

    docs = list(
        session.execute(
            purgeable_stmt(kb_ids=kb_ids, older_than=older_than, doc_ids=doc_ids)
        )
        .scalars()
        .all()
    )
    stats = PurgeStats()
    total = len(docs)
    if not total:
        if progress is not None:
            progress(1.0)
        return stats

    done = 0
    for batch in _batched(docs):
        rows = session.execute(chunk_texts_stmt([d.id for d in batch])).all()
        tokens = collect_chunk_tokens(rows)
        if tokens:
            unregister_chunks(session, tokens)
        per_doc = chunk_counts(rows)

        for doc in batch:
            try:
                # Qdrant 是 HTTP 调用，没有 session —— 同步/异步两版共用同一份
                remove_from_indexes(doc.kb_id, doc.id)
            except Exception as exc:  # noqa: BLE001
                # Qdrant 挂了不该卡住清理：PG 才是真相源（D7），
                # 残留的点由对账任务捡（阶段 4）。但必须**记下来**。
                logger.error("清 Qdrant 失败 doc=%s: %s", doc.id, exc)
                stats.qdrant_failed.append(doc.id)

            sha, did, size = doc.blob_sha, doc.id, doc.size_bytes or 0
            session.delete(doc)  # chunks / jobs 靠 ON DELETE CASCADE 跟着走
            session.flush()
            dropped = release_blob(session, sha)
            stats.add(did, chunks=per_doc[doc.id], blob_dropped=dropped,
                      size_bytes=size)
        done += len(batch)
        if progress is not None:
            progress(done / total)

    if progress is not None:
        progress(1.0)
    return stats


async def purge_documents_async(
    session,
    *,
    kb_ids: list[int] | None = None,
    older_than: dt.datetime | None = None,
    doc_ids: list[int] | None = None,
    progress: Callable[[float], None] | None = None,
) -> PurgeStats:
    """异步版（API 的 `DELETE /trash` 用）。见 CONVENTIONS §0.9。"""
    from ..common.storage import release_blob_async
    from ..retrieval.terms import unregister_chunks_async

    docs = list(
        (
            await session.execute(
                purgeable_stmt(kb_ids=kb_ids, older_than=older_than, doc_ids=doc_ids)
            )
        )
        .scalars()
        .all()
    )
    stats = PurgeStats()
    total = len(docs)
    if not total:
        if progress is not None:
            progress(1.0)
        return stats

    done = 0
    for batch in _batched(docs):
        rows = (await session.execute(chunk_texts_stmt([d.id for d in batch]))).all()
        tokens = collect_chunk_tokens(rows)
        if tokens:
            await unregister_chunks_async(session, tokens)
        per_doc = chunk_counts(rows)

        for doc in batch:
            try:
                remove_from_indexes(doc.kb_id, doc.id)
            except Exception as exc:  # noqa: BLE001
                logger.error("清 Qdrant 失败 doc=%s: %s", doc.id, exc)
                stats.qdrant_failed.append(doc.id)

            sha, did, size = doc.blob_sha, doc.id, doc.size_bytes or 0
            await session.delete(doc)  # type: ignore[attr-defined]
            await session.flush()  # type: ignore[attr-defined]
            dropped = await release_blob_async(session, sha)
            stats.add(did, chunks=per_doc[doc.id], blob_dropped=dropped,
                      size_bytes=size)
        done += len(batch)
        if progress is not None:
            progress(done / total)

    if progress is not None:
        progress(1.0)
    return stats


# ============================================================
# 非当前文档的向量点：及时清理（D177）
# ============================================================


#: 「刚被顶替/删除」到清点之间的收敛窗口。**不是正确性要求**，只是避免与
#: 刚被顶替、可能还在跑的摄取抢跑（它可能再写几个点）。见 `remove_noncurrent_points`。
STALE_POINT_GRACE = dt.timedelta(minutes=5)
#: 一次 sweep 最多处理多少份文档。用 `doc_id any […]` 过滤，所以这是
#: "几次 Qdrant 往返"的分母，不是"几个点"的分母。
STALE_POINT_BATCH = 1000


def noncurrent_doc_ids_stmt(*, older_than: dt.datetime) -> Select:
    """「已非当前」的文档 id（用于清它们的向量点）。

    判据只有一条：`deleted_at IS NOT NULL` —— 覆盖**两种**"已非当前"：
    - 同名顶替（`ingest/versions.py::supersede` 同时置 `deleted_at`）
    - 手工软删（`DELETE /documents/{id}`）

    ⚠️ 与 `purgeable_stmt`（30 天物理删）**不是一回事**：这条只管向量点，
    **不碰** PG 行 / df / blob。别把两者合并 —— 那是 §5.29 那个静默坑的入口。
    """
    return (
        select(Document.id)
        .where(Document.deleted_at.isnot(None), Document.deleted_at < older_than)
        .order_by(Document.id)
    )


def remove_noncurrent_points(
    session,
    *,
    grace: dt.timedelta = STALE_POINT_GRACE,
    batch: int = STALE_POINT_BATCH,
) -> int:  # noqa: ANN001 — 同步 Session（worker 用）
    """删除「已非当前」文档在 Qdrant 的全部点（dense / sparse / visual）。返回份数。

    ## 为什么需要它（R7，实测）

    文档被同名新版本顶替后（`supersede`）旧点**不会**被删，而它要等 30 天的
    `purge` 才清。这在两处造成真实问题，实测：顶替一次后
    `check_drift` 报 `kb_dense 多 1 / kb_sparse 多 1`（旧点成了"孤儿"）——
    于是巡检的「索引对账」会在**整整 30 天里**一直黄（违反"长期红的项等于没有项"）；
    同时旧点占着召回名额（`_drop_deleted` 在 `top_k` 截断**前**才挡掉，
    但它先占满了 `top_k*2` 的融合池）。

    ## 为什么可以安全地删（旧点没有消费者）

    🔑 **恢复一律重摄取**：两个恢复入口（`POST /documents/{id}/restore`、
    `POST /kb/{id}/restore`）都 `enqueue(KIND_INGEST)` —— 旧向量**从来不会被复用**。
    所以 D44 那句"3 天内恢复免重跑索引"在本代码里本就不成立（见 D177）。

    ⚠️ 只删 Qdrant 点：**不碰** df（`terms`）/ blob / PG 行。df 的正确口径是
    "全部 chunk 行"（D115），行留到 30 天 `purge` 才走六步链。

    ⚠️ 与 D142「对账只报告不自动修」**不冲突**：那条防的是删**正在写**的点
    （`flush` 未提交的窗口 → 永久空洞）。这里删的是**已提交为非当前**的文档
    （`deleted_at IS NOT NULL`）的整份点集 —— 那一份**永远不会**再被检索，
    删了也不会产生空洞。
    """
    from ..common.qdrant import COLL_DENSE, COLL_SPARSE, COLL_VISUAL, delete_by_filter

    cutoff = dt.datetime.now(dt.UTC) - grace
    ids = list(session.execute(
        noncurrent_doc_ids_stmt(older_than=cutoff).limit(batch)
    ).scalars().all())
    if not ids:
        return 0
    flt = {"must": [{"key": "doc_id", "match": {"any": ids}}]}
    for coll in (COLL_DENSE, COLL_SPARSE, COLL_VISUAL):
        delete_by_filter(coll, flt)
    return len(ids)


__all__ = [
    "PURGE_BATCH",
    "PurgeStats",
    "STALE_POINT_BATCH",
    "STALE_POINT_GRACE",
    "chunk_counts",
    "chunk_texts_stmt",
    "collect_chunk_tokens",
    "noncurrent_doc_ids_stmt",
    "purge_documents",
    "purge_documents_async",
    "purgeable_stmt",
    "remove_noncurrent_points",
]

# ============================================================
# 库与工作区（D125）
# ============================================================


@dataclass
class ContainerPurgeStats:
    """容器清理的结果。

    🔑 **只报容器自己的数量**，不报文档/chunk/字节 ——
    那些由 `purge_documents` 的 `PurgeStats` 报。两套数字合成一个
    会让人以为"清掉了 N 个文档"，而实际清掉的是 N 个**库**
    （§5.57：数字只能来自真实来源）。
    """

    kbs: int = 0
    workspaces: int = 0


def purgeable_containers_stmt(
    model, *, older_than: dt.datetime | None = None
) -> Select:
    """「哪些库/工作区该被物理删除」。

    ⚠️ 条件**只有 `deleted_at < older_than`**，没有 `is_current` ——
    容器没有"版本"概念（那是文档的事，D28），软删即到期候选。

    纯 SQL 构造、不碰 session（与 `purgeable_stmt` 同一形状），
    所以选择条件不会在两条驱动之间漂。
    """
    stmt = select(model).where(model.deleted_at.isnot(None))
    if older_than is not None:
        stmt = stmt.where(model.deleted_at < older_than)
    return stmt


def purge_containers(
    session,
    *,
    older_than: dt.datetime | None = None,
) -> ContainerPurgeStats:
    """物理删除到期软删的**库与工作区**（worker 的 `KIND_CLEANUP` 用）。

    🔑 **只删容器行**：它们下面的文档由 `purge_documents` 收走
    （`delete_kb` 已把它们标成软删，那条链会覆盖到）。
    所以调用方**必须先**跑 `purge_documents`，本函数做的是
    "文档都清完了，把空壳删掉"这一步 —— 它**不碰** df / Qdrant / blob。

    ⚠️ 为什么不再走一遍文档链：同一批文档被清两次会让 df **减两次**
    （`_df_delta` 只加不减，§5.29），症状是 idf 变负、检索被噪声淹没，
    **而且不报错**。所以"顺序 + 职责"都要固定，不能图省事两处都清。

    ⚠️ **工作区先删**：它的 KB 靠 `ON DELETE CASCADE` 跟着走；
    反过来先删库、再删工作区虽然也对，但那样就**分不清**哪些库是
    "自己到期该删"、哪些是"跟着工作区走的"，日志里会少报库数。
    """
    stats = ContainerPurgeStats()

    ws_rows = list(
        session.execute(
            purgeable_containers_stmt(Workspace, older_than=older_than)
        )
        .scalars()
        .all()
    )
    ws_ids = [w.id for w in ws_rows]
    for ws in ws_rows:
        session.delete(ws)
    if ws_rows:
        session.flush()
    stats.workspaces = len(ws_rows)

    kb_rows = list(
        session.execute(
            purgeable_containers_stmt(KnowledgeBase, older_than=older_than)
        )
        .scalars()
        .all()
    )
    # 已经在上面跟着工作区级联删掉的库**不再重复报数**
    kept = [kb for kb in kb_rows if not (ws_ids and kb.workspace_id in ws_ids)]
    for kb in kept:
        session.delete(kb)
    session.flush()
    stats.kbs = len(kept)
    return stats
