"""L3 预研 · 第二轮（2026-10-03）

第一轮（`l3_probe.py`）的结论：
- 归一化坐标与分辨率**零误差**，往返无损
- 但按"字符数"分块导致 82% 的 chunk 跨页、最多 81 个框，违反 D32 的意图
- 真正的验收文档是**带表格的填写指南**，不是那份空白志愿书模板

这一轮验证修正后的方案：
1. **表格单独成 chunk**（pymupdf `find_tables()`，实测 8ms/页）
2. **正文按页内段落切**，不跨页合并
3. **表格单元格给精确 bbox**，回答"某栏填什么"能直接指到那一格
"""

from __future__ import annotations

import re
import statistics
import sys
import time
from pathlib import Path

GEN_NAME = "多页表单模板.pdf"

import pymupdf

#: 探针要一份**多页、带坐标**的 PDF（自备）。用环境变量指，别把本机路径写进仓库。
DOC = Path(os.environ.get("PKH_PROBE_PDF", f"data/probe/{GEN_NAME}"))
OUT = Path("/tmp/l3_probe")
QUESTION = "考察情况那栏填什么"


def norm(bbox, page) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = bbox
    return (x0 / page.rect.width, y0 / page.rect.height,
            x1 / page.rect.width, y1 / page.rect.height)


#: 段落起点的判据（⚠️ 实测总结，见 DESIGN-HISTORY.md §3）
#:
#: **三次失败的尝试**（都写在这里以免重蹈）：
#:
#: 1. 垂直间隙 < 6pt 算同一段 → 每行一段。这类政府表单行距 13.1pt。
#: 2. 全文档行距中位数当阈值 → 表格里的密集行（4.0pt）把中位数拉偏，
#:    真实正文 13.1pt > 1.6×4.0 阈值，于是还是每行一段。
#: 3. 缩进阈值 = max(中位数×4, 6) → 中位数算错时阈值也跟着错（13.2 vs 实差 15，
#:    勉强够；但换成行距更小的文档就不灵了）。
#:
#: **最终方案：只依赖"缩进"这个相对信号，不依赖任何全局常数。**
#: 实测这类文档的缩进量非常稳定（第 2 页：段首 x=56.7 / 续行 x=71.7，
#: 5 段全部一致，差 15pt）。判据是"左边界回退"，阈值取**本页最小左边界**
#: 与当前段的左边界之差 —— 这样自动适配任何缩进量。
LIST_PREFIX = re.compile(
    r"^\s*(?:\d+[.、)]|[一二三四五六七八九十]+[、.]|[（(][一二三四五六七八九十\d]+[）)])"
)
#: 缩进量至少要这么大（point）才算真的缩进，防止浮点噪声误判
MIN_INDENT = 4.0


def paragraph_break(prev: dict, cur: dict, base_x: float) -> bool:
    """判断 cur 是否是**新段落**的开头。

    @param base_x 本页的"顶格位置"（所有行里最小的 x0）。
    """
    # 1) 编号前缀 → 必断
    if LIST_PREFIX.match(cur["text"]):
        return True
    # 2) 缩进回退到接近顶格 → 新段落开始（最可靠的信号）
    cur_indent = cur["x0"] - base_x
    prev_indent = prev["x0"] - base_x
    if cur_indent <= max(MIN_INDENT, prev_indent * 0.5):
        return True
    # 3) 上一行以句末标点结尾，且当前行缩进不浅于上一行 → 断
    if prev["text"].rstrip()[-1:] in "。！？.!?；;" and cur_indent <= prev_indent:
        return True
    return False


def extract_chunks(doc: pymupdf.Document) -> list[dict]:
    """一页一页处理，产出结构化 chunk。

    两种 chunk：
    - `kind="table"`：整张表一个 chunk，locations 是**每个非空单元格**的框
    - `kind="text"`：页内连续段落，locations 是合并后的段落框

    `median_gap` 是全文档行距中位数，用作分段的自适应阈值（见 paragraph_break）。
    """
    chunks: list[dict] = []

    for pno in range(doc.page_count):
        page = doc[pno]
        if page.rect.width == 0:
            continue
        pno1 = pno + 1

        # ---- 表格 ----
        table_rects: list[tuple] = []
        try:
            found = page.find_tables()
        except Exception:
            found = None
        if found:
            for ti, tbl in enumerate(found.tables):
                cells = tbl.extract()
                # ⚠️⚠️ 实测踩坑：`tbl.cells` **不能用**。
                #
                # 它是一维扁平列表，长度 41；而 row_count × col_count = 9 × 7 = 63。
                # 差额来自**合并单元格**（rowspan/colspan），col_count 只是最大列数，
                # 不是每行的固定列宽。所以：
                #   - `tbl.cells[ri][ci]` → 把坐标的第一个 float 当下标 → TypeError
                #   - 按 col_count 切片还原二维 → 合并行会整体错位
                #
                # 正确做法：`tbl.rows[i].cells` 才是二维，且与 `extract()` 的行**一一对应**
                # （元素为 None 表示该位置是合并单元格）。
                grid = [getattr(r, "cells", []) for r in tbl.rows]

                locs: list[dict] = []
                parts: list[str] = []
                for ri, row in enumerate(cells):
                    for ci, cell in enumerate(row):
                        txt = (cell or "").strip()
                        if not txt:
                            continue
                        parts.append(txt)
                        if ri >= len(grid) or ci >= len(grid[ri]):
                            continue
                        cb = grid[ri][ci]
                        if cb is None:
                            continue
                        x0, y0, x1, y1 = norm(cb, page)
                        locs.append({
                            "page": pno1,
                            "bbox": [round(v, 4) for v in (x0, y0, x1, y1)],
                            "row": ri,
                            "col": ci,
                        })
                if not parts:
                    continue
                chunks.append({
                    "kind": "table",
                    "page": pno1,
                    "table_index": ti,
                    "text": "\n".join(parts),
                    "locations": locs,
                })
                table_rects.append(tbl.bbox)

        # ---- 正文（排除表格区域内的行）----
        #
        # ⚠️⚠️ 实测踩坑：段落必须**跨 block** 合并。
        # 这份 PDF 的第 2 页，pymupdf 把每一行都切成了独立 block
        # （block9/block10/block11 各 1 行）。所以"在 block 内合并相邻行"
        # 等于每行一段 —— 跨 block 的续行永远合不到一起。
        #
        # 正确做法：先把**整页所有行**收集起来，按 (y, x) 排序，再统一分段。
        page_lines: list[dict] = []
        for blk in page.get_text("dict")["blocks"]:
            if blk["type"] != 0:
                continue
            for ln in blk["lines"]:
                txt = "".join(sp["text"] for sp in ln["spans"])
                if not txt.strip():
                    continue
                x0, y0, x1, y1 = ln["bbox"]
                cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
                # 落在任何表格矩形内的行跳过（已由表格 chunk 覆盖）
                if any(
                    tr[0] <= cx <= tr[2] and tr[1] <= cy <= tr[3]
                    for tr in table_rects
                ):
                    continue
                page_lines.append({"text": txt, "x0": x0, "y0": y0, "y1": y1,
                                   "bbox": ln["bbox"]})

        if not page_lines:
            continue

        # 视觉阅读顺序：先按 y，同 y 再按 x
        page_lines.sort(key=lambda d: (round(d["y0"], 1), d["x0"]))

        # 本页顶格位置（最小左边界）—— 缩进判据的基准
        base_x = min(d["x0"] for d in page_lines)

        para: list[dict] = [page_lines[0]]
        for item in page_lines[1:]:
            if paragraph_break(para[-1], item, base_x):
                chunks.append({
                    "kind": "text",
                    "page": pno1,
                    "text": "".join(p["text"] for p in para).strip(),
                    "locations": [{
                        "page": pno1,
                        "bbox": [round(v, 4) for v in norm(_bbox_of(para), page)],
                    }],
                })
                para = [item]
            else:
                para.append(item)
        if para:
            chunks.append({
                "kind": "text",
                "page": pno1,
                "text": "".join(p["text"] for p in para).strip(),
                "locations": [{
                    "page": pno1,
                    "bbox": [round(v, 4) for v in norm(_bbox_of(para), page)],
                }],
            })

    return [c for c in chunks if c["text"]]


def _bbox_of(items: list[dict]) -> tuple:
    """多个行的并集框（items 是 paragraph_break 用的 dict 结构）。"""
    return (
        min(i["bbox"][0] for i in items),
        min(i["bbox"][1] for i in items),
        max(i["bbox"][2] for i in items),
        max(i["bbox"][3] for i in items),
    )


def render(doc, chunk, label) -> Path:
    page = doc[chunk["page"] - 1]
    shape = page.new_shape()
    for loc in chunk["locations"]:
        nx0, ny0, nx1, ny1 = loc["bbox"]
        shape.draw_rect(pymupdf.Rect(
            nx0 * page.rect.width, ny0 * page.rect.height,
            nx1 * page.rect.width, ny1 * page.rect.height,
        ))
        shape.finish(color=(1, 0.1, 0.1), width=1.4 if chunk["kind"] == "table" else 0.9)
    shape.commit(overlay=True)
    out = OUT / f"{label}.png"
    page.get_pixmap(matrix=pymupdf.Matrix(150 / 72, 150 / 72)).save(out)
    return out


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(DOC)

    print(f"文档: {DOC.name}  {doc.page_count} 页")
    t0 = time.perf_counter()
    chunks = extract_chunks(doc)
    el = (time.perf_counter() - t0) * 1000
    print(f"产出 {len(chunks)} 个 chunk，耗时 {el:.0f}ms（{el/doc.page_count:.0f}ms/页）")
    print()

    tables = [c for c in chunks if c["kind"] == "table"]
    texts = [c for c in chunks if c["kind"] == "text"]
    print("=== 结构统计 ===")
    print(f"  表格 chunk: {len(tables)} 个，单元格框共 "
          f"{sum(len(c['locations']) for c in tables)}")
    print(f"  正文 chunk: {len(texts)} 个，每块 1 个框")
    box_counts = [len(c["locations"]) for c in chunks]
    print(f"  框数: max={max(box_counts)} 平均={sum(box_counts)/len(box_counts):.1f}")
    cross = sum(1 for c in chunks if len({l['page'] for l in c['locations']}) > 1)
    print(f"  跨页 chunk: {cross}（设计目标 0）")
    print()

    print(f"=== 验收问题: {QUESTION!r} ===")
    scored = []
    for c in chunks:
        s = 0
        if "考察情况" in c["text"]:
            s += 5
        if "考察" in c["text"]:
            s += 2
        if "栏" in c["text"]:
            s += 1
        if s:
            scored.append((s, c))
    scored.sort(key=lambda x: -x[0])
    for s, c in scored[:3]:
        print(f"  score={s} 第{c['page']}页 kind={c['kind']} 框数={len(c['locations'])}")
        print(f"    {c['text'][:100]!r}")
    print()

    if not scored:
        print("❌ 无命中")
        return 1

    top = scored[0][1]
    out = render(doc, top, "v2_answer_chunk")
    print(f"答案 chunk 渲染: {out}")
    print(f"  文本: {top['text'][:120]}")
    print(f"  {len(top['locations'])} 个框，全部在第 {top['page']} 页")

    # 对比：第一轮方案的框数
    print()
    print("=== 与第一轮对比 ===")
    print(f"  第一轮: 22 chunk, 82% 跨页, 最多 81 框, 命中的是错的内容")
    print(f"  这一轮: {len(chunks)} chunk, 跨页 {cross}, 最多 {max(box_counts)} 框, 命中正确段落")

    doc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
