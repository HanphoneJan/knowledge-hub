"""单文档摄取流水线（D35 双份存储的写入侧）。

```
原文件（blob）→ 解析 → 分块 → PG chunks 表 → Qdrant kb_dense / kb_sparse
```

## 为什么分两个阶段

`parse` 与 `chunk` 分开是因为**成本差 3 个数量级**：
- 解析 PDF：~40ms/页
- colSmol 编码：**15s/图**（视觉腿，实测 fp32；与分辨率无关，见 `MAX_SCAN_PAGES`）

文本腿可以同步做完（D46 要求检索段 < 500ms），视觉腿必须入队慢慢跑。
所以 `index` 阶段只写文本腿，`embed_visual` 是**独立任务 kind**。

## 一致性

Postgres 是真相源，Qdrant 是可重建派生索引（D7）：
- PG 写成功 → 写 Qdrant；Qdrant 失败 → 任务重试，PG 数据完好
- Qdrant 写成功 → 更新 `documents.status = ready`
- 删文档时 PG 先软删（对外立刻生效），Qdrant 由 cleanup 任务清（D44 留 3 天）
"""

from __future__ import annotations

import logging
import mimetypes
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

from ..common.config import get_settings
from ..common.jobs import (
    KIND_EMBED_VISUAL,
    KIND_PDF_PREVIEW,
    KIND_THUMBNAIL,
    enqueue,
)
from ..common.models import Chunk, Document, KnowledgeBase
from ..common.qdrant import (
    COLL_DENSE,
    COLL_SPARSE,
    point_id,
    sparse_vector,
    upsert_points,
)
from ..common.storage import blob_path
from ..retrieval import bm25
from ..retrieval.pool import encode_texts
from ..retrieval.terms import (
    TermRegistry,
    get_corpus_stats,
    register_chunks,
    unregister_chunks,
)
from . import chunker
from . import strategies
from .parsers import parse_to_chunks
from .validate import chunk_quality, validate_parse

logger = logging.getLogger(__name__)

_settings = get_settings()

#: D30 硬上限
MAX_PAGES = 2000
#: D37 扫描件上限
#:
#: ⚠️ 这个数字的依据是**实测单页成本**：colSmol 恒定约 13 个 512×512 图块
#:   ≈ **15s/图**（float32；与分辨率无关，见 `retrieval/pool.py::get_visual_model`），
#:   50 页 ≈ **12 分钟**。D37 当初按"66s/页"给的是 50 页 ≈ 55 分钟，
#:   现在的实测比那个数字快 4.6 倍 —— **上限因此变得更宽松**，
#:   而"成本是常数"（不是"某个会过时的秒数"）才是它继续成立的理由。
VDR_SECONDS_PER_PAGE = 15
MAX_SCAN_PAGES = 50

# ============================================================
# 进度回报（D14 的"进度回传"在文本腿上的落地）
# ============================================================

#: 每个阶段结束时上报的进度值。
#:
#: ⚠️ 这些是**阶段权重，不是实测耗时占比** —— 别拿它们做耗时预测。
#: 定成这样的形状是因为实测（15 页 PDF、74 chunk）：
#: 解析 ~0.6s、分块 ~0.2s、**编码 ~6.5s**（88ms/chunk × 74）。
#: 编码占大头，所以给它最宽的区间，进度条才不会在最后一段卡住不动。
#:
#: 视觉腿（colSmol 15s/图）是**另一个 kind**，它按页上报，不走这里。
STAGE_PROGRESS = {
    "start": 0.02,
    "parsed": 0.35,
    "chunked": 0.45,
    "indexed": 0.97,
}
#: 编码阶段占 `[EMBED_FROM, EMBED_TO]`，每批编码完上报一次
EMBED_FROM = 0.45
EMBED_TO = 0.97
#: 一批编码多少 chunk。批太大 → 中间点太少（进度条又变成两跳），
#: 批太小 → `encode_texts` 的批处理开销被放大（实测 CPU 上 batch>1 无收益）。
EMBED_BATCH = 16


def _can_render_pages(path: Path, file_name: str) -> bool:
    """这份文件能不能渲染出页面图（缩略图/页图的前提）。

    ⚠️ 判格式看 **`file_name`** 而不是 `path.suffix` —— blob 路径是
    `{sha}/original`，没有扩展名（内容寻址存储刻意不保留原文件名）。
    看错会导致**所有**文档都被判成"渲染不了"，缩略图永远不入队。
    （同一个根因炸过两处，见 CONVENTIONS §0.8。）
    """
    del path  # 只看文件名，但签名留 path 是为了调用方直观看出判据
    return file_name.lower().endswith(".pdf")


def embed_fraction(done: int, total: int) -> float:
    """编码阶段的进度：`done/total` 映射到 `[EMBED_FROM, EMBED_TO]`。

    抽成纯函数是为了能单测 —— "进度是否真的在动"这件事在端到端里
    **测不准**：轮询间隔与编码耗时的关系决定了能采到几个中间点，
    采样不到就分不清"没有中间点"和"没采到"。
    """
    if total <= 0:
        return EMBED_FROM
    ratio = min(max(done / total, 0.0), 1.0)
    return EMBED_FROM + (EMBED_TO - EMBED_FROM) * ratio


class IngestError(Exception):
    """摄取失败。`retryable` 决定任务是否重试。

    区分可重试与不可重试很重要：D38 定了"能降级就降级"，
    但"文件超过 50MB"重试一万次也是 50MB。
    """

    def __init__(self, message: str, *, retryable: bool = False, code: str = "INGEST_FAILED"):
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.code = code


@dataclass
class IngestResult:
    doc_id: int
    chunk_count: int
    elapsed_ms: int
    scan_pages: list[int]
    is_scanned: bool


# ============================================================
# 主入口
# ============================================================


def ingest_document(
    session,
    doc_id: int,
    *,
    queue_visual: bool | None = None,
    progress: Callable[[float], None] | None = None,
) -> IngestResult:
    """把一个文档从 blob 走到 Qdrant。

    调用方（worker）负责事务边界与任务状态更新。

    @param queue_visual 要不要给扫描页/照片入队 `embed_visual`。
        **不传 = 跟 `VDR_ENABLED` 配置走**（默认开，D124）。
        🔑 显式 `False` 是"这份不入队"（离线脚本按需控制）；
        而 `VDR_ENABLED=false` 是**运维决定**：worker 根本不加载 colSmol
        （省 2.5GB 与 68s/页），扫描件与照片就**只有文本腿或什么都没有**。
        这不是静默降级 —— 关掉时文档状态直接 `ready`，
        `/admin/consistency` 的 `kb_visual` 腿也会照实报"PG 说该有、Qdrant 没有"。

    @param progress 阶段进度回调，收到 0~1 的浮点。
        **每报一次都是一次独立事务**（worker 侧的 `heartbeat`），
        所以别在回调里做重活，也不要假设它一定被调用。
        不传就完全不回报（离线脚本 `pipeline/` 走的就是这条路）。

    @raises IngestError 解析/编码失败
    """
    t0 = time.perf_counter()
    if queue_visual is None:
        queue_visual = _settings.vdr_enabled


    def report(value: float) -> None:
        if progress is not None:
            progress(value)

    report(STAGE_PROGRESS["start"])
    doc = session.get(Document, doc_id)
    if doc is None:
        raise IngestError(f"文档 {doc_id} 不存在", retryable=False)
    kb = session.get(KnowledgeBase, doc.kb_id)
    if kb is None:
        raise IngestError(f"知识库 {doc.kb_id} 不存在", retryable=False)

    path = blob_path(doc.blob_sha)
    if not path.exists():
        raise IngestError(
            f"blob 缺失: {path}（内容寻址存储与 DB 不一致）", retryable=False
        )

    # ---- 1. 解析 ----
    doc.status = "parsing"
    session.flush()

    # ⚠️ 解析器按**扩展名**分派，而 blob 路径是 `{sha}/original` —— 没有扩展名。
    # 所以必须显式传 `documents.file_name`（原始文件名，含扩展名）。
    #
    # 踩过的坑：直接用 `path.suffix` → 每个 blob 都是"无扩展名" →
    # worker 一律报 `UnsupportedFormat: 不支持的格式: (无扩展名)`。
    # **扩展名/mime 的真相在 DB 行里，不在文件系统里** ——
    # 内容寻址存储刻意不保留原文件名（这是它去重的前提）。
    parsed, scan_pages, is_scanned = parse_to_chunks(
        path,
        file_name=doc.file_name,
        mime_type=doc.mime_type,
        chunk_size=kb.chunk_size,
        chunk_overlap=kb.chunk_overlap,
    )

    # 导入的聊天切片是**渲染出来的 markdown**，解析器看不出它是聊天
    # （只会给 `kind="text"`）。但 D41/M7 要求它们是 `kind="chat"` ——
    # `routing.py` 的 chat 意图靠 `prefer_kinds={"chat"}` 生效，
    # 全是 `text` 的话那条规则**永远匹配不到**，还会留下一句
    # "库里没有 chat 类结果，不做过滤"。
    # ⚠️ 判据用 `doc.doc_source`（DB 行里的事实），不看文件名 ——
    #   内容寻址存储的 blob 路径没有扩展名（§0.8）。
    if doc.doc_source == "chat_import":
        for c in parsed:
            c.kind = "chat"

    if is_scanned and len(scan_pages) > MAX_SCAN_PAGES:
        raise IngestError(
            f"扫描件 {len(scan_pages)} 页，超过 D37 上限 {MAX_SCAN_PAGES} 页。"
            f"视觉编码要 {len(scan_pages) * VDR_SECONDS_PER_PAGE / 60:.0f} 分钟，不值得。",
            retryable=False,
            code="TOO_MANY_SCAN_PAGES",
        )

    # ---- 1a. 切分策略（D164）----
    # ⚠️ 策略在**解析之后、掩码之前**应用：语义/父块/上下文都作用于"内容"，
    #   而 PII 掩码要作用在最终要入库的文本上（改了策略不改掩码顺序的语义）。
    full_text: str | None = None
    encoding = "standard"
    late_spans: list[tuple[int, int]] | None = None
    strategy_notes: list[str] = []
    if parsed and kb.chunk_strategy and kb.chunk_strategy != strategies.DEFAULT_STRATEGY:
        full_text = "\n\n".join(c.text for c in parsed)
        from .contextual import get_contextualizer

        # 🔑 D170：只有 contextual 才需要外部 LLM，按需读库配置
        #   （其它策略不需要，避免每次摄取多一次无关查询）。
        llm_cfg = None
        if kb.chunk_strategy == "contextual":
            from ..retrieval.llm import load_llm_config_sync

            llm_cfg = load_llm_config_sync(session)

        result = strategies.apply_strategy(
            parsed,
            strategy=kb.chunk_strategy,
            context=strategies.ChunkContext(
                full_text=full_text,
                size=kb.chunk_size,
                overlap=kb.chunk_overlap,
                embed=encode_texts,
                llm=get_contextualizer(llm_cfg),
                is_pdf=doc.file_name.lower().endswith(".pdf"),
            ),
        )
        parsed = result.chunks
        encoding = result.encoding
        strategy_notes = list(result.notes)
        if encoding == "late":
            full_text = full_text if full_text is not None else "\n\n".join(
                c.text for c in parsed
            )
            late_spans = strategies.spans_for(full_text, parsed)

    # ---- 1b'. 解析校验（D166）----
    # 🔑 空内容**不再静默 ready**：文本格式解析出 0 个 chunk 是"真的没内容/解析失败"，
    #   与"扫描件没有文本层、靠视觉腿"是两件事。后者 `scan_pages` 非空，放行。
    if not parsed:
        if not (is_scanned or scan_pages):
            raise IngestError(
                f"{doc.file_name} 解析后没有任何可检索内容"
                "（不是扫描件，也没有文本层）—— 请检查文件本身",
                retryable=False,
                code="PARSE_EMPTY",
            )
        parse_report: dict[str, Any] = {"skipped": "扫描件/照片，无文本内容，走视觉腿"}
    else:
        joined = full_text if full_text is not None else "\n".join(c.text for c in parsed)
        report_obj = validate_parse(joined)
        parse_report = report_obj.to_dict()
        if report_obj.fatal:
            raise IngestError(
                f"{doc.file_name} 的解析结果为空：{'；'.join(report_obj.issues)}",
                retryable=False,
                code="PARSE_EMPTY",
            )
        if report_obj.issues:
            logger.warning(
                "文档 %s 解析体检有问题：%s", doc_id, "；".join(report_obj.issues)
            )

    # ---- 1b. PII 检出 +（按库开关）掩码（D121）----
    # ⚠️ **只掩索引侧**：blob 里的原件、`/preview` 页图、`/documents/{id}/file`
    #   全部保持原文。所以掩码不会让人拿不回原件，
    #   但也意味着**开了掩码就搜不到自己的手机号**（D121 的取舍）。
    # 🔑 先检出**再**掩码：计数必须记掩码前的，
    #   否则报告永远显示"0 处 PII"，用户会以为库是干净的。
    pii_counts, pii_total = _detect_pii(parsed)
    doc.pii_hits = {"total": pii_total, "by_type": pii_counts}
    if kb.pii_mask and pii_total:
        _mask_chunks(parsed)

    doc.page_count = scan_pages and max(scan_pages) or _count_pages(path)
    doc.is_scanned = is_scanned
    # 🔑 D124：**落库**视觉腿该有哪些页。它是页级索引在 PG 侧唯一的真相来源 ——
    #   `is_scanned` 只说"这份要走视觉路径"，说不出"该有几个点"，
    #   而没有它 `/admin/consistency` 就没法对账视觉腿（那正是 D123 烂了
    #   这么久都没人发现的根因：只有两条腿被对账覆盖）。
    doc.scan_pages = scan_pages
    session.flush()
    report(STAGE_PROGRESS["parsed"])

    if not parsed:
        # 纯扫描件 / 照片：没有文本 chunk，全靠视觉腿
        doc.quality = {"parse": parse_report, "chunking": {"total": 0},
                       "strategy_notes": strategy_notes}
        if queue_visual and scan_pages:
            for pg in scan_pages:
                enqueue(
                    session, KIND_EMBED_VISUAL,
                    doc_id=doc_id, kb_id=doc.kb_id,
                    payload={"page": pg},
                )
        # 🔑 D124：**入队了视觉任务才 `pending`**，等 `handle_visual`
        #   把这一页页都编码完再置 `ready`（见 worker handler）。
        #   ⚠️ 原来这里无条件 `pending`，而视觉任务失败时**没有任何地方**
        #     把它改回 ready —— 文档就永远停在"处理中"，
        #     前端 `KbDetailPage` 的 `pending` 判据会一直轮询下去。
        #   `VDR_ENABLED=false`（不跑 colSmol）时更要直接 ready：
        #     没有待办的任务就不该显示"处理中"。
        doc.status = "pending" if (queue_visual and scan_pages) else "ready"
        doc.chunk_count = 0
        session.flush()

        # 扫描件同样要缩略图（D36 的原话："扫描件页面也是图片，
        # 用户能直接看到表格长什么样"）
        if queue_visual and _can_render_pages(path, doc.file_name):
            enqueue(
                session, KIND_THUMBNAIL,
                doc_id=doc_id, kb_id=doc.kb_id,
                payload={"page_count": doc.page_count or len(scan_pages) or 1},
            )
        report(STAGE_PROGRESS["indexed"])
        return IngestResult(
            doc_id=doc_id, chunk_count=0,
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
            scan_pages=scan_pages, is_scanned=is_scanned,
        )

    # ---- 2. 分块（写 PG）----
    doc.status = "chunking"
    chunks = chunker.assign_indices(parsed)
    quality = chunk_quality(chunks)
    doc.quality = {
        "parse": parse_report,
        "chunking": quality.to_dict(),
        "strategy": kb.chunk_strategy,
        "encoding": encoding,
        "strategy_notes": strategy_notes,
    }
    if not quality.acceptable:
        logger.warning(
            "文档 %s 的 chunk 质量有问题：%s", doc_id, "；".join(quality.errors)
        )
    _replace_chunks(session, doc_id, chunks)
    doc.chunk_count = len(chunks)
    session.flush()
    report(STAGE_PROGRESS["chunked"])

    # ---- 3. 嵌入 + 写 Qdrant 文本腿 ----
    doc.status = "embedding"
    session.flush()
    _index_text_legs(
        session, doc, kb, chunks, report=report,
        encoding=encoding, full_text=full_text, spans=late_spans,
    )

    # ---- 4. 后续任务 ----
    if queue_visual and scan_pages:
        for pg in scan_pages:
            enqueue(
                session, KIND_EMBED_VISUAL,
                doc_id=doc_id, kb_id=doc.kb_id,
                payload={"page": pg},
            )

    # Office 预览（D34 v1.1）：转换是**外部进程**（LibreOffice，实测 0.7s/份，
    # 但 300 页 pptx 可能 30s+），所以放独立任务 ——
    # 放在 ingest 里同步做会让进度"卡在 90% 不动"，而 /jobs 分不清在转还是在编码。
    # ⚠️ **不要加 `if doc.status == "ready"`**（第一版加了，从未成立）：
    #   `doc.status = "ready"` 在下面几行才赋值，那个守卫是**死的**，
    #   于是没有任何文档派生出预览任务 —— 症状是"功能写好了但没反应"，
    #   和 D36 的"handler 从没人入队"是同一类（第 1 版的注释还写着
    #   "handler 写好了，永远不会被调用"，我又踩了一次同样的形状）。
    #   走到这里本身就是摄取成功：纯扫描件那条路径在上面就 return 了。
    try:
        from .office import needs_conversion

        if needs_conversion(doc.file_name, doc.mime_type):
            enqueue(session, KIND_PDF_PREVIEW, doc_id=doc.id, kb_id=doc.kb_id)
    except (ImportError, OSError) as exc:
        # ⚠️ **只吞这两类**：第一版写 `except Exception`，结果把
        #   `NameError: KIND_PDF_PREVIEW is not defined`（我忘了导入那个常量）
        #   也吞成一条 warning —— 日志里一行、摄取照常完成、预览永远不生成。
        #   宽 except 会把"我写错了"伪装成"环境问题"，这正是它的危险之处。
        logger.warning("判断是否需要 Office 预览转换失败（doc=%s）：%s", doc.id, exc)
        raise

    # 缩略图（D36）：入库时预生成，宽 400px
    # ⚠️ 之前**没有任何地方给 KIND_THUMBNAIL 入队** —— handler 写好了，
    # 永远不会被调用，`data/blobs/*/thumbs/` 一张图都没有。
    # 只给 PDF/图片文档入队：其它格式渲染不出页面（render_page 只认 PDF）。
    if queue_visual and _can_render_pages(path, doc.file_name):
        enqueue(
            session, KIND_THUMBNAIL,
            doc_id=doc_id, kb_id=doc.kb_id,
            payload={"page_count": doc.page_count or 1},
        )

    doc.status = "ready"
    session.flush()

    return IngestResult(
        doc_id=doc_id,
        chunk_count=len(chunks),
        elapsed_ms=int((time.perf_counter() - t0) * 1000),
        scan_pages=scan_pages,
        is_scanned=is_scanned,
    )


def _detect_pii(chunks: list[chunker.Chunk]) -> tuple[dict[str, int], int]:
    """全文检出 PII（🔴 **不掩码**，只数）。

    ⚠️ 按 chunk 逐个检测而不是拼成整篇：拼接后一个跨 chunk 边界的
    号码（前半在 chunk 3、后半在 chunk 4）会检不出来，
    而掩码本身也只发生在 chunk 内（chunk 已经是索引的最小单位）。
    这个"跨边界漏检"是已知取舍，写在这里而不是假装没有。
    """
    from .pii import summarize

    counts: dict[str, int] = {}
    total = 0
    for c in chunks:
        by_type, n = summarize(c.text)
        for k, v in by_type.items():
            counts[k] = counts.get(k, 0) + v
        total += n
    return counts, total


def _mask_chunks(chunks: list[chunker.Chunk]) -> int:
    """就地掩码 chunk 文本，返回被改动的 chunk 数。

    ⚠️ 就地改而不是返回新对象：chunks 随后会被
    `_replace_chunks` 写进 PG、被 `_index_text_legs` 送进 Qdrant ——
    在这里换引用，就必须把两个调用点一起改，而漏一个就意味着
    "PG 里是掩码后的、向量库是原文的"，那是最难发现的泄露。
    """
    from .pii import mask_text

    changed = 0
    for c in chunks:
        masked, hits = mask_text(c.text)
        if hits:
            c.text = masked
            changed += 1
    return changed


def _count_pages(path: Path) -> int:
    try:
        import pymupdf

        d = pymupdf.open(path)
        try:
            return d.page_count
        finally:
            d.close()
    except Exception:  # noqa: BLE001
        return 0


def _replace_chunks(session, doc_id: int, chunks: list[chunker.Chunk]) -> None:
    """重写文档的 chunk 行。

    覆盖式更新（D28）时旧 chunk 会一起没 —— 否则 chunk_index 会错位，
    而 `chunk_index` 参与 Qdrant 点 ID 的计算（D33）。
    """
    old = session.execute(
        select(Chunk).where(Chunk.doc_id == doc_id)
    ).scalars().all()
    if old:
        unregister_chunks(session, [tokenize_row(c.content_norm) for c in old])
        session.execute(delete(Chunk).where(Chunk.doc_id == doc_id))
        session.flush()

    session.add_all(
        [
            Chunk(
                doc_id=doc_id,
                chunk_index=i,
                content_norm=c.text,
                char_start=c.char_start,
                char_end=c.char_end,
                locations=c.locations or None,
                token_count=c.token_count,
                kind=c.kind,
                table_index=c.table_index,
                row_count=c.row_count,
                col_count=c.col_count,
                table_json=c.table_json,
                parent_text=c.parent_text,
                section_path=c.section_path,
            )
            for i, c in enumerate(chunks)
        ]
    )
    session.flush()


def tokenize_row(text: str) -> list[str]:
    return bm25.tokenize(text)


def _index_text_legs(
    session, doc, kb, chunks: list[chunker.Chunk],
    report: Callable[[float], None] | None = None,
    *,
    index_terms: bool = True,
    encoding: str = "standard",
    full_text: str | None = None,
    spans: list[tuple[int, int]] | None = None,
    #: 🔑 D193：写入哪个物理集合。默认逻辑名（别名模式下解析成当前物理集合）。
    #: 蓝绿重建时传影子集合名（`kb_dense__<gen>`），实现"先建新的、再切别名"。
    dense_collection: str = COLL_DENSE,
    sparse_collection: str = COLL_SPARSE,
) -> None:
    """写 dense + sparse 两条腿。

    @param index_terms False 表示词表已经登记过（本批 chunk）。
        **必须**关掉：`register_chunks` 是"加 df"，重复调用会把 df 算成双倍
        （`_df_delta` 只加不减，见 §5.29），而 df 翻倍的后果是 idf 变负、
        检索结果被噪声淹没且**不报错**。重建索引时走的就是这条路径。

    ⚠️ 两腿的点 ID **必须一致**（`point_id(kb, doc, idx)`），
    RRF 融合就是靠 ID 对齐的。

    ⚠️ 编码**分批**做（`EMBED_BATCH`）不是为了省内存，是为了让进度条能动 ——
    一次性编 74 个 chunk 要 6.5 秒，其间前端什么都看不到。
    分批不改变输出：`encode_texts` 是纯函数，同一批文本的向量与顺序无关。
    """
    texts = [c.text for c in chunks]
    doc_tokens = [bm25.tokenize(t) for t in texts]

    # BM25 词表增量登记（必须先做，拿到 term_id 才能算权重）
    if index_terms:
        reg = register_chunks(session, doc_tokens)
    else:
        # 重建路径：词表由 handle_reindex 为**全体**活 chunk 登记过了
        reg = TermRegistry(session)
        reg.ensure_loaded({t for toks in doc_tokens for t in toks})
        reg.flush()
    n_docs, avgdl = get_corpus_stats(session)

    all_terms = {t for toks in doc_tokens for t in toks}
    dfs_by_id = reg.dfs_by_id(all_terms)

    # ---- dense（分批编码 + 逐批上报）----
    total = len(texts)
    vectors: list[list[float]] = []
    if encoding == "late" and total:
        # 🔑 Late Chunking：整篇过一次 Transformer、按 span 池化。
        #   `full_text`/`spans` 没传（**重建索引**就是这条路径）时按入库时的
        #   口径重建：`"\n\n".join(chunk texts)` —— late 不改块文本，所以
        #   重建出来的全文与入库时**逐字一致**，向量也就一致。
        late_full = full_text if full_text is not None else "\n\n".join(texts)
        late_spans = spans if spans is not None else strategies.spans_for(late_full, chunks)
        try:
            from ..retrieval.pool import encode_late_spans

            vectors = encode_late_spans(late_full, late_spans)
            if report is not None:
                report(EMBED_TO)
        except Exception:  # noqa: BLE001
            logger.warning(
                "Late Chunking 编码失败，退回标准编码（doc=%s）", doc.id, exc_info=True
            )
            vectors = []
    if not vectors:
        for start in range(0, total, EMBED_BATCH):
            vectors.extend(encode_texts(texts[start : start + EMBED_BATCH]))
            if report is not None:
                report(embed_fraction(min(start + EMBED_BATCH, total), total))

    points = [
        {
            "id": point_id(kb.id, doc.id, i),
            "vector": vectors[i],
            "payload": chunker.build_payload(
                c,
                chunk_index=i,
                kb_id=kb.id,
                doc_id=doc.id,
                workspace_id=kb.workspace_id,
                visibility=kb.visibility,
                source_kind=kb.source_kind,
                owner_id=kb.created_by,
                file_name=doc.file_name,
                mime_type=doc.mime_type,
                updated_at=doc.updated_at.isoformat() if doc.updated_at else None,
            ),
        }
        for i, c in enumerate(chunks)
    ]
    upsert_points(dense_collection, points)
    # D191：记下"dense 腿是用哪个模型建的"——查询前据此校验 Embedding 一致性。
    # 🔑 放在"真的写完 dense 向量"之后：只有这里能保证记录与编码同源。
    from ..common.index_models import record_current_text_model

    record_current_text_model(session)

    # ---- sparse ----
    sparse_points = [
        {
            "id": point_id(kb.id, doc.id, i),
            "vector": sparse_vector(
                bm25.sparse_to_dict(
                    doc_tokens[i], reg.ids, dfs_by_id, n_docs, avgdl
                )
            ),
            "payload": points[i]["payload"],
        }
        for i in range(len(chunks))
    ]
    upsert_points(sparse_collection, sparse_points)


# ============================================================
# 删除时的反向操作
# ============================================================


def remove_from_indexes(kb_id: int, doc_id: int) -> int:
    """把一个文档的点从 Qdrant 两个文本 collection 删掉。

    视觉腿的点 ID 是 `point_id_for_page`，也要按 filter 清。
    """
    from ..common.qdrant import delete_by_filter

    flt = {"must": [{"key": "doc_id", "match": {"value": doc_id}}]}
    for coll in (COLL_DENSE, COLL_SPARSE):
        delete_by_filter(coll, flt)
    # kb_visual 用的是 point_id_for_page，同样带 doc_id payload
    from ..common.qdrant import COLL_VISUAL

    delete_by_filter(COLL_VISUAL, flt)
    return doc_id
