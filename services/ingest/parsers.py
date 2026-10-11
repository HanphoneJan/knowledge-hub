"""格式解析器：把各种文件变成带坐标的 chunk。

⚠️ **依赖方向硬约束**（`CONVENTIONS.md` §0.1）：
`services/` **不得** import `pipeline/`。所以解析器在这里重新实现，
而不是从 `pipeline/doc_parser/` import。`pipeline/` 只作为 CLI 包装调它。

`pipeline/doc_parser/backends/light_native.py` 的核心逻辑（各格式的提取）
已经搬到这里，并且额外产出**坐标**（D33）。

支持的格式与降级策略（D38：能降级就降级）：

| 格式 | 解析方式 | 有坐标 |
|---|---|---|
| PDF | pymupdf，表格 + 段落 | ✅ |
| DOCX | python-docx → 段落/表格（无坐标） | ❌ |
| XLSX | openpyxl → 每行一个 chunk | ❌ |
| PPTX | python-pptx → 每页幻灯片一个 chunk | ❌ |
| MD/TXT | 直接读，按标题+段落切 | ❌ |
| 图片 | 只走视觉路径，不产文本 chunk | — |
| 其它/损坏 | 抛 `IngestError` | — |
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from .chunker import Chunk, chunks_from_markdown, clean_text
from .locations import ParsedChunk, parse_pdf_with_locations

logger = logging.getLogger(__name__)

# 🔴 D140：`SUPPORTED_SUFFIXES` / `IMAGE_SUFFIXES` 现在由**文件末尾的派发表**
#    `PARSERS` 派生（见 `_derive_suffix_sets`），**不再手写第二份清单**。
#    以前它们在这里各写一份字面量，而 `parse_to_chunks` 又是第三条 if 链 ——
#    加一种格式要改三处，漏一处的后果分两种：
#    只改白名单 ⇒ 用户能上传，worker 在摄取时报 `UnsupportedFormat`（延迟失败，
#    而且报错在 job 上不在上传响应上）；只改分派 ⇒ 上传直接 415，用户莫名。
#    M4 承诺的正是"新格式**注册**即可，不改调度逻辑"。


class UnsupportedFormat(Exception):
    """不在白名单里的格式（15 端点应返回 415）。"""


def guess_mime(path: Path) -> str | None:
    import mimetypes

    mt, _ = mimetypes.guess_type(path.name)
    return mt


def parse_to_chunks(
    path: Path,
    *,
    file_name: str | None = None,
    mime_type: str | None = None,
    chunk_size: int = 500,
    chunk_overlap: int = 50,
) -> tuple[list[Chunk], list[int], bool]:
    """解析文件 → `(chunk 列表, 扫描页码, 是否扫描件)`。

    @param path blob 的实际路径（内容寻址，**没有有意义的扩展名**）
    @param file_name **原始文件名**（含扩展名）。格式分派以它为准。
    @raises UnsupportedFormat 格式不支持

    ⚠️ `path.suffix` **不能用** —— blob 路径是 `{sha}/original`，扩展名是空的。
    扩展名的真相在 `documents.file_name` 里（内容寻址存储刻意不保留原文件名，
    那是它去重的前提）。踩过：worker 全部报
    `UnsupportedFormat: 不支持的格式: (无扩展名)`。
    """
    suffix = Path(file_name or path.name).suffix.lower()

    # 🔑 查**派发表**，不再是一条 if 链（D140）。表在文件末尾 ——
    #   它引用下面那些解析函数，定义在它们之前会 NameError。
    #   Python 在**调用时**解析这个名字，所以这里的书写顺序没有问题。
    entry = PARSERS.get(suffix)
    if entry is None:
        raise UnsupportedFormat(f"不支持的格式: {suffix or '(无扩展名)'}")
    return entry.fn(path, chunk_size, chunk_overlap)


# ============================================================
# PDF —— 有坐标（D33）
# ============================================================


def _parse_pdf(path: Path, size: int, overlap: int):
    result = parse_pdf_with_locations(path)
    chunks = [
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
        for pc in result.chunks
        if pc.text.strip()
    ]
    return chunks, result.scan_pages, result.is_scanned


# ============================================================
# 文本类
# ============================================================


def _parse_text(path: Path, size: int, overlap: int) -> list[Chunk]:
    raw = _read_text(path)
    return chunks_from_markdown(clean_text(raw), size=size, overlap=overlap)


def _parse_csv(path: Path, size: int, overlap: int) -> list[Chunk]:
    """CSV → 每个数据组一个 chunk，**首行当表头**（与 XLSX 一致）。

    ⚠️ 以前 CSV 走 `_parse_text` 当纯文本，表头与列的对应关系在切分后丢失。
    D169 改成与 XLSX 同一套（保表头 + 对齐列），检索时"第几列"才连得起来。
    """
    import csv
    import io

    raw = _read_text(path)
    try:
        rows = list(csv.reader(io.StringIO(raw)))
    except csv.Error:
        # 解析失败退回纯文本（能降级就降级，D38）
        return chunks_from_markdown(clean_text(raw), size=size, overlap=overlap)
    return _rows_to_chunks(rows, size=size, kind="table", prefix=None)


def _parse_html(path: Path, size: int, overlap: int) -> list[Chunk]:
    """HTML → 按标题层级切（D169 新增格式，文章 §按文档结构切）。

    ⚠️ 不引入 bs4（重依赖）：标准库 `html.parser` 足够抽标题与正文，
    把 `h1~h6` 还原成 markdown 标题，下游 `chunks_from_markdown` 直接认。
    """
    raw = _read_text(path)
    md = _html_to_markdown(raw)
    return chunks_from_markdown(clean_text(md), size=size, overlap=overlap)


def _html_to_markdown(html: str) -> str:
    from html.parser import HTMLParser

    class _Extractor(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.parts: list[str] = []
            self._skip = 0

        def handle_starttag(self, tag, attrs):  # noqa: ANN001
            if tag in ("script", "style", "noscript"):
                self._skip += 1
                return
            if re.fullmatch(r"h[1-6]", tag):
                self.parts.append("\n\n" + "#" * int(tag[1]) + " ")
            elif tag in ("p", "div", "br", "li", "tr", "section", "article"):
                self.parts.append("\n")

        def handle_endtag(self, tag):  # noqa: ANN001
            if tag in ("script", "style", "noscript"):
                self._skip = max(0, self._skip - 1)
                return
            if re.fullmatch(r"h[1-6]", tag):
                self.parts.append("\n\n")
            elif tag in ("p", "div", "li", "tr", "section", "article"):
                self.parts.append("\n")

        def handle_data(self, data):  # noqa: ANN001
            if not self._skip:
                self.parts.append(data)

    p = _Extractor()
    p.feed(html or "")
    text = "".join(p.parts)
    # 折叠多余空白：连续空行留两个
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]*\n(\s*\n)+", "\n\n", text)
    return text.strip()


def _read_text(path: Path) -> str:
    """读文本，自动试几种编码。

    ⚠️ 中文文档里 GBK/GB18030 很常见（老 doc 转出来的、网页另存的），
    直接按 utf-8 读会抛 UnicodeDecodeError。
    """
    data = path.read_bytes()
    for enc in ("utf-8", "gb18030", "utf-16", "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    # latin-1 永远不会失败，作为兜底（可能产生乱码但不会崩）
    return data.decode("utf-8", errors="replace")


# ============================================================
# DOCX
# ============================================================


def _parse_docx(path: Path, size: int, overlap: int) -> list[Chunk]:
    """docx → chunk。

    ⚠️ **无坐标**（D34 排 v1.1）。当前只能给出段落文本，
    引用时定位到"第 N 段"而不是页码。要坐标需先转 PDF，
    而转换会改变排版 —— 前端要标注"预览为转换版本"。

    🔑 **D168：保留标题层级**。原来只抽纯文本，`Heading 1/2/3` 全丢了 ⇒
    进 `chunks_from_markdown` 后找不到任何标题，整篇当一个 section，
    章节边界与 `section_path` 一起消失（文章 §Word 标题层级）。
    现在按样式名生成 `#`×N —— 好处是**下游一行都不用改**：
    `chunks_from_markdown` 本来就认 markdown 标题。
    """
    import docx

    document = docx.Document(str(path))
    parts: list[str] = []
    # 段落 XML 元素 → Paragraph（为的是拿到 `.style.name`）
    para_by_el = {p._p: p for p in document.paragraphs}

    def para_text(el) -> str:
        return "".join(n.text or "" for n in el.iter() if n.tag.endswith("}t")).strip()

    def heading_level(el) -> int | None:
        p = para_by_el.get(el)
        if p is None or p.style is None:
            return None
        return _heading_level_from_name(getattr(p.style, "name", "") or "")

    # ⚠️ python-docx 不暴露块顺序，必须按 body 的 XML 顺序遍历，
    # 否则表格会全跑到段落后面或前面（light_native.py 里踩过）
    body = document.element.body
    for child in body.iterchildren():
        if child.tag.endswith("}p"):
            txt = para_text(child)
            if not txt:
                continue
            lvl = heading_level(child)
            parts.append(("#" * lvl + " " + txt) if lvl else txt)
        elif child.tag.endswith("}tbl"):
            rows = []
            for tr in child.iter():
                if tr.tag.endswith("}tr"):
                    rows.append(
                        [para_text(tc) for tc in tr.iter() if tc.tag.endswith("}tc")]
                    )
            if rows:
                parts.append(_md_table(rows))

    md = clean_text("\n\n".join(parts))
    return chunks_from_markdown(md, size=size, overlap=overlap)


#: `Heading 1` / `标题 1` / `Heading1` → 级别。锚点用样式名（Word 的落地方式）。
_HEADING_RE = re.compile(r"(?:heading|标题)\s*([1-6])", re.IGNORECASE)


def _heading_level_from_name(name: str) -> int | None:
    """样式名 → 标题级别（1~6），不是标题返回 `None`。

    ⚠️ 只看样式名，**不猜字号**：字号是继承来的、运行时未必拿得到，
    拿它当判据会时对时错（文章也提醒"样式未必可靠"，但字号更不可靠）。
    """
    m = _HEADING_RE.search(name or "")
    if m:
        return int(m.group(1))
    if (name or "").strip().lower() in ("title", "标题"):
        return 1
    return None


def _md_table(rows: list[list[str]]) -> str:
    """二维表 → Markdown 表（检索时表格内容才连得起来）。"""
    if not rows:
        return ""
    rows = [[(c or "").replace("|", "\\|").strip() for c in r] for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |",
           "|" + "|".join("---" for _ in rows[0]) + "|"]
    out.extend("| " + " | ".join(r) + " |" for r in rows[1:])
    return "\n".join(out)


# ============================================================
# XLSX
# ============================================================


def _parse_xlsx(path: Path, size: int, overlap: int) -> list[Chunk]:
    """xlsx → 每个工作表一个或多个 chunk。

    策略：**按行分组**，每组不超过 `size`，同时保留表头
    （表头是列名，缺了表格内容就失去了语义）。

    🔑 D169 修两处：
    1. **列按下标对齐**（原来表头/数值各自 `if v is not None` 过滤，
       中间有空单元格时列会错位 —— 表头 3 列、值却是另一个 3 列）；
    2. **chunk 文本带工作表名**，多 sheet 文档能分清来源。
    """
    import openpyxl

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    out: list[Chunk] = []
    try:
        for ws in wb.worksheets:
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
            rows = [r for r in rows if any(c is not None and str(c).strip() for c in r)]
            if len(rows) < 1:
                continue
            out.extend(
                _rows_to_chunks(
                    rows, size=size, kind="table", prefix=f"[工作表: {ws.title}]"
                )
            )
    finally:
        wb.close()
    return out


def _rows_to_chunks(
    rows: list[list[Any]],
    *,
    size: int,
    kind: str,
    prefix: str | None = None,
) -> list[Chunk]:
    """二维行 → chunk，**首行当表头并按列对齐**（XLSX / CSV 共用，D169）。

    ⚠️ 对齐是重点：每个单元格按**它所在的列下标**落位（`None` → 空占位），
    不再各自过滤 `None`。列数取"最宽一行"，短行补空。
    """
    if not rows:
        return []
    ncol = max((len(r) for r in rows), default=0)
    if ncol == 0:
        return []

    def cell(v: Any) -> str:
        return "" if v is None else str(v).strip()

    def aligned(row: list[Any]) -> str:
        cells = [cell(v) for v in row] + [""] * (ncol - len(row))
        return " | ".join(cells).rstrip(" |")

    header = aligned(rows[0])
    if not header.strip():
        return []

    head = f"{prefix}\n" if prefix else ""

    out: list[Chunk] = []
    buf: list[str] = []
    for row in rows[1:]:
        line = aligned(row)
        if not line.strip(" |"):
            continue
        buf.append(line)
        if sum(len(b) for b in buf) >= size:
            out.append(Chunk(text=head + header + "\n" + "\n".join(buf), kind=kind))
            buf = []
    if buf:
        out.append(Chunk(text=head + header + "\n" + "\n".join(buf), kind=kind))
    return out


# ============================================================
# PPTX
# ============================================================


def _parse_pptx(path: Path, size: int, overlap: int) -> list[Chunk]:
    """pptx → 每张幻灯片一个 chunk（超过 size 就再切）。"""
    from pptx import Presentation

    prs = Presentation(str(path))
    out: list[Chunk] = []
    for idx, slide in enumerate(prs.slides, start=1):
        parts: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                t = shape.text_frame.text.strip()
                if t:
                    parts.append(t)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    line = " | ".join(c.text.strip() for c in row.cells)
                    if line.strip():
                        parts.append(line)
        if not parts:
            continue
        text = "\n".join(parts)
        if len(text) <= size:
            out.append(Chunk(text=text, kind="text", page=idx))
        else:
            for piece in chunks_from_markdown(text, size=size, overlap=overlap):
                out.append(Chunk(text=piece.text, kind="text", page=idx))
    return out


# ============================================================
# 格式派发表（D140）—— **唯一**一份"支持哪些格式"的清单
# ============================================================
#
# 🔑 **加一种格式 = 在这里加一条**（M4 承诺的"新格式注册即可，不改调度逻辑"）：
# 上传白名单由它派生，分派也由它驱动，所以不会再出现
# "白名单里有了、解析器没有"（延迟到摄取才失败）这种漂移。


def _as_document_parser(fn):  # noqa: ANN001, ANN202
    """把"只返回 chunk 列表"的解析器包成统一形状 `(chunks, [], False)`。

    🔴 D140 第一版**漏了这一层**，症状是
    `ValueError: not enough values to unpack (expected 3, got 1)` ——
    `_parse_text` / `_parse_docx` / `_parse_xlsx` / `_parse_pptx` 都只返回
    `list[Chunk]`，以前是**调用点**给它们补的 `, [], False`，
    换成注册表之后那层不见了。
    ⇒ 派发表的契约是**一个形状**（三元组），适配器在**注册时**做归一，
    而不是留给每个调用点各自记着。
    """
    def _wrapped(path: Path, size: int, overlap: int):
        chunks = fn(path, size, overlap)
        # 🔑 归一的同时**断言**形状：万一某个解析器改成返回三元组，
        #   这里会立刻炸，而不是悄悄套成 `((a,b,c), [], False)`。
        assert isinstance(chunks, list), (
            f"{getattr(fn, '__name__', fn)} 应返回 list[Chunk]，"
            f"实际返回 {type(chunks).__name__} —— 别让它自己返回三元组"
        )
        return chunks, [], False

    _wrapped.unified = "wrapped"          # 供守卫识别（见 test_parser_registry）
    _wrapped.wrapped_from = getattr(fn, "__name__", str(fn))
    return _wrapped


def _parse_image(path: Path, size: int, overlap: int):
    """图片本身没有文本，走视觉路径（D31 可选生成图片描述）。

    ⚠️ 返回 `(chunks, scan_pages=[1], is_scanned=True)` —— 第三项就是
    "这是扫描件"的唯一判据，`PARSERS` 里靠 `scanned=True` 标出来，
    上传白名单与 `IMAGE_SUFFIXES` 都从**这个标记**派生。
    """
    return [], [1], True


class _Parser:
    """一条派发记录：`fn` 是 `(path, chunk_size, chunk_overlap) -> (chunks, pages, scanned)`。"""

    __slots__ = ("fn", "scanned")

    def __init__(self, fn, scanned: bool = False) -> None:  # noqa: ANN001
        self.fn = fn
        self.scanned = scanned

    def __repr__(self) -> str:  # pragma: no cover — 调试用
        return f"_Parser({getattr(self.fn, '__name__', self.fn)}, scanned={self.scanned})"


#: `_parse_pdf` 原生就返回 `(chunks, scan_pages, is_scanned)`，标一下，
#: 让守卫能统一检查"每一条派发记录都保证返回三元组"（见 test_parser_registry）。
_parse_pdf.unified = "native"    # type: ignore[attr-defined]
_parse_image.unified = "native"  # type: ignore[attr-defined]

#: 后缀（小写，含点）→ 派发记录。
#: 🔑 文档类一律过 `_as_document_parser` —— 它们只返回 `list[Chunk]`，
#:   由适配器补成统一形状（第一版漏了，D140 的真回归）。
PARSERS: dict[str, _Parser] = {
    ".pdf": _Parser(_parse_pdf),
    ".md": _Parser(_as_document_parser(_parse_text)),
    ".markdown": _Parser(_as_document_parser(_parse_text)),
    ".txt": _Parser(_as_document_parser(_parse_text)),
    ".csv": _Parser(_as_document_parser(_parse_csv)),
    ".html": _Parser(_as_document_parser(_parse_html)),
    ".htm": _Parser(_as_document_parser(_parse_html)),
    ".json": _Parser(_as_document_parser(_parse_text)),
    ".docx": _Parser(_as_document_parser(_parse_docx)),
    ".xlsx": _Parser(_as_document_parser(_parse_xlsx)),
    ".pptx": _Parser(_as_document_parser(_parse_pptx)),
    # 图片没有文本层：靠 `scanned=True` 进"视觉腿"那一档
    ".jpg": _Parser(_parse_image, scanned=True),
    ".jpeg": _Parser(_parse_image, scanned=True),
    ".png": _Parser(_parse_image, scanned=True),
    ".webp": _Parser(_parse_image, scanned=True),
    ".bmp": _Parser(_parse_image, scanned=True),
    ".tiff": _Parser(_parse_image, scanned=True),
}


def _derive_suffix_sets(parsers: dict) -> tuple[frozenset[str], frozenset[str]]:
    """从派发表推出 `(支持文档的扩展名, 图片扩展名)`。

    🔑 抽成函数是为了**可测**：测试能断言"当前这两个常量确实等于
    `_derive_suffix_sets(PARSERS)` 的结果" —— 那样"有人又手写了一份白名单"
    就会被抓住（而直接断言"常量等于某个字面量集合"抓不住）。
    """
    docs = frozenset(s for s, p in parsers.items() if not p.scanned)
    images = frozenset(s for s, p in parsers.items() if p.scanned)
    return docs, images


#: D45 白名单之外的一律拒绝上传（15 端点校验用）—— **派生，别手写**
SUPPORTED_SUFFIXES, IMAGE_SUFFIXES = _derive_suffix_sets(PARSERS)
