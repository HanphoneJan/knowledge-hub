"""知识库端点（M2）。契约见 `docs/DESIGN-API.md` 第 3 节。

## 可见性是这一层唯一需要小心的东西（D6 / D43）

| visibility | 谁能看 | 谁能改 |
|---|---|---|
| `private` | 只有创建者 | 只有创建者 |
| `workspace` | 该工作区全体成员 | 该工作区全体成员 |
| `global` | **所有用户** | **所有用户**（D43） |

D43 是个有意识的取舍："所有用户可读可编辑"在 5~20 人的信任团队里没问题，
但**如果以后对外开放，这是最先被攻击的点**。所以：

1. 判定逻辑集中在 `auth.can_write_kb` / `auth.can_read_kb`，
   不在路由里散落 —— 将来接 RBAC 时只改这两处
2. 路由返回的错误码全部是 `KB_NOT_FOUND`（fail-closed），
   列表接口只返回我有权限的，不返回"存在但无权"的占位
"""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, status
from sqlalchemy import and_, exists, false, func, or_, select

from ...common.auth import (
    assert_kb_access,
    assert_kb_write,
    assert_workspace_access,
    can_write_kb,
    visible_kb_clause,
    writable_kb_clause,
)
from ...common.config import get_settings
from ...common.deps import CurrentUser, DbSession
from ...common.errors import InvalidRequest, ResourceNotFound
from ...common.jobs import KIND_CLEANUP, KIND_INGEST, enqueue, enqueue_async
from ...common.models import Chunk, Document, KnowledgeBase, Workspace
from ...common.pagination import run_paginated
from ...common.schemas import (
    CreateKbReq,
    KbDetail,
    KbOut,
    PageMeta,
    UpdateKbReq,
)

logger = logging.getLogger(__name__)

#: D166 之前的文档 `quality` 为 NULL —— 按当时的默认策略计（D164 的默认就是它）。
_DEFAULT_STRATEGY = "recursive"


def stale_strategy_condition(kb_id: int, strategy: str):
    """「还停在旧切分策略」的当前文档的 SQL 条件（D181）。

    判据 = 文档**摄取时**记下的策略（`documents.quality->>'strategy'`，D166）
    ≠ 该库**当前**的 `chunk_strategy`。改策略只对之后的摄取生效，所以这个条件
    数出来的就是"重摄取欠账"（对应文章「坑三：一库里混多套切法」）。

    ⚠️ 单独抽成函数是为了能在**真库**上单测它（§5.92）—— 内联在端点里就只能
    靠端到端测。`coalesce` 兜底成默认策略：默认库不会平白报一堆欠账。
    """
    return and_(
        Document.kb_id == kb_id,
        Document.is_current.is_(True),
        Document.deleted_at.is_(None),
        func.coalesce(Document.quality["strategy"].astext, _DEFAULT_STRATEGY)
        != strategy,
    )
router = APIRouter(prefix="/kb", tags=["kb"])


def _visible_kb_filter(ctx, workspace_id: int | None = None):
    """构造"我能看到的 KB"过滤条件（PG 侧，列表/回收站用）。

    🔑 **实现已经收进 `auth.visible_kb_clause`**（D129）——
    本函数只是保留一个名字给本模块用，**不要再在这里写一遍三档可见性**：
    它本来就有两份拷贝（这里 + `load_user_context`），而"两处复刻可见性逻辑"
    正是本项目唯一一次真实越权的成因（CONVENTIONS §0.6）。
    加第三个使用点（回收站）时不收口，就等于给同一条历史漏洞再开一个口子。
    """
    return visible_kb_clause(ctx, model=KnowledgeBase, workspace_id=workspace_id)


@router.get("", response_model=list[KbOut], summary="我可见的知识库")
async def list_kb(
    session: DbSession,
    ctx: CurrentUser,
    workspace_id: int | None = None,
    visibility: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> list[KbOut]:
    stmt = select(KnowledgeBase).where(
        KnowledgeBase.deleted_at.is_(None),
        _visible_kb_filter(ctx, workspace_id),
    )
    if visibility:
        stmt = stmt.where(KnowledgeBase.visibility == visibility)
    rows, _ = await run_paginated(
        session, stmt.order_by(KnowledgeBase.created_at), page, page_size
    )
    return [KbOut.model_validate(r) for r in rows]


@router.get(
    "/trash",
    response_model=list[KbOut],
    summary="知识库回收站（我删掉的那些）",
)
async def list_kb_trash(session: DbSession, ctx: CurrentUser) -> list[KbOut]:
    """**软删**的知识库列表（D129）。

    🔑 为什么必须有这个端点：`DELETE /kb/{id}` 软删、`POST /kb/{id}/restore`
    能放回去（D125），但**没有任何地方能列出"我删过哪些"** ——
    用户只能凭记忆按 id 调恢复端点。而 D26/D42 承诺的是"30 天反悔窗口"，
    看不见的窗口等于没有（D125 的原话：承诺了却没有，比明说不可撤销更糟）。

    ⚠️ **可见性用 `auth.writable_kb_clause`（与 `restore_kb` 的 `can_write_kb`
    逐档等价）**，不是读侧的 `visible_kb_clause` ——
    两者差在"workspace 可见性但创建者已不是成员"那一格：那种库**读得到、
    改不了**，拿读判据筛就会出现"列表里有、点恢复 403"（D129 第一版就踩了）。

    ⚠️ 软删的库**不在** `ctx.readable_kb_ids` 里（`load_user_context` 过滤
    `deleted_at IS NULL`），所以这里**不能**拿 `readable_kb_ids` 过滤 ——
    那个集合按定义就不含软删行，用它筛的结果**恒为空**。
    与 D125 的 restore 撞的是同一堵墙：软删的行必须"先按行取、再判权限"。
    """
    rows = (
        await session.execute(
            select(KnowledgeBase)
            .where(
                KnowledgeBase.deleted_at.isnot(None),
                writable_kb_clause(ctx, model=KnowledgeBase),
            )
            .order_by(KnowledgeBase.deleted_at.desc())
        )
    ).scalars().all()
    return [KbOut.model_validate(r) for r in rows]


@router.post(
    "",
    response_model=KbOut,
    status_code=status.HTTP_201_CREATED,
    summary="创建知识库",
)
async def create_kb(
    req: CreateKbReq, session: DbSession, ctx: CurrentUser
) -> KbOut:
    # 必须是我所属的工作区 —— 否则能把 KB 建到不存在的/别人的工作区里
    await assert_workspace_access(session, ctx, req.workspace_id)

    if req.chunk_overlap >= req.chunk_size:
        raise InvalidRequest(
            f"chunk_overlap({req.chunk_overlap}) 必须小于 chunk_size({req.chunk_size})"
        )

    kb = KnowledgeBase(
        workspace_id=req.workspace_id,
        name=req.name,
        description=req.description,
        visibility=req.visibility,
        source_kind=req.source_kind,
        embed_model=get_settings().text_embed_model,  # D22：全局统一，不可按 KB 改
        chunk_size=req.chunk_size,
        chunk_overlap=req.chunk_overlap,
        chunk_strategy=req.chunk_strategy,
        generate_image_desc=req.generate_image_desc,
        created_by=ctx.user_id,
    )
    session.add(kb)
    await session.commit()
    return KbOut.model_validate(kb)


@router.get("/{kb_id}", response_model=KbDetail, summary="知识库详情（含统计）")
async def get_kb(kb_id: int, session: DbSession, ctx: CurrentUser) -> KbDetail:
    kb = (
        await session.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.id == kb_id, KnowledgeBase.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if kb is None or not ctx.can_read_kb(kb_id):
        from ...common.errors import ResourceNotFound

        raise ResourceNotFound(detail={"kb_id": kb_id})

    # ⚠️ 统计要只算 is_current 的文档。旧版本（D28 覆盖式更新后进回收站）
    # 还在表里，算进去会让"文档数"随覆盖次数虚增。
    docs = (
        await session.execute(
            select(
                func.count(Document.id),
                func.coalesce(func.sum(Document.size_bytes), 0),
            ).where(
                Document.kb_id == kb_id,
                Document.is_current.is_(True),
                Document.deleted_at.is_(None),
            )
        )
    ).one()
    chunks = (
        await session.execute(
            select(func.count(Chunk.id))
            .join(Document, Document.id == Chunk.doc_id)
            .where(
                Document.kb_id == kb_id,
                Document.is_current.is_(True),
                Document.deleted_at.is_(None),
            )
        )
    ).scalar_one()

    # 🔑 D181：还停在旧切分策略的文档数（"重摄取欠账"，对应文章坑三）。
    stale_strategy = (
        await session.execute(
            select(func.count(Document.id)).where(
                stale_strategy_condition(kb_id, kb.chunk_strategy)
            )
        )
    ).scalar_one()

    return KbDetail(
        **KbOut.model_validate(kb).model_dump(),
        doc_count=docs[0],
        chunk_count=chunks,
        size_bytes=int(docs[1]),
        stale_strategy_docs=int(stale_strategy),
    )


@router.patch("/{kb_id}", response_model=KbOut, summary="改知识库设置")
async def update_kb(
    kb_id: int, req: UpdateKbReq, session: DbSession, ctx: CurrentUser
) -> KbOut:
    kb = await assert_kb_write(session, ctx, kb_id)

    if req.name is not None:
        kb.name = req.name
    if req.description is not None:
        kb.description = req.description
    if req.visibility is not None:
        kb.visibility = req.visibility
    if req.generate_image_desc is not None:
        kb.generate_image_desc = req.generate_image_desc
    if req.pii_mask is not None and req.pii_mask != kb.pii_mask:
        kb.pii_mask = req.pii_mask
        # ⚠️ **不回溯已有 chunk**（D121）：掩码是"摄取时"这一步，
        #   改开关不会重切已有文档 —— 那要重跑整个索引。
        #   所以这里明确告诉调用方"只影响之后的摄取"，
        #   由前端在响应里提示"已有 N 份文档需要重新摄取才生效"
        #   （`GET /kb/{id}/pii` 里有那个 N）。
        logger.info(
            "知识库 %s 的 PII 掩码开关 → %s（**仅对之后的摄取生效**，"
            "已有文档需重新摄取）",
            kb_id, kb.pii_mask,
        )

    # 分块参数改了 → 已有 chunk 的切法就过时了。
    # 这里**不自动重跑**（一次全量重建可能要几分钟，不能让一个 PATCH 卡住），
    # 前端拿到 202 的语义后显式调 /documents/{id}/reparse。
    if req.chunk_size is not None:
        kb.chunk_size = req.chunk_size
    if req.chunk_overlap is not None:
        kb.chunk_overlap = req.chunk_overlap
    if (
        req.chunk_size is not None or req.chunk_overlap is not None
    ) and kb.chunk_overlap >= kb.chunk_size:
        raise InvalidRequest("chunk_overlap 必须小于 chunk_size")
    # D164：切分策略。与 chunk_size 同款 —— **只对之后重新摄取的文档生效**，
    # 不在这里自动重跑（同一份 KB 全量重建可能几分钟）。
    # ⚠️ `contextual` 需要外部 LLM（默认抽取式没有）—— 这里**不拦**，
    #   让摄取时按 `strategies` 的降级逻辑记一条 note 如实说明，
    #   而不是拒绝配置（配置可能是为"等 LLM 配好之后"）。
    if req.chunk_strategy is not None:
        kb.chunk_strategy = req.chunk_strategy
        logger.info(
            "知识库 %s 的切分策略 → %s（仅对之后重新摄取的文档生效）",
            kb_id, kb.chunk_strategy,
        )

    # 🔑 per-KB 精排开关（D87）。**这个字段之前只能靠手写 SQL 改**
    #   （`eval/api_1d_check.py` 就是 `UPDATE knowledge_bases SET rerank_model=…`），
    #   于是"按库开启精排"这个功能对真实用户**根本不存在**：
    #   读路径三处都读它（`/search`、`/ask`、MCP），**写路径一处都没有**。
    #
    # ⚠️ `""` 与 `null` 都归一成 `None`（= 不启用，D90）：
    #   留着 `""` 的话 `retrieval/rerank.py::model_name()` 会把它当成
    #   "配了模型名"去加载然后失败 —— `""` 从来不是"关"的意思，只是脏数据。
    if req.rerank_model is not None:
        kb.rerank_model = req.rerank_model.strip() or None
        logger.info("知识库 %s 的精排 → %s", kb_id, kb.rerank_model or "（关闭）")

    await session.commit()
    return KbOut.model_validate(kb)


@router.delete(
    "/{kb_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除知识库（软删除）"
)
async def delete_kb(kb_id: int, session: DbSession, ctx: CurrentUser) -> None:
    """软删除（D26：30 天回收站）。

    ⚠️ 这里**不动 Qdrant**。向量清理走 cleanup 任务（D44 旧版本向量留 3 天），
    在 API 里同步删 4600 个点会让 DELETE 请求卡好几秒。

    代价是"删掉的 KB 还能被搜到"最多 3 天 —— 但检索侧会二次过滤
    （`is_current` + `deleted_at`），所以对外表现是搜不到。
    """
    kb = await assert_kb_write(session, ctx, kb_id)
    kb.deleted_at = dt.datetime.now(dt.UTC)
    # 其下文档一并进回收站，否则 KB 恢复后文档状态不一致
    for doc in (
        await session.execute(
            select(Document).where(
                Document.kb_id == kb_id,
                Document.is_current.is_(True),
                Document.deleted_at.is_(None),
            )
        )
    ).scalars():
        doc.deleted_at = kb.deleted_at
        doc.is_current = False
    enqueue(session, KIND_CLEANUP, kb_id=kb_id, payload={"reason": "kb_deleted"})
    await session.commit()


@router.post(
    "/{kb_id}/restore", response_model=KbOut, summary="恢复知识库（撤销软删除）"
)
async def restore_kb(kb_id: int, session: DbSession, ctx: CurrentUser) -> KbOut:
    """恢复被软删的知识库，**连同它当时带走的那批文档**。

    🔑 为什么必须有这个端点（D26/D125）：`DELETE /kb/{id}` 把它下面的文档
    一并软删了，但**没有任何地方能把它们放回去** —— 于是"删库"在 API 上
    是**不可撤销**的，而 D26 承诺的是"30 天反悔窗口"。
    承诺了反悔窗口却没有它的删除，比明说不可撤销更糟：
    用户以为还有 29 天，结果发现数据早就没了。

    ⚠️ 鉴权要**先按行取、再判权限**：软删的行不在 `readable_kb_ids` 里
    （`load_user_context` 过滤 `deleted_at IS NULL`），所以
    `assert_kb_write` 会永远 403 —— 恢复功能等于不存在。
    仍然用 `can_write_kb`（private 只有创建者、workspace 要成员），
    失败与不存在**同码**（§0.4）。

    🔑 只恢复"**这次删除带走**"的文档，判据是 `deleted_at` **时间戳相等**：
    `delete_kb` 把同一个 `kb.deleted_at` 盖到所有文档上，
    所以那批文档恰好共享这个时间戳。
    ⚠️ 不能用 `<=`：回收站里那些**更早就被单独删掉**或
    **被同名新版本顶替**的文档也是 `deleted_at <= stamp`，
    它们是用户另外删的，不该跟着回来。
    """
    kb = (
        await session.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.id == kb_id,
                KnowledgeBase.deleted_at.isnot(None),
                # 🔴 D130：它的工作区还软删着 → 恢复不了（必须先恢复工作区）。
                # 与 `GET /kb/trash` 的可见性同源，否则会出现
                # "列表里没有、却能按 id 恢复"这种自相矛盾的状态。
                exists().where(
                    Workspace.id == KnowledgeBase.workspace_id,
                    Workspace.deleted_at.is_(None),
                ),
            )
        )
    ).scalar_one_or_none()
    if kb is None or not can_write_kb(ctx, kb):
        raise ResourceNotFound(detail={"kb_id": kb_id})

    stamp = kb.deleted_at
    kb.deleted_at = None
    rows = (
        await session.execute(
            select(Document).where(
                Document.kb_id == kb_id,
                Document.deleted_at == stamp,
            )
        )
    ).scalars().all()
    for doc in rows:
        doc.deleted_at = None
        doc.is_current = True
        doc.status = "pending"
        # ⚠️ 恢复后**必须重跑摄取**：D44 定了旧版本向量只留 3 天；
        #   即使还在，期间改过 `chunk_size` 的话旧切法也已经过时。
        #   与 `POST /documents/{id}/restore` 同一条理由、同一个入口。
        await enqueue_async(session, KIND_INGEST, doc_id=doc.id, kb_id=kb_id)
    logger.info("恢复知识库 %s，连带恢复 %s 份文档并重新摄取", kb_id, len(rows))
    await session.commit()
    return KbOut.model_validate(kb)
