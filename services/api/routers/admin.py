"""运维端点（M9：索引对账 / 重建 / 磁盘统计 —— 契约 §12）。

三件事，都是"多租户系统能不能真的运维起来"的前提：

## 1. `POST /admin/reindex` —— 重建索引

Qdrant 是**可重建的派生索引**（D7），所以重建必须安全可重复 ——
点 ID 是 `hash(kb_id, doc_id, chunk_index)`（D33），重建出来与原来完全一致。
它**入队**而不是同步跑：一份 74 chunk 的文档重建要几百毫秒，
20 份就是几秒，同步跑会把 API 进程占住（D50 的教训）。

⚠️ **范围默认是"我有权访问的库"，不是全库**。重建不是只读操作 ——
它会删掉 Qdrant 里该库的点再重建，越权的全库重建等于让别人��库短暂搜不到。

## 2. `GET /admin/consistency` —— PG vs Qdrant 对账

D7 明确允许两者短暂不一致，但"短暂"要有地方看。这个端点返回**具体不一致的 id**，
而不是一个布尔值 —— 布尔值只能告诉你"有问题"，不能告诉你"修哪几个"。

⚠️ 两侧都用 `point_id(kb, doc, chunk)` 这**同一个函数**算键：
自己另写一份公式的话，公式一改两侧一起错，对账就会永远说"一致"。

🔑 **三条腿各自对账**（D124 起包含视觉腿）：

| 腿 | PG 侧的真相来源 | 键 |
|---|---|---|
| `kb_dense` / `kb_sparse` | `chunks` 行 | `point_id(kb, doc, chunk_index)` |
| `kb_visual` | `documents.scan_pages`（页码清单） | `point_id_for_page(kb, doc, page)` |

⚠️ **顶层 `consistent` 只是文本腿口径**（dense ∪ sparse）。
视觉腿是**页级**点（`chunk_index` 为负），混进并集只会让两边的数字都没法读 ——
看 `legs["kb_visual"]`。

🔑 为什么必须把视觉腿纳入对账（D123 的教训）：
`kb_visual` 恒为 0 个点时**没有任何地方会报警** —— 它长期被排除在对账之外，
于是"8 个任务全 failed"被读成"库里没有照片"，
一路让 M4（扫描件视觉路径）与 M6（photo 路由收益）两个里程碑
都被误判成"缺数据"。**没有被对账覆盖的腿等于不存在的腿。**

## 3. `GET /workspaces/{id}/stats` —— 按工作区统计磁盘

M9 要求"按工作区统计"。⚠️ **必须给两个字节数，而且它们量的不是同一个东西**：
- `sum_doc_size_bytes`：各库文档 `size_bytes` 之和 —— **只有原始文件**
- `blob_disk_bytes`：这些 blob **目录**的实际占盘 = 原始 + 派生文件
  （`preview.pdf`、`thumbs/*.jpg`，见 §5.16 的目录布局）

所以 `blob_disk_bytes` 通常**大于** `sum_doc_size_bytes`（实测：318KB 原始
+ 预览与缩略图 = 722KB）。⚠️ 早期文档写成"更小"，是只想着去重、忘了派生物。

去重体现在**另一个方向**：同一份内容被 N 个库引用时，`sum_doc_size_bytes`
会算 N 次，而磁盘只占一份（按 **sha 去重**后累加）。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from sqlalchemy import func, select

from ...common.auth import assert_kb_access, assert_superadmin, can_admin_workspace
from ...common.deps import CurrentUser, DbSession
from ...common.errors import InvalidRequest, ResourceNotFound
from ...common.jobs import KIND_REINDEX, enqueue_async
from ...common.models import Blob, Chunk, Document, KnowledgeBase, Workspace
from ...common.consistency import (          # 🔑 D143：对账判据的唯一来源
    COLL_DENSE,
    COLL_SPARSE,
    COLL_VISUAL,
    pg_page_point_ids_async,
    pg_point_ids_async,
    qdrant_point_ids,
)
from ...common.schemas_ops import (
    ConsistencyReport,
    LegReport,
    ReindexResp,
    WorkspaceStats,
)
from ...common.storage import blob_dir

logger = logging.getLogger(__name__)
router = APIRouter(tags=["admin"])


@router.post(
    "/admin/reindex",
    response_model=ReindexResp,
    summary="重建索引（D33 稳定 ID 保证安全）",
)
async def reindex(
    ctx: CurrentUser,
    session: DbSession,
    kb_id: int | None = None,
    swap: bool = False,
) -> ReindexResp:
    """入队 `KIND_REINDEX` 重建索引（不阻塞 API 进程）。

    ⚠️ **默认范围是"我有权访问的库"**。重建不是只读操作 ——
    一个越权的全库重建等于让别人的库在重建期间搜不到东西。

    🔑 D193：`swap=true` 是**蓝绿重建**（整实例、需超管）——把两条文本腿重建到
    影子集合、验证后原子切别名，全程旧索引可读，旧集合保留用于回滚。
    它要求别名模式（`QDRANT_ALIASES=true`），此时忽略 `kb_id`。
    """
    if swap:
        # 整实例操作：会重建**所有人**的向量，必须超管。
        assert_superadmin(ctx)
        job = await enqueue_async(session, KIND_REINDEX, payload={"swap": True})
        await session.commit()
        logger.info("蓝绿重建：入队整实例 swap 任务 %s", job.id)
        return ReindexResp(kb_ids=[], job_ids=[job.id], mode="swap")

    if kb_id is not None:
        await assert_kb_access(session, ctx, kb_id)
        targets = [kb_id]
    else:
        targets = sorted(ctx.readable_kb_ids)

    if not targets:
        raise InvalidRequest("没有可重建的知识库（你对任何库都没有访问权）")

    job_ids = [
        (await enqueue_async(session, KIND_REINDEX, kb_id=k)).id for k in targets
    ]
    await session.commit()
    logger.info("重建索引：%s 个库，任务 %s", len(targets), job_ids)
    return ReindexResp(kb_ids=targets, job_ids=job_ids)


@router.get(
    "/admin/consistency",
    response_model=ConsistencyReport,
    summary="PG vs Qdrant 一致性对账（D7）",
)
async def consistency(
    ctx: CurrentUser, session: DbSession, kb_id: int | None = None
) -> ConsistencyReport:
    """返回**具体不一致的 id**，不是布尔值。

    布尔值只能告诉你"有问题"，不能告诉你"修哪几个" —— 对账的价值全在后者。
    """
    targets = [kb_id] if kb_id is not None else sorted(ctx.readable_kb_ids)
    for k in targets:
        await assert_kb_access(session, ctx, k)
    if not targets:
        return ConsistencyReport(kb_ids=[])

    # 🔑 D143：判据与 Qdrant 侧全部来自 `common/consistency.py`（**唯一一份**），
    #   与 `deploy/monitor.py`、worker 的定期对账共用。
    #   ⚠️ 以前这里是**第二份**实现（async 版）：两份各写一遍 `WHERE` 与
    #   "页级点怎么认"，改了一处就会让"端点说一致、巡检说有漂移"——
    #   对账工具自己不一致是最难查的一类问题（D140 刚为两份清单付过代价）。
    pg = await pg_point_ids_async(session, targets)
    # 🔑 分腿对账：dense 与 sparse 分开比（见 schemas_ops.ConsistencyReport 的说明）。
    #   取并集的话"只有一条腿坏了"会报成"一致"，而那正是真实的故障模式。
    per_leg = {
        COLL_DENSE: qdrant_point_ids([COLL_DENSE], targets),
        COLL_SPARSE: qdrant_point_ids([COLL_SPARSE], targets),
    }
    # 🔑 D124：**视觉腿也要对账**，否则它坏了没有任何地方会知道。
    #   `kb_visual` 存的是**页级**点（`point_id_for_page`），PG 侧的真相来源
    #   是 `documents.scan_pages`（D37 视觉路径的页码清单）。
    #   ⚠️ 这一条正是 D123 烂了那么久没人发现的根因：只有两条腿被对账覆盖，
    #     "kb_visual 恒为 0 个点"看起来像"库里没有照片"，而不是"任务全 failed"。
    per_leg[COLL_VISUAL] = qdrant_point_ids(
        [COLL_VISUAL], targets, page_level=True
    )
    expected_visual = await pg_page_point_ids_async(session, targets)

    legs: dict[str, LegReport] = {}
    for coll, ids in per_leg.items():
        expected = expected_visual if coll == COLL_VISUAL else pg
        miss = sorted(expected - ids)
        dang = sorted(ids - expected)
        legs[coll] = LegReport(
            collection=coll,
            points=len(ids),
            missing=miss[:200],
            dangling=dang[:200],
            missing_count=len(miss),
            dangling_count=len(dang),
            consistent=not miss and not dang,
        )
    # ⚠️ 顶层是**文本腿**（dense ∪ sparse）口径 —— 视觉腿是页级、
    #   与 chunk 级点 ID 空间独立，混进并集会让两边的数字都没法读。
    #   要看视觉腿看 `legs["kb_visual"]`。
    qdrant = set().union(per_leg[COLL_DENSE], per_leg[COLL_SPARSE])
    missing = sorted(pg - qdrant)
    dangling = sorted(qdrant - pg)

    return ConsistencyReport(
        kb_ids=targets,
        pg_chunks=len(pg),
        qdrant_points=len(qdrant),
        legs=legs,
        missing_in_qdrant=missing[:200],
        dangling_in_pg=dangling[:200],
        missing_count=len(missing),
        dangling_count=len(dangling),
        consistent=not missing and not dangling,
    )


# ⚠️ D143：三份对账实现（这里 / `deploy/monitor.py` / 旧的 worker 路径）已收成
#   **一份**：`common/consistency.py`。端点、巡检、worker 的定期对账现在共用
#   同一份判据（`_stmt_point_ids` / `_stmt_page_ids` / `qdrant_point_ids`），
#   所以三处的数字必然一致。
#   下面那段 async 版的 `_pg_point_ids` / `_pg_page_point_ids` /
#   `_qdrant_point_ids` 已删除 —— 留着就等于又开了一份会漂的拷贝。


@router.get(
    "/workspaces/{ws_id}/stats",
    response_model=WorkspaceStats,
    summary="按工作区统计（M9：磁盘用量）",
)
async def workspace_stats(ws_id: int, ctx: CurrentUser, session: DbSession) -> WorkspaceStats:
    """工作区维度的统计（KB / 文档 / chunk / 磁盘）。

    ⚠️ **两个字节数，缺一不可**：见模块 docstring 的说明。
    """
    ws = (
        await session.execute(
            select(Workspace).where(
                Workspace.id == ws_id, Workspace.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if ws is None or not can_admin_workspace(ctx, ws):
        raise ResourceNotFound(detail={"workspace_id": ws_id})

    kb_ids = list(
        (
            await session.execute(
                select(KnowledgeBase.id).where(
                    KnowledgeBase.workspace_id == ws_id,
                    KnowledgeBase.deleted_at.is_(None),
                )
            )
        ).scalars().all()
    )
    if not kb_ids:
        return WorkspaceStats(workspace_id=ws_id)

    live = (
        Document.kb_id.in_(kb_ids),
        Document.is_current.is_(True),
        Document.deleted_at.is_(None),
    )
    n_docs, sum_size = (
        await session.execute(
            select(func.count(Document.id), func.coalesce(func.sum(Document.size_bytes), 0))
            .where(*live)
        )
    ).one()
    n_chunks = (
        await session.execute(
            select(func.count(Chunk.id))
            .join(Document, Document.id == Chunk.doc_id)
            .where(*live)
        )
    ).scalar_one()
    shas = list(
        (await session.execute(select(Document.blob_sha).where(*live))).scalars().all()
    )
    n_exclusive = (
        await session.execute(
            select(func.count(Blob.sha256)).where(
                Blob.ref_count == 1, Blob.sha256.in_(shas)
            )
        )
    ).scalar_one()

    return WorkspaceStats(
        workspace_id=ws_id,
        kb_count=len(kb_ids),
        doc_count=n_docs,
        chunk_count=n_chunks,
        sum_doc_size_bytes=int(sum_size),
        exclusive_blob_count=n_exclusive,
        blob_disk_bytes=_disk_bytes(shas),
    )


def _disk_bytes(shas: list[str]) -> int:
    """这些 blob 目录实际占多少字节（**按 sha 去重**，含派生文件）。

    ⚠️ 含派生文件（`preview.pdf` / `thumbs/*.jpg`），所以它**大于**原始文件之和。
    别拿它与 `sum_doc_size_bytes` 比大小 —— 两者量的不是同一个东西。
    

    ⚠️ 内容寻址意味着同一个目录可能被多个库引用（`data/blobs/{aa}/{sha}` 里
    没有 kb 信息）—— 逐库累加会重复计算，用户会发现"删了半天没腾出那么多空间"。
    宁可少算也不重复算。
    """
    total = 0
    for sha in set(shas):
        d = blob_dir(sha)
        if not d.exists():
            continue
        for f in d.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    return total