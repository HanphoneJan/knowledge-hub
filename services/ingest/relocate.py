"""把已存 chunk 的文本**定位**到 LibreOffice 转换版 PDF 的坐标上（D131）。

## 为什么是"定位"而不是"迁移坐标"

CONVENTIONS §6.10 写的是"Office 文档坐标：走 LibreOffice 转 PDF 后排版会变，
需重新评估"。评估的结论（`deploy/verify_l3_office.py`，真实 docx 实测）是：
**这个问题的前提就不对** ——

- `.docx/.xlsx/.pptx` 的解析器**根本不产坐标**（`parsers.py::_parse_docx`
  的 docstring 第一行就写着"⚠️ 无坐标（D34 排 v1.1）"）
  ⇒ "把**原件**坐标迁到转换版"没有东西可迁
- 而在转换版里，**已存的 chunk 文本几乎都能定位**（实测平均覆盖 99%，
  8 个 chunk 全部 ≥95%）

⇒ 所以坐标的正确来源是**转换版自己**：用户看到的页图就是那份 PDF，
文本与坐标来自**同一次渲染**，于是"重排导致框指错字"这个风险**不存在**
（D106 担心的正是它）。

## 判据：覆盖率不够就**不给框**

🔑 这是本模块最重要的一条：**宁可没有框，也不要错的框**
（D106 的原话："那比没有框更误导"）。
定位不到（或覆盖率太低）的 chunk 就**不写 locations** ——
前端对没有坐标的引用会退化成"高亮整页/不给框"，而不会指错地方。

## 为什么用贪心最长匹配而不是 `page.search_for`

`search_for` 只能查**一整段连续文本**；chunk 文本常常横跨多个段落
（解析器按字符数切），也可能横跨表格单元格的边界 ——
直接拿 chunk 的前 16 字去搜，实测命中率只有 50%（`verify_l3_office.py`
第一版就是这个数字，它量的是 needle 的失败，不是定位能力的上限）。

贪心的做法：逐位置取**能在任意一行里找到的最长片段**（40 字上限、8 字下限），
命中就吃掉它、继续往后。跳过定位不到的字，所以局部对不齐不影响整体。
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

#: 贪心匹配的最长/最短片段。⚠️ 下限 8 是实测出来的：6 字以下满页都是
#:   "党的"、"为了"这类片段，匹配到了也不知道是哪一句。
MAX_PIECE = 40
MIN_PIECE = 8

#: 🔑 覆盖率低于这个值就**不给框**（宁可没有，也不要错的）。
#: 实测那份 docx 是 96~100%，所以 0.6 是在"明显不可用"与
#: "个别字对不齐"之间留的余量。
MIN_COVERAGE = 0.6

#: 单个 chunk 最多写多少个框（与 `locations.py::MAX_BOXES_PER_CHUNK` 同量级，
#: 前端对多框有网格/外框两种画法，见 CONVENTIONS §6.2/§6.9）。
MAX_BOXES = 60


@dataclass(frozen=True)
class LineBox:
    """转换版里的一行。

    @param text **已归一化**的文本（去空白）—— 用它匹配，也用它建索引。
    @param bbox 归一化坐标（0~1），顺序 `[x0, y0, x1, y1]`。
    @param page 页号（1 起，与 `chunks.locations` 一致）。
    """

    text: str
    bbox: tuple[float, float, float, float]
    page: int


def normalize(text: str) -> str:
    """比较用的规范化：**去掉所有空白**。

    🔑 必须去空白：转换会重新折行、重排空格，而 chunk 文本来自原始排版。
    实测保留空白时覆盖率会掉到 50% 上下（同一份 docx）。
    """
    return "".join(text.split())


def _index_lines(lines: Sequence[LineBox]) -> dict[str, list[int]]:
    """`8 字片段 → 行下标` 的倒排索引（加速贪心）。

    🔑 只索引**最短片段长度**（8）这一个长度：更长片段从它的前 8 字查起，
    再在候选行里 `in` 验证 —— 少一层索引，命中率一样。
    """
    idx: dict[str, list[int]] = {}
    for i, ln in enumerate(lines):
        t = ln.text
        if len(t) < MIN_PIECE:
            continue
        for j in range(len(t) - MIN_PIECE + 1):
            idx.setdefault(t[j:j + MIN_PIECE], []).append(i)
    return idx


def relocate_text(
    text: str, lines: Sequence[LineBox], index: dict[str, list[int]] | None = None
) -> tuple[float, list[LineBox]]:
    """把一段文本定位到这些行上。返回 `(覆盖率, 命中的框，按出现顺序去重)`。

    ⚠️ 返回的框**可能来自多行**（chunk 横跨段落）—— 前端支持多框
    （表格是"网格"、正文是"多段各自画"），所以不需要合并成一个。
    """
    needle = normalize(text)
    n = len(needle)
    if n == 0 or not lines:
        return 0.0, []
    index = index if index is not None else _index_lines(lines)

    covered = 0
    boxes: list[LineBox] = []
    seen: set[tuple[float, ...]] = set()
    i = 0
    while i < n:
        placed = False
        for ln_len in range(min(MAX_PIECE, n - i), MIN_PIECE - 1, -1):
            piece = needle[i:i + ln_len]
            for li in index.get(piece[:MIN_PIECE], ()):
                line = lines[li]
                if piece not in line.text:
                    continue
                covered += ln_len
                key = tuple(line.bbox)
                if key not in seen:
                    seen.add(key)
                    boxes.append(line)
                i += ln_len
                placed = True
                break
            if placed:
                break
        if not placed:
            i += 1          # 这一字定位不到，跳过
    return covered / n, boxes


def relocate_chunks(
    chunks: Iterable[tuple[int, str]], lines: Sequence[LineBox]
) -> dict[int, list[dict]]:
    """`{chunk_index: [{"page": n, "bbox": [...]}]}` —— 只给**够格**的 chunk。

    @param chunks `(chunk_index, text)` 序列（顺序无所谓）。
    @param lines  转换版里的行（`LineBox`）。

    🔑 返回的**只有覆盖率够格的**（`MIN_COVERAGE`），其余**没有键** ——
    调用方据此"不写 locations"，而不是写一个空数组
    （空数组会让前端以为"有坐标但这次没画"，那是另一种误导）。
    """
    index = _index_lines(lines)
    out: dict[int, list[dict]] = {}
    for chunk_index, text in chunks:
        cov, boxes = relocate_text(text, lines, index)
        if cov < MIN_COVERAGE or not boxes:
            continue
        out[chunk_index] = [
            {"page": b.page, "bbox": [round(v, 6) for v in b.bbox]}
            for b in boxes[:MAX_BOXES]
        ]
    return out