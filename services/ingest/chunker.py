"""分块与 chunk → Qdrant payload 的转换。

D35 双份存储的"归一化"侧在这里产生：`content_norm`（用于嵌入）
与 `text_raw`（引用展示用）可以是同一份，也可以不同。

⚠️ 与 L3 的关系：`ingest/locations.py` 已经把 PDF 切成了带坐标的
`ParsedChunk`。本模块负责：
1. 把非 PDF 格式（md / docx / html / 纯文本）转成 `ParsedChunk`
2. 把过长的 chunk 再切一刀（PDF 的段落已经很小，但 markdown 可能很大）
3. 组装成 `chunks` 表 + Qdrant payload 都能用的结构
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from .locations import ParsedChunk

logger = logging.getLogger(__name__)

#: 默认分块参数。KB 表里的 `chunk_size` / `chunk_overlap` 可以覆盖。
DEFAULT_CHUNK_SIZE = 500
DEFAULT_CHUNK_OVERLAP = 50

#: Markdown 标题 —— 分块的天然边界
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
#: 中文段落分隔（空行）
_PARA_SPLIT = re.compile(r"\n\s*\n")


@dataclass
class Chunk:
    """一个待入库的 chunk（尚未分配 chunk_index）。"""

    text: str
    kind: Literal["text", "table", "page", "chat"] = "text"
    locations: list[dict[str, Any]] = field(default_factory=list)
    page: int = 1
    table_index: int | None = None
    row_count: int | None = None
    col_count: int | None = None
    #: 该 chunk 在整份文档归一化文本里的字符偏移（可选）
    char_start: int | None = None
    char_end: int | None = None
    token_count: int | None = None
    #: D165 Parent-Child：父块上下文（章节 / PDF 整页）。
    #: 🔑 父块**不进向量库**（召回靠子块、上下文靠它），存 PG + payload 即可。
    parent_text: str | None = None
    #: D165 层级元数据：本文档里的章节路径（`H1 > H2 > H3`）。
    #: 文章说"被低估的手段"——检索时可按层级过滤、生成时能补回上下文。
    section_path: str | None = None


def from_rows(rows: list[Any]) -> list[Chunk]:
    """PG 的 `chunks` 行 → `chunker.Chunk`（重建索引时用）。

    ⚠️ 字段名不对齐：ORM 行是 `content_norm`，`chunker.Chunk` 是 `text`。
    直接把 ORM 行当 Chunk 传会 `AttributeError` —— 重建索引第一版就踩了。

    ⚠️ `text` 用**归一化文本**（`content_norm`）而不是原文：与入库时
    一致（`_index_text_legs` 收的就是解析器给出的归一化 chunk）。
    否则重建出来的向量与入库时的不同 —— 那就不是"重建"而是"改了语料"。
    """
    out: list[Chunk] = []
    for r in rows:
        locs = list(getattr(r, "locations", None) or [])
        out.append(
            Chunk(
                text=r.content_norm,
                kind=getattr(r, "kind", "text") or "text",
                locations=locs,
                page=(locs[0].get("page", 1) if locs else 1),
                table_index=getattr(r, "table_index", None),
                row_count=getattr(r, "row_count", None),
                col_count=getattr(r, "col_count", None),
                char_start=getattr(r, "char_start", None),
                char_end=getattr(r, "char_end", None),
                token_count=getattr(r, "token_count", None),
                # ⚠️ D165：重建索引也要带上父块与章节路径，否则"重建"会
                #    把 Parent-Child / 章节元数据一起丢掉（症状：重建后
                #    证据里突然没有父块上下文）。
                parent_text=getattr(r, "parent_text", None),
                section_path=getattr(r, "section_path", None),
            )
        )
    return out


def estimate_tokens(text: str) -> int:
    """粗略 token 数。中文约 1 字 ≈ 1.5 token（bge 的实际分词比例）。"""
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    other = len(text) - cjk
    return int(cjk * 1.5 + other * 0.3) + 1


# ============================================================
# 非 PDF 格式 → chunk
# ============================================================


def chunks_from_markdown(
    md: str, *, size: int = DEFAULT_CHUNK_SIZE, overlap: int = DEFAULT_CHUNK_OVERLAP
) -> list[Chunk]:
    """Markdown / 纯文本 → chunk。

    切分优先级：**标题 > 段落 > 硬切**。
    先按 markdown 标题分节，节内按空行分段，段还太长才硬切。
    这样每个 chunk 尽量是一个语义完整的单元，检索命中后可读性好。
    """
    md = (md or "").strip()
    if not md:
        return []

    out: list[Chunk] = []
    for path, heading, body in _split_sections(md):
        # 标题拼进正文 —— 否则搜"考察情况登记表"命中不到标题行
        text = f"{heading}\n{body}".strip() if heading else body
        for piece in _split_paragraphs(text, size, overlap):
            # D165：把章节路径写进块。默认策略下这只是**多一个字段**，
            # 文本/切分边界逐字不变（基线守卫）。
            out.append(Chunk(text=piece, kind="text", section_path=path or None))

    return [c for c in out if c.text.strip()]


def _split_sections(md: str) -> list[tuple[str, str, str]]:
    """按 markdown 标题切成 `(章节路径, 标题行, 正文)`。

    章节路径 = 标题栈拼成 `H1 > H2 > H3`（**不含** `#`）——
    它进 `chunk.section_path`，供 Parent-Child 分组与层级过滤。
    """
    matches = list(_MD_HEADING.finditer(md))
    if not matches:
        return [("", "", md)]

    sections: list[tuple[str, str, str]] = []
    # 第一个标题之前的内容（无章节路径）
    if matches[0].start() > 0:
        sections.append(("", "", md[: matches[0].start()].strip()))

    stack: list[tuple[int, str]] = []
    for i, m in enumerate(matches):
        level = len(m.group(1))
        title = m.group(2).strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        path = " > ".join(t for _, t in stack)
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md)
        sections.append((path, m.group(0), md[m.end() : end].strip()))
    return [(p, h, b) for p, h, b in sections if (h + b).strip()]


# ============================================================
# 清洗去噪（D166 / 文章 §清洗去噪）
# ============================================================

#: 页眉/页脚判定：同样的一行在整个文档里出现 ≥ 这么多次，且不长于
#: `_MAX_BOILERPLATE_LEN` → 视为模板噪声，删。阈值取 3 是保守值：
#: 重复两行很可能是正文里刻意的重复，**宁可留噪声也不删正文**。
_BOILERPLATE_MIN_REPEAT = 3
_MAX_BOILERPLATE_LEN = 40

#: 零宽 / 不可见控制字符（PDF 抽取常见）。
_INVISIBLE = dict.fromkeys(
    [*range(0x200B, 0x2010), 0xFEFF, 0x200E, 0x200F, 0x202A, 0x202B, 0x202C,
     0x202D, 0x202E, 0x00AD]
)


def clean_text(text: str) -> str:
    """清洗去噪（保守）。

    1. 去零宽/不可见控制字符；
    2. 三行以上的连续空行压成两行；
    3. 删**重复页眉页脚**（同文档出现 ≥3 次且短于 40 字）。

    ⚠️ 只在**高置信**时删：页眉页脚判据要求"重复次数"，不做"短行就删"这种
    会误伤正文标题的激进规则。返回清洗后的文本（不就地改）。
    """
    if not text:
        return text
    text = text.translate(_INVISIBLE)
    lines = text.split("\n")

    # 统计非空短行的出现次数
    counts: dict[str, int] = {}
    for ln in lines:
        s = ln.strip()
        if s and len(s) <= _MAX_BOILERPLATE_LEN:
            counts[s] = counts.get(s, 0) + 1
    boilerplate = {
        s for s, n in counts.items() if n >= _BOILERPLATE_MIN_REPEAT
    }

    out: list[str] = []
    blank = 0
    for ln in lines:
        s = ln.strip()
        if s in boilerplate:
            continue
        if not s:
            blank += 1
            if blank > 2:      # 超过两个连续空行就不再追加
                continue
        else:
            blank = 0
        out.append(ln)
    return "\n".join(out).strip()


def _split_paragraphs(text: str, size: int, overlap: int) -> list[str]:
    """段 → chunk。段内按字符硬切，带 overlap。"""
    paras = [p.strip() for p in _PARA_SPLIT.split(text) if p.strip()]
    if not paras:
        return []

    out: list[str] = []
    cur = ""
    for p in paras:
        if len(p) > size:
            # 这个段本身就超长 → 先把 cur 冲掉，再硬切它
            if cur:
                out.append(cur)
                cur = ""
            out.extend(_hard_split(p, size, overlap))
            continue
        if len(cur) + len(p) + 1 <= size:
            cur = f"{cur}\n{p}" if cur else p
        else:
            if cur:
                out.append(cur)
            cur = p
    if cur:
        out.append(cur)
    return out


def _hard_split(text: str, size: int, overlap: int) -> list[str]:
    """超长文本按字符硬切。

    尽量在标点处断开 —— 避免把一句话劈成两半，那会让引用体验很差。
    """
    if len(text) <= size:
        return [text]

    out: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            # 往后找一个标点作为切点（最多回退 size 的 20%）
            window = int(size * 0.2)
            best = -1
            for i in range(end, max(start + size - window, start + 1), -1):
                if text[i - 1] in "。！？；\n":
                    best = i
                    break
            if best > start:
                end = best
        piece = text[start:end].strip()
        if piece:
            out.append(piece)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return out


# ============================================================
# PDF chunk → 通用 chunk
# ============================================================


def chunks_from_parsed(parsed: list[ParsedChunk]) -> list[Chunk]:
    """`locations.parse_pdf_with_locations` 的产物 → `Chunk`。"""
    out: list[Chunk] = []
    for i, pc in enumerate(parsed):
        if not pc.text.strip():
            continue
        out.append(
            Chunk(
                text=pc.text,
                kind=pc.kind,
                locations=pc.locations,
                page=pc.page,
                table_index=pc.table_index,
                row_count=pc.row_count,
                col_count=pc.col_count,
                char_start=pc.char_start,
                char_end=pc.char_end,
            )
        )
    return out


# ============================================================
# 组装
# ============================================================


def assign_indices(chunks: list[Chunk]) -> list[Chunk]:
    """按 `chunk_index` 从 0 编号，并补 `token_count`。

    `chunk_index` 与 `(kb_id, doc_id, chunk_index)` 一起决定 Qdrant 点 ID（D33），
    所以**必须稳定**：同一个文档重复入库要得到同样的编号，否则
    "删索引再重建"会产生不同的点 ID。
    """
    for i, c in enumerate(chunks):
        c.token_count = estimate_tokens(c.text)
    return chunks


def build_payload(
    chunk: Chunk,
    *,
    chunk_index: int,
    kb_id: int,
    doc_id: int,
    workspace_id: int,
    visibility: str,
    source_kind: str,
    owner_id: int | None,
    file_name: str,
    mime_type: str | None,
) -> dict[str, Any]:
    """chunk → Qdrant payload。

    ⚠️ `kb_id` / `workspace_id` / `doc_id` / `source_kind` / `visibility`
    **必须**在 payload 里且**必须**建了 payload 索引 ——
    `build_qdrant_filter` 完全依赖 `kb_id` 过滤。缺一个就是全库扫描。
    """
    payload: dict[str, Any] = {
        "kb_id": kb_id,
        "doc_id": doc_id,
        "chunk_index": chunk_index,
        "workspace_id": workspace_id,
        "visibility": visibility,
        "source_kind": source_kind,
        "owner_id": owner_id,
        "file_name": file_name,
        "mime": mime_type,
        "kind": chunk.kind,
        "page": chunk.page,
        # 检索用（归一化文本）与引用展示用（原文片段）分开存，
        # 将来归一化规则变了可以只重建前者
        "text_norm": chunk.text,
        "text_raw": chunk.text,
    }
    if chunk.locations:
        payload["locations"] = chunk.locations
    if chunk.table_index is not None:
        payload["table_index"] = chunk.table_index
    # D165：父块上下文与章节路径。⚠️ **不建 payload 索引** —— 它们只随结果
    # 回传（`ask.to_evidences` 用父块做上下文），不参与过滤，建索引是浪费。
    if chunk.parent_text:
        payload["parent_text"] = chunk.parent_text
    if chunk.section_path:
        payload["section_path"] = chunk.section_path
    return payload
