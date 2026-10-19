"""L3 引用坐标技术预研（D33 前置验证，2026-10-03）

目标：验证「chunk → 多个 bbox」的映射能否做到精确，够不够支撑阶段 1 的验收标准
（上传多页表单 PDF → 用一个自然语言问题搜到它 → 点引用画对框）。

这份脚本是**一次性的预研**，跑完的结论已写入 DESIGN-HISTORY.md §3。
正式实现要搬进 services/ingest/locations.py。
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# 🔴 探针 PDF / 验收问题 / 原文标记是**真实数据** ⇒ 从不被追踪的
#   `_local_data.py` 读（没有则退回合成示例）。
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:  # noqa: SIM105
    import _local_data as _LD
except ModuleNotFoundError:
    import _local_data_example as _LD

import pymupdf  # noqa: E402

PDF = Path(os.environ.get("PKH_PROBE_PDF", _LD.PROBE_PDF))
OUT = Path("/tmp/l3_probe")

# 验收用的真实问题
QUESTION = _LD.QUESTION


@dataclass
class Line:
    """一个文本行，带归一化坐标。"""

    page: int  # 1-based
    text: str
    bbox: tuple[float, float, float, float]  # 归一化 0~1
    pt_bbox: tuple[float, float, float, float]  # 原始 point

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]


@dataclass
class Chunk:
    text: str
    locations: list[dict] = field(default_factory=list)
    pages: set[int] = field(default_factory=set)


def extract_lines(doc: pymupdf.Document) -> list[Line]:
    """抽取全部文本行 + 归一化坐标。

    用 `dict` 而不是 `blocks`：`blocks` 会把视觉上相邻但逻辑无关的文字合成一块
    （实测第 5 页只有 3 个 block 却有 26 行），拿不到行级精度。
    """
    lines: list[Line] = []
    for pno in range(doc.page_count):
        page = doc[pno]
        W, H = page.rect.width, page.rect.height
        if W == 0 or H == 0:
            continue
        for blk in page.get_text("dict")["blocks"]:
            if blk["type"] != 0:
                continue
            for ln in blk["lines"]:
                # 逐 span 拼，span 之间可能没有空格
                text = "".join(sp["text"] for sp in ln["spans"])
                if not text.strip():
                    continue
                x0, y0, x1, y1 = ln["bbox"]
                lines.append(
                    Line(
                        page=pno + 1,
                        text=text,
                        bbox=(x0 / W, y0 / H, x1 / W, y1 / H),
                        pt_bbox=(x0, y0, x1, y1),
                    )
                )
    return lines


def merge_and_chunk(
    lines: list[Line], size: int = 500, overlap: int = 80
) -> list[Chunk]:
    """把行合并成 chunk，并记录每行对应的 bbox。

    合并规则：连续的行属于同一个段落（上一行末尾不是句末标点）就并进当前 chunk。
    跨页也继续并 —— 一个 chunk 可能横跨第 3~4 页。
    """
    chunks: list[Chunk] = []
    cur = Chunk(text="")
    cur_lines: list[Line] = []

    SENT_END = "。！？；.!?;：:"

    def flush() -> None:
        nonlocal cur, cur_lines
        if not cur_lines:
            return
        cur.text = "".join(l.text for l in cur_lines).strip()
        # 合并相邻同页且垂直连续的框，减少前端要画的框数
        cur.locations = _merge_boxes(cur_lines)
        cur.pages = {l.page for l in cur_lines}
        if cur.text:
            chunks.append(cur)
        # overlap：回退若干行做重叠
        if overlap > 0 and cur_lines:
            tail: list[Line] = []
            n = 0
            for l in reversed(cur_lines):
                tail.insert(0, l)
                n += len(l.text)
                if n >= overlap:
                    break
            cur_lines = tail
            cur = Chunk(text="")
        else:
            cur_lines = []
            cur = Chunk(text="")

    for ln in lines:
        cur_lines.append(ln)
        n = len(cur_lines[-1].text)
        total = sum(len(l.text) for l in cur_lines)
        ends_sentence = ln.text.rstrip()[-1:] in SENT_END
        if total >= size or ends_sentence:
            flush()
    flush()
    return chunks


def _merge_boxes(lines: list[Line], gap: float = 0.008) -> list[dict]:
    """把同一页内垂直相邻的框并成一个。

    gap 用归一化单位（0.008 ≈ 6pt），比行距小但比行高小 ——
    行距内的框合并，段落间的大空隙不合并。
    """
    out: list[dict] = []
    by_page: dict[int, list[Line]] = {}
    for l in lines:
        by_page.setdefault(l.page, []).append(l)

    for page in sorted(by_page):
        page_lines = sorted(by_page[page], key=lambda l: l.bbox[1])
        cur: list[float] | None = None
        for l in page_lines:
            x0, y0, x1, y1 = l.bbox
            if cur is None:
                cur = [x0, y0, x1, y1]
                continue
            # 垂直间隙小于阈值 且 水平有重叠 → 同一段落，合并
            v_gap = y0 - cur[3]
            h_overlap = min(cur[2], x1) - max(cur[0], x0)
            if 0 <= v_gap <= gap and h_overlap > 0:
                cur = [
                    min(cur[0], x0),
                    min(cur[1], y0),
                    max(cur[2], x1),
                    max(cur[3], y1),
                ]
            else:
                out.append({"page": page, "bbox": [round(v, 4) for v in cur]})
                cur = [x0, y0, x1, y1]
        if cur is not None:
            out.append({"page": page, "bbox": [round(v, 4) for v in cur]})
    return out


def render_verification(
    doc: pymupdf.Document, chunk: Chunk, label: str, page: int
) -> Path:
    """把 chunk 的所有框画到那一页上，渲染成图。人工确认是否框住了原文。"""
    p = doc[page - 1]
    pix = p.get_pixmap(matrix=pymupdf.Matrix(150 / 72, 150 / 72))
    W, H = pix.width, pix.height

    shape = p.new_shape()
    for loc in chunk.locations:
        if loc["page"] != page:
            continue
        nx0, ny0, nx1, ny1 = loc["bbox"]
        # 关键：shape 的坐标是 **PDF point**，不是像素。
        # 归一化要先乘回 point 空间，再由渲染矩阵放大。
        shape.draw_rect(
            pymupdf.Rect(nx0 * p.rect.width, ny0 * p.rect.height,
                         nx1 * p.rect.width, ny1 * p.rect.height)
        )
        shape.finish(color=(1, 0.1, 0.1), width=1.0)
    shape.commit(overlay=True)

    out = OUT / f"{label}.png"
    doc[page - 1].get_pixmap(
        matrix=pymupdf.Matrix(150 / 72, 150 / 72)
    ).save(out)
    return out


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(PDF)

    print(f"文档: {PDF.name}  {doc.page_count} 页")
    t0 = time.perf_counter()
    lines = extract_lines(doc)
    t_extract = time.perf_counter() - t0
    print(f"抽取 {len(lines)} 行，耗时 {t_extract * 1000:.0f}ms")

    t0 = time.perf_counter()
    chunks = merge_and_chunk(lines)
    t_chunk = time.perf_counter() - t0
    print(f"切成 {len(chunks)} 个 chunk，耗时 {t_chunk * 1000:.0f}ms")
    print()

    # ---- 统计：坐标质量 ----
    box_counts = [len(c.locations) for c in chunks]
    page_spans = [len(c.pages) for c in chunks]
    print("=== 坐标统计 ===")
    print(f"  每 chunk 框数: min={min(box_counts)} 中位="
          f"{sorted(box_counts)[len(box_counts)//2]} max={max(box_counts)} 平均="
          f"{sum(box_counts)/len(box_counts):.1f}")
    multi = sum(1 for n in page_spans if n > 1)
    print(f"  跨页 chunk: {multi}/{len(chunks)} ({multi/len(chunks)*100:.0f}%)")
    print(f"  无框 chunk: {sum(1 for n in box_counts if n == 0)}")
    print()

    # ---- 归一化往返无损验证 ----
    print("=== 归一化 → point 往返验证（全部框）===")
    worst = 0.0
    for c in chunks:
        for loc in c.locations:
            nx0, ny0, nx1, ny1 = loc["bbox"]
            back = (nx0, ny0, nx1, ny1)  # 乘回 point 后应等于 bbox/W
    # 直接比对存的值与重算值
    for c in chunks:
        for loc in c.locations:
            p = doc[loc["page"] - 1]
            nx0, ny0, nx1, ny1 = loc["bbox"]
            re_pt = (nx0 * p.rect.width, ny0 * p.rect.height,
                     nx1 * p.rect.width, ny1 * p.rect.height)
            worst = max(worst, max(abs(a - b) for a, b in zip(re_pt, loc["_pt"]))
                        if "_pt" in loc else worst)
    print(f"  最大误差 {worst:.2e} point（0 表示完全无损）")
    print()

    # ---- 真实问题（本地配置）----
    print(f"=== 验收问题实测: {QUESTION!r} ===")
    kw = [m for m in _LD.ANSWER_MARKERS]
    scored = []
    for c in chunks:
        score = sum(c.text.count(k) for k in kw)
        if score:
            scored.append((score, c))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        print("  ❌ 没有任何 chunk 命中关键词 —— 分块有问题")
        return 1
    for score, c in scored[:3]:
        print(f"  score={score} 页={sorted(c.pages)} 框数={len(c.locations)}")
        print(f"    {c.text[:90]!r}")
    print()

    top_score, top = scored[0]
    page = sorted(top.pages)[0]
    out = render_verification(doc, top, "verify_top_chunk", page)
    print(f"已渲染验证图: {out}  （chunk 跨 {sorted(top.pages)} 页，画的是第 {page} 页）")
    print(f"  该 chunk 在第 {page} 页有 {sum(1 for l in top.locations if l['page']==page)} 个框")

    # 也渲染最大框数的 chunk（最坏情况）
    worst_c = max(chunks, key=lambda c: len(c.locations))
    if worst_c is not top and worst_c.locations:
        wp = sorted(worst_c.pages)[0]
        out2 = render_verification(doc, worst_c, "verify_worst_chunk", wp)
        print(f"最坏情况（{len(worst_c.locations)} 框，跨 {sorted(worst_c.pages)} 页）: {out2}")

    doc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
