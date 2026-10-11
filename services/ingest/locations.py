"""L3 引用坐标提取（D33 / D55~D60）。

把 PDF 切成**带精确坐标**的 chunk，让前端能画框高亮引用位置。

方案经过 `eval/l3_probe_v2.py` 预研验证（结论见 `DESIGN-HISTORY.md §3`）：
- 归一化坐标往返误差 `0.00e+00 point`，与 DPI 完全无关
- 表格整张一个 chunk，locations 精确到每个非空单元格（带 row/col）
- 正文按段落切，locations 恰好 1 个框
- **不跨页合并**（跨页会让点引用时画出满屏框）
- 成本 8~12ms/页

⚠️ 四个必须知道的坑（都在预研里踩过，改这段代码前先读）：
1. 画框前要把归一化坐标乘回 PDF **point** 空间，不是像素
2. 段落切分不能用全局行距常数（表格 4pt vs 正文 13.1pt，任何常数都会对一种失效）
3. 段落合并必须**整页收集行后统一做** —— 这类 PDF 每行是一个独立 block
4. 表格框只能取 `tbl.rows[i].cells`，`tbl.cells` 因合并单元格会错位
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pymupdf

logger = logging.getLogger(__name__)

ChunkKind = Literal["text", "table", "page", "chat"]

#: 单个 chunk 最多多少个框（D59）。超出防前端卡死。
MAX_BOXES_PER_CHUNK = 200

#: 判定"缩进"的最小量（point）。低于此值视为浮点噪声，不算缩进。
MIN_INDENT = 4.0

#: 段落起点的编号前缀：`1.` `2、` `一、` `（1）`
LIST_PREFIX = re.compile(
    r"^\s*(?:\d+\s*[.、)]|[一二三四五六七八九十]+\s*[、.]|[（(]\s*[一二三四五六七八九十\d]+\s*[）)])"
)

#: 句末标点（上一行以它结尾且下一行缩进不浅 → 可断段）
SENTENCE_END = "。！？.!?；;：:"


@dataclass
class ParsedChunk:
    """一个带坐标的 chunk。

    字段与 `services/common/models.Chunk` 对应（D58）：
    - `text` → `content_norm`
    - `locations` → JSONB 里的坐标数组
    """

    text: str
    kind: ChunkKind = "text"
    locations: list[dict[str, Any]] = field(default_factory=list)
    page: int = 1
    page_count: int = 1
    #: 表格专用
    table_index: int | None = None
    row_count: int | None = None
    col_count: int | None = None
    #: 在归一化 markdown/全文里的字符偏移（可选，便于引用高亮）
    char_start: int | None = None
    char_end: int | None = None

    @property
    def token_estimate(self) -> int:
        """粗略 token 数。中文约 1 字 ≈ 1.5 token，这里保守乘 1.5。"""
        return int(len(self.text) * 1.5)


@dataclass
class ParsedDoc:
    """整个文档的解析结果。"""

    chunks: list[ParsedChunk]
    page_count: int
    #: True 表示**整个文档**无文本层（扫描件），locations 不可用，只能高亮整页
    is_scanned: bool = False
    #: 扫描页码列表（1-based）。混合文档里既有文本页也有扫描页
    scan_pages: list[int] = field(default_factory=list)
    #: 文本字符数为 0 的页（真正的空白页）。**不是**扫描页 —— 页面上什么都没有
    blank_pages: list[int] = field(default_factory=list)


# ============================================================
# 坐标工具
# ============================================================


def has_locations(raw: Any) -> bool:  # noqa: ANN401 — JSONB 的值，什么都可能是
    """这份 chunk **真的**有坐标吗？

    🔴 **必须看内容，不能看 `locations IS NULL`**（D131 实测踩了三次）：
    这一列是 `JSONB`，而 SQLAlchemy 在 `locations=[...]` 时不会替你判断
    "空列表"，于是**存进去的常常是 JSON `null` 而不是 SQL `NULL`**：

    ```
    chunk_index | locations IS NULL | jsonb_typeof(locations)
    0           | f                 | 'null'
    ```

    用 `IS NULL` 去判"这份要不要补坐标"，结果一条都筛不出来
    （补坐标的 handler 静默补了 0 个）；用 `IS NOT NULL` 去判"能不能画框"，
    结果放行了一份**没有框**的 chunk —— 症状是"框画不出来"而不是报错。

    ⚠️ 为什么不能改成 SQL 侧判断（`jsonb_typeof(col) != 'array'` 之类）：
    `jsonb_array_length` 作用在标量上会**抛错**（而不是返回 NULL），
    而 `OR` 的短路 PostgreSQL 并不保证 —— 迟早有一天顺序变了就变成一个
    运行时错误。判断放在 Python 侧，每份文档几百行，代价可以忽略。
    """
    if not raw:                      # SQL NULL / None / {}
        return False
    if isinstance(raw, dict):        # 形状不对（历史脏数据），当没有
        return False
    if isinstance(raw, (list, tuple)):
        return any(isinstance(x, dict) and x.get("bbox") for x in raw)
    return bool(raw)


def normalize_bbox(bbox: tuple[float, float, float, float], page: Any) -> list[float]:
    """point 坐标 → 归一化到 0~1。

    归一化后与 DPI/分辨率无关（实测往返误差 0），前端任何缩放都能直接用。
    """
    x0, y0, x1, y1 = bbox
    w, h = page.rect.width, page.rect.height
    if w <= 0 or h <= 0:
        return [0.0, 0.0, 0.0, 0.0]
    return [
        _clamp(x0 / w), _clamp(y0 / h),
        _clamp(x1 / w), _clamp(y1 / h),
    ]


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, v))


def denormalize_bbox(bbox: list[float], page: Any) -> tuple[float, float, float, float]:
    """归一化 → point 坐标。

    ⚠️ pymupdf 的 `Shape.draw_rect()` 要的是 point，**不是像素**。
    直接传"归一化 × 像素宽"会让框整体下移约半行（预研踩过）。
    前端做同样的换算（pdf.js 里用 viewport）。
    """
    return (
        bbox[0] * page.rect.width,
        bbox[1] * page.rect.height,
        bbox[2] * page.rect.width,
        bbox[3] * page.rect.height,
    )


def union_bbox(boxes: list[tuple[float, float, float, float]]) -> tuple[float, ...]:
    xs0 = min(b[0] for b in boxes)
    ys0 = min(b[1] for b in boxes)
    xs1 = max(b[2] for b in boxes)
    ys1 = max(b[3] for b in boxes)
    return (xs0, ys0, xs1, ys1)


# ============================================================
# 阅读顺序（D167：分栏感知）
# ============================================================

#: 行数太少不分栏（避免小页面/单行段落被误判）。
MIN_LINES_FOR_COLUMNS = 8
#: 栏间空隙至少这么宽才认为有分栏（point）。
MIN_COLUMN_GAP = 15.0
#: 一栏至少要占这么多比例的行数（避免把少数缩进行误当一栏）。
MIN_COLUMN_SHARE = 0.2


def order_lines(page_lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按阅读顺序排行（D167）。

    单栏文档退化为 `(y, x)` 排序 —— 与 D167 之前的实现**逐字相同**
    （基线守卫）。双栏/多栏时先按"没有行跨越的竖直切线"切列，列内再排，
    避免把左栏结论接到右栏论据前面（文章 §PDF 多栏布局）。

    ⚠️ 判据用**行的水平区间**而不是 x0 直方图：缩进行仍然横跨左边界，
    所以"缩进"不会造出一条合法切线；真正的分栏之间才有空白竖带。
    """
    if len(page_lines) < MIN_LINES_FOR_COLUMNS:
        return sorted(page_lines, key=lambda d: (round(d["y0"], 1), d["x0"]))
    return _order_columns(page_lines)


def _order_columns(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(lines) < MIN_LINES_FOR_COLUMNS:
        return sorted(lines, key=lambda d: (round(d["y0"], 1), d["x0"]))

    cut, gap = _widest_vertical_cut(lines)
    if cut is None or gap < MIN_COLUMN_GAP:
        return sorted(lines, key=lambda d: (round(d["y0"], 1), d["x0"]))

    left = [l for l in lines if l["x1"] <= cut]
    right = [l for l in lines if l["x0"] >= cut]
    rest = [l for l in lines if l not in left and l not in right]
    if min(len(left), len(right)) < len(lines) * MIN_COLUMN_SHARE:
        return sorted(lines, key=lambda d: (round(d["y0"], 1), d["x0"]))
    # 左栏读完再读右栏；"rest"（横跨切线的行，通常是通栏标题）放到最后
    return _order_columns(left) + _order_columns(right) + _order_columns(rest)


def _widest_vertical_cut(lines: list[dict[str, Any]]) -> tuple[float | None, float]:
    """找一条**没有行水平跨越**的最宽竖直线。返回 `(x, 宽度)`。"""
    events: list[tuple[float, int]] = []
    for l in lines:
        events.append((l["x0"], 1))
        events.append((l["x1"], -1))
    events.sort()
    active = 0
    prev = events[0][0]
    best_x: float | None = None
    best = 0.0
    for x, delta in events:
        if active == 0 and x > prev:
            gap = x - prev
            if gap > best:
                best = gap
                best_x = (prev + x) / 2
        active += delta
        prev = x
    return best_x, best


def grid_to_markdown(rows: list[list[Any]]) -> str:
    """二维表 → Markdown 表（D169）。

    🔑 表格 chunk 的**文本**用行列结构（而不是 `\\n` 拼单元格）——
    否则列与列之间的语义关系在 embedding 里完全丢失（文章 §表格）。
    坐标（`locations` 的 row/col）不受影响，仍是逐单元格。
    """
    if not rows:
        return ""

    def cell(v: Any) -> str:
        return ("" if v is None else str(v)).replace("|", "\\|").replace("\n", " ").strip()

    grid = [[cell(c) for c in row] for row in rows]
    ncol = max((len(r) for r in grid), default=0)
    if ncol == 0:
        return ""
    grid = [r + [""] * (ncol - len(r)) for r in grid]

    header = grid[0]
    out = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join("---" for _ in header) + "|",
    ]
    out.extend("| " + " | ".join(r) + " |" for r in grid[1:])
    return "\n".join(out)


# ============================================================
# 段落切分（D56 / D57）
# ============================================================


def is_paragraph_start(prev: dict[str, Any], cur: dict[str, Any], base_x: float) -> bool:
    """判断 cur 是否是**新段落**的开头。

    只用相对量，不依赖任何全局常数：
    1. 编号前缀（`1.` `一、` `（1）`）→ 必断
    2. 缩进回退到接近本页顶格 → 断（最可靠的信号）
    3. 上一行以句末标点结尾且当前行缩进不浅 → 断

    @param base_x 本页"顶格位置"（该页所有行里最小的 x0）

    ⚠️ 不要加"垂直间隙 > N pt"这类判据：实测表格行距 4pt、正文 13.1pt，
    任何常数都会对其中一种失效。见 DESIGN-HISTORY.md §3。
    """
    if LIST_PREFIX.match(cur["text"]):
        return True

    cur_indent = cur["x0"] - base_x
    prev_indent = prev["x0"] - base_x

    # 缩进回退到接近顶格
    if cur_indent <= max(MIN_INDENT, prev_indent * 0.5):
        return True

    # 上一行说完了一句
    if prev["text"].rstrip()[-1:] in SENTENCE_END and cur_indent <= prev_indent:
        return True

    return False


# ============================================================
# 主入口
# ============================================================


def parse_pdf_with_locations(
    pdf_path: Path | str,
    *,
    max_boxes: int = MAX_BOXES_PER_CHUNK,
    text_threshold: int = 20,
) -> ParsedDoc:
    """解析 PDF → 带坐标的 chunk。

    @param text_threshold 每页文本字符数低于此值判定为"扫描页"，
        走视觉路径（D60：不产坐标，只能高亮整页）
    @param max_boxes 单 chunk 框数上限（D59）

    @raises ValueError 文件不存在或不是 PDF
    """
    path = Path(pdf_path)
    if not path.exists():
        raise ValueError(f"文件不存在: {path}")

    doc = pymupdf.open(path)
    try:
        return _parse_open_doc(doc, max_boxes=max_boxes, text_threshold=text_threshold)
    finally:
        doc.close()


def _parse_open_doc(
    doc: pymupdf.Document, *, max_boxes: int, text_threshold: int
) -> ParsedDoc:
    chunks: list[ParsedChunk] = []
    scan_pages: list[int] = []
    blank_pages: list[int] = []

    for pno in range(doc.page_count):
        page = doc[pno]
        if page.rect.width <= 0 or page.rect.height <= 0:
            continue

        raw_text = page.get_text().strip()
        if not raw_text:
            # 无文本层。要区分两种情况，语义完全不同：
            #   - **有位图** → 扫描页，必须走 colSmol 视觉路径（15s/图，D37）
            #   - **无位图** → 真空白页，跳过即可
            #
            # ⚠️ 别把空白页算进 scan_pages：上层会据此决定"整份走视觉路径"，
            #    而空白页送去 colSmol 是白烧 15s 算力、产出无意义的向量。
            #
            # ⚠️ 判定用 `get_images()` 而不是有没有图形对象 ——
            #    矢量图形（draw_rect/draw_circle）同样不产生文本层，
            #    但它不是扫描件，把它送去 colSmol 也是浪费。
            if page.get_images():
                scan_pages.append(pno + 1)
            else:
                blank_pages.append(pno + 1)
            continue
        if len(raw_text) < text_threshold:
            # 扫描页：几乎无文本层（可能有页码/水印残留）。
            # colSmol 只给视觉向量没有坐标（D60），只能高亮整页。
            scan_pages.append(pno + 1)
            continue

        try:
            page_chunks = _parse_page(doc, page, pno + 1, doc.page_count, max_boxes)
        except Exception:  # noqa: BLE001 — 单页失败不能中断整个文档
            logger.exception("第 %d 页解析失败，跳过", pno + 1)
            continue
        chunks.extend(page_chunks)

    content_pages = doc.page_count - len(blank_pages)
    # `is_scanned` = "整份文档没有可用文本层"。
    #
    # ⚠️ 阈值用 `>` 而不是 `>=`，且要求**一页文本层都没有**才算 True。
    # 混合文档（部分页扫描、部分页有文本）必须判 False ——
    # 文本页能出精确坐标（D55），只让扫描页走 colSmol 即可。
    # 之前用 `len(scan) >= content/2`，结果 2 页里 1 页扫描就被判成整份扫描件，
    # 上层据此对全部页走视觉路径 → 白烧 15s/图算力。
    return ParsedDoc(
        chunks=chunks,
        page_count=doc.page_count,
        is_scanned=bool(scan_pages) and content_pages > 0 and not chunks,
        scan_pages=scan_pages,
        blank_pages=blank_pages,
    )


def _parse_page(
    doc: pymupdf.Document,
    page: Any,
    pno: int,
    page_count: int,
    max_boxes: int,
) -> list[ParsedChunk]:
    """解析单页 → 表格 chunk + 段落 chunk。"""
    out: list[ParsedChunk] = []
    table_index = 0

    # ---- 1. 表格 ----
    table_rects: list[tuple[float, float, float, float]] = []
    try:
        found = page.find_tables()
    except Exception:  # noqa: BLE001 — 表格识别失败降级为纯文本
        logger.warning("第 %d 页表格识别失败", pno, exc_info=True)
        found = None

    if found:
        for tbl in found.tables:
            # _table_chunk 可能返回**多个** chunk（超框数上限时按行拆分，D59）
            produced = _table_chunk(page, tbl, pno, page_count, table_index, max_boxes)
            if produced:
                out.extend(produced)
                table_rects.append(tbl.bbox)
                table_index += 1

    # ---- 2. 正文段落 ----
    # ⚠️ 必须**整页收集行**再分段，不能只在 block 内做（D57）：
    # 实测这类 PDF 每行是一个独立 block，按 block 循环时跨 block 的续行合不到一起。
    page_lines: list[dict[str, Any]] = []
    for blk in page.get_text("dict")["blocks"]:
        if blk["type"] != 0:  # 0=text, 1=image
            continue
        for ln in blk["lines"]:
            txt = "".join(sp["text"] for sp in ln["spans"])
            if not txt.strip():
                continue
            x0, y0, x1, y1 = ln["bbox"]
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            # 落在表格矩形内的行已由表格 chunk 覆盖，跳过
            if any(
                tr[0] <= cx <= tr[2] and tr[1] <= cy <= tr[3]
                for tr in table_rects
            ):
                continue
            page_lines.append(
                {"text": txt, "x0": x0, "y0": y0, "y1": y1, "x1": x1, "bbox": ln["bbox"]}
            )

    if page_lines:
        # 视觉阅读顺序（D167）：分栏感知。单栏文档退化为"先 y 再 x"，
        # 与旧实现逐字相同；双栏才不会把左右栏同行穿插。
        page_lines = order_lines(page_lines)
        base_x = min(d["x0"] for d in page_lines)

        para: list[dict[str, Any]] = [page_lines[0]]
        for item in page_lines[1:]:
            if is_paragraph_start(para[-1], item, base_x):
                out.append(_text_chunk(para, page, pno, page_count))
                para = [item]
            else:
                para.append(item)
        if para:
            out.append(_text_chunk(para, page, pno, page_count))

    return out


def _text_chunk(
    lines: list[dict[str, Any]], page: Any, pno: int, page_count: int
) -> ParsedChunk:
    text = "".join(l["text"] for l in lines).strip()
    ub = union_bbox([l["bbox"] for l in lines])
    return ParsedChunk(
        text=text,
        kind="text",
        # 正文段落：恰好 1 个框（D55）
        locations=[{"page": pno, "bbox": [round(v, 4) for v in normalize_bbox(ub, page)]}],
        page=pno,
        page_count=page_count,
    )


def _table_chunk(
    page: Any,
    tbl: Any,
    pno: int,
    page_count: int,
    table_index: int,
    max_boxes: int,
) -> list[ParsedChunk]:
    """表格 → 一个或多个 chunk，locations 精确到每个非空单元格。

    正常情况返回 1 个；超过 `max_boxes` 框数上限时按行拆成多个（D59）。

    ⚠️ 框只能从 `tbl.rows[i].cells` 取（D58）：
    `tbl.cells` 是一维扁平列表，长度因合并单元格 ≠ row_count × col_count
    （实测 9×7=63 但只有 41 个）。按下标取会 TypeError 或**静默错位**。
    """
    try:
        grid = tbl.extract()
    except Exception:  # noqa: BLE001
        logger.warning("表格 %d 内容提取失败", table_index, exc_info=True)
        return []

    row_frames = getattr(tbl, "rows", [])
    locations: list[dict[str, Any]] = []
    # `(行号, 单元格文本)`。带行号是为了超框数上限时能按行精确拆（D59），
    # 纯文本列表无法可靠反推切点。
    parts: list[tuple[int, str]] = []

    for ri, row in enumerate(grid):
        for ci, cell in enumerate(row):
            txt = (cell or "").strip()
            if not txt:
                continue
            parts.append((ri, txt))
            if ri >= len(row_frames):
                continue
            cells_in_row = getattr(row_frames[ri], "cells", [])
            if ci >= len(cells_in_row):
                continue
            cb = cells_in_row[ci]
            if cb is None:  # 合并单元格占据的位置
                continue
            locations.append(
                {
                    "page": pno,
                    "bbox": [round(v, 4) for v in normalize_bbox(cb, page)],
                    "row": ri,
                    "col": ci,
                }
            )

    if not parts:
        return []

    # D59：框数上限。超了就**按行拆成多个 chunk**，而不是只留一个外框。
    #
    # 只留外框的方案实测有问题：一张 40×10 的表格触发上限后，整张表只剩 1 个框，
    # 前端只能高亮整个表格 —— 等于退化成"页级高亮"，L3 的精度全丢了。
    # 拆分的代价是同一张表变成多个 chunk，但每个仍能精确到行。
    if len(locations) > max_boxes:
        logger.info(
            "表格 %d 有 %d 个单元格，超过上限 %d，按行拆分",
            table_index, len(locations), max_boxes,
        )
        return _split_table(
            parts, locations, tbl, page, pno, page_count,
            table_index, max_boxes, grid=grid,
        )

    return [
        ParsedChunk(
            # D169：文本用 Markdown 表（行列结构），坐标仍是逐单元格。
            text=grid_to_markdown(grid),
            kind="table",
            locations=locations,
            page=pno,
            page_count=page_count,
            table_index=table_index,
            row_count=tbl.row_count,
            col_count=tbl.col_count,
        )
    ]


def _split_table(
    parts: list[tuple[int, str]],
    locations: list[dict[str, Any]],
    tbl: Any,
    page: Any,
    pno: int,
    page_count: int,
    table_index: int,
    max_boxes: int,
    grid: list[list[Any]] | None = None,
) -> list[ParsedChunk]:
    """把超限的表格按行分组拆成多个 chunk。

    每组框数不超过 `max_boxes`，且**按行边界切**（不切一半行），
    这样每组仍是一个完整的逻辑片段。

    @param parts `(行号, 单元格文本)` 列表。**必须带行号** —— 之前用纯文本列表
        按"累计框数"反推切点，实测 1099 + 1199 字 > 原文，是错的：
        空单元格被跳过时"第 n 个框"与"第 n 段文本"根本不对应。
        带行号后直接按行号取，每段文本与它的框精确对应。
    """
    by_row: dict[int, list[dict[str, Any]]] = {}
    for loc in locations:
        by_row.setdefault(loc["row"], []).append(loc)

    rows_sorted = sorted(by_row)
    groups: list[list[int]] = []  # 每组包含哪些行
    cur: list[int] = []
    cur_boxes = 0
    for r in rows_sorted:
        n = len(by_row[r])
        if cur and cur_boxes + n > max_boxes:
            groups.append(cur)
            cur, cur_boxes = [], 0
        cur.append(r)
        cur_boxes += n
    if cur:
        groups.append(cur)

    text_by_row: dict[int, list[str]] = {}
    for row_no, txt in parts:
        text_by_row.setdefault(row_no, []).append(txt)

    out: list[ParsedChunk] = []
    for rows in groups:
        rowset = set(rows)
        group_boxes = [b for r in rows for b in by_row[r]]
        # D169：每组写成 Markdown 表，并**重复表头**（拆分后每段仍可独立阅读）。
        if grid:
            sub = [grid[0]] + [grid[r] for r in rows if r < len(grid)]
            text = grid_to_markdown(sub)
        else:  # pragma: no cover — grid 正常一定有
            texts = [t for r in rows for t in text_by_row.get(r, [])]
            text = "\n".join(texts)
        out.append(
            ParsedChunk(
                text=text or "(空表格片段)",
                kind="table",
                locations=group_boxes,
                page=pno,
                page_count=page_count,
                table_index=table_index,
                row_count=len(rows),
                col_count=max((b["col"] for b in group_boxes), default=0) + 1,
            )
        )
        # rowset 只用于调试断言，确保没有丢行
        assert len(rowset) == len(rows)
    return out
