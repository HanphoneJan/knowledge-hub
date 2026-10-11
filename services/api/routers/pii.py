"""🔑 PII 检出报告（D121 · M8）—— **只读，不改任何东西**。

## 一个端点回答两个不同的问题

| 问题 | 数据来源 | 为什么不能用同一个来源回答 |
|---|---|---|
| **我的原文里有几处敏感信息？** | `documents.pii_hits`（摄取时掩码**前**记下的） | 开了掩码后索引里是 0，但用户真正关心的是"原文有多少" |
| **索引里现在还剩几处？** | 现场扫 `chunks.content_norm` | 只看存储的计数无法验证"掩码到底生效没有" |

⚠️ 这两个数字**必然不同**（开了掩码就是 `原文 N / 索引 0`），
把它们混成一个数字是本项目最容易犯的错之一：
"掩码开着却显示 3 处" 会让用户以为掩码没生效而反复排查，
而真相是"这 3 处在**原件**里，索引里已经没有了"。

## 🔑 报告绝不返回原文片段

只给**计数 + 哪个文件**。给了片段，这份报告就成了新的泄露面 ——
而它的用途恰恰是"我要不要给这个库开掩码 / 要不要分享出去"。
（看内容是 `/kb/{id}/documents/{doc_id}/chunks` 的事，且要过 `assert_kb_read`。）

## 口径必须与真正掩掉的完全一致（`pii.py` 的去重）

现场扫描**不能**直接数 `detect_pii()` 的原始命中：
一串 18 位数字会同时命中身份证与银行卡，直接数就是"1 处数成 2 处"。
所以两处都走 `pii.summarize()`（内部经 `mask_text` 去重）——
**报告说掩了 2 处、实际只掩了 1 处**同样会让人怀疑掩码坏了。

## `indexed` 扫描为什么有上限

现场扫描要读全库的 chunk 文本。个人库动辄几 MB，一次请求扫 50 MB
会把 P99 从 20ms 变成 5s —— 所以**有字符上限 + 明确告知是否截断**。
⚠️ 宁可说"扫了前 4MB，还有更多没扫"，也不要假装扫完了：
一个"看起来完整其实只扫了 1%"的数字比截断提示危险得多。

## 与其它 KB 视图一致的地方

租户过滤走 `assert_kb_access`（§0.6 唯一入口），之后只用
`kb_id = :kb_id` 取数据，**不在这里重算可见性**。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from sqlalchemy import select

from ...common.auth import assert_kb_access
from ...common.deps import CurrentUser, DbSession
from ...common.errors import ResourceNotFound
from ...common.models import Chunk, Document, KnowledgeBase
from ...common.schemas_ops import PiiDocOut, PiiReport
from ...ingest.pii import summarize

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/kb", tags=["pii"])

#: 现场扫描的字符上限。⚠️ 超了就**如实说截断**，不静默少报。
SCAN_CHAR_LIMIT = 4_000_000
#: 文档明细最多列多少条（按命中数降序）。
DOC_LIMIT = 200


@router.get(
    "/{kb_id}/pii",
    response_model=PiiReport,
    summary="PII 检出报告（只读：原文里几处 / 索引里还剩几处）",
)
async def pii_report(kb_id: int, session: DbSession, ctx: CurrentUser) -> PiiReport:
    # 🔑 `assert_kb_access` **返回 None**（它只做断言），所以还要自己取一次
    #   KnowledgeBase 才知道开关状态。第一版把返回值当 KB 行用 →
    #   `NoneType has no attribute 'pii_mask'` → 整页 500。
    await assert_kb_access(session, ctx, kb_id)
    kb = (
        await session.execute(
            select(KnowledgeBase).where(KnowledgeBase.id == kb_id)
        )
    ).scalar_one_or_none()
    if kb is None:
        # 理论上断言已经过了；这里仍兜住（断言与查询之间被软删的竞态）
        raise ResourceNotFound(detail={"kb_id": kb_id})

    rows = (
        await session.execute(
            select(
                Document.id, Document.file_name, Document.pii_hits,
                Document.status, Document.chunk_count,
            )
            .where(Document.kb_id == kb_id, Document.deleted_at.is_(None))
            .order_by(Document.id)
        )
    ).all()

    docs: list[PiiDocOut] = []
    source_by_type: dict[str, int] = {}
    docs_with_pii = 0
    docs_not_scanned = 0
    docs_no_text = 0
    for doc_id, file_name, hits, status_, chunk_count in rows:
        # 🔴 **没有文本层的文档要单列**（照片 / 纯扫描件）。
        #   摄取里 `_detect_pii` 对它们返回 0（`parsed` 是空的），
        #   于是它们安静地贡献 0 —— 而那个 0 会被读成"这份文档没有 PII"。
        #   真相是"它压根没有文本可检测"：图片里的信息（身份证照、票据）
        #   要靠 OCR，而 OCR 不在这个功能的范围内（D121）。
        # ⚠️ 判据用 `chunk_count = 0` 而不是扩展名：一份解析出 0 个 chunk 的
        #   .txt 与一张照片是同一件事（没有文本层），用户要处理的方式也一样。
        if chunk_count == 0:
            docs_no_text += 1
        if hits is None:
            # NULL = 没跑过检测（还没摄取完：排队中 / 失败）。
            # ⚠️ 不等于"干净" —— 报告必须能区分这两者（D121）。
            docs_not_scanned += 1
            continue
        total = int(hits.get("total") or 0)
        if total == 0:
            continue
        docs_with_pii += 1
        by_type = {k: int(v) for k, v in (hits.get("by_type") or {}).items()}
        for k, v in by_type.items():
            source_by_type[k] = source_by_type.get(k, 0) + v
        docs.append(
            PiiDocOut(
                doc_id=doc_id, file_name=file_name, status=status_,
                total=total, by_type=by_type,
            )
        )

    # 🔑 先在**全量**上求和，再截断明细。
    #   反过来的话（先截断再求和）总数会偏小，
    #   而 `docs_truncated: true` 明摆着告诉用户"还有更多" ——
    #   用户会拿总数与明细条数对照，然后发现对不上。
    source_total = sum(d.total for d in docs)
    docs.sort(key=lambda d: (-d.total, d.doc_id))
    docs_truncated = len(docs) > DOC_LIMIT
    if docs_truncated:
        docs = docs[:DOC_LIMIT]

    indexed_total, indexed_by_type, scanned = await _scan_indexed(session, kb_id)

    return PiiReport(
        kb_id=kb_id,
        masking="on" if kb.pii_mask else "off",
        source_total=source_total,
        source_by_type=source_by_type,
        indexed_total=indexed_total,
        indexed_by_type=indexed_by_type,
        doc_count=len(rows),
        docs_with_pii=docs_with_pii,
        docs_not_scanned=docs_not_scanned,
        docs_no_text=docs_no_text,
        docs=docs,
        docs_truncated=docs_truncated,
        scan_chars=scanned,
        scan_truncated=scanned >= SCAN_CHAR_LIMIT,
    )


async def _scan_indexed(session, kb_id: int) -> tuple[int, dict[str, int], int]:
    """现场扫索引文本（`chunks.content_norm`），返回
    `(总数, 按类型, 已扫字符数)`。

    ⚠️ 只扫**用户可见**的文档（`deleted_at IS NULL` 且 `is_current`）：
    回收站里的旧版本不该算进"我的库现在还有几处 PII"。
    软删未 purge 的 chunk 仍在表里（D115 的 BM25 词表口径），
    但那是词表口径问题，与"用户可见的内容"无关 —— 这里按文档可见性过滤。
    """
    total = 0
    by_type: dict[str, int] = {}
    scanned = 0
    stmt = (
        select(Chunk.content_norm)
        .join(Document, Document.id == Chunk.doc_id)
        .where(
            Document.kb_id == kb_id,
            Document.deleted_at.is_(None),
            Document.is_current.is_(True),
        )
        .execution_options(yield_per=500)
    )
    stream = await session.stream(stmt)
    async for (text,) in stream:
        if not text:
            continue
        scanned += len(text)
        chunk_types, n = summarize(text)
        total += n
        for k, v in chunk_types.items():
            by_type[k] = by_type.get(k, 0) + v
        if scanned >= SCAN_CHAR_LIMIT:
            logger.info(
                "PII 报告：知识库 %s 的索引扫描达上限 %d 字符，返回截断",
                kb_id, SCAN_CHAR_LIMIT,
            )
            break
    return total, by_type, scanned