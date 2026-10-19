"""`services/ingest/locations.py` 的单元测试。

覆盖的是**预研里实际踩过的坑**（`DESIGN-HISTORY.md §3`），不是理想路径：

1. 归一化往返无损（与 DPI 无关）
2. ⚠️ 段落切分不能依赖全局行距常数
3. ⚠️ 分段必须跨 block 整页做
4. ⚠️ 表格框只能取 `tbl.rows[i].cells`
5. 空白页 ≠ 扫描页
6. 框数上限按行拆分后文本不丢
7. 旋转页坐标仍然对齐

跑法：`uv run pytest tests/ -v`
"""

from __future__ import annotations

import pymupdf
import pytest

from services.ingest.locations import (
    MAX_BOXES_PER_CHUNK,
    ParsedChunk,
    denormalize_bbox,
    is_paragraph_start,
    normalize_bbox,
    parse_pdf_with_locations,
)

# ============================================================
# 1. 归一化往返
# ============================================================


def test_normalize_roundtrip_is_lossless():
    """归一化 → point 往返必须零误差（D33 的前提）。"""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "ROUNDTRIP TEST", fontsize=12)
    bb = None
    for b in page.get_text("dict")["blocks"]:
        if b["type"] == 0:
            bb = b["lines"][0]["bbox"]
            break
    assert bb is not None

    norm = normalize_bbox(bb, page)
    back = denormalize_bbox(norm, page)

    for a, b in zip(back, bb, strict=True):
        assert abs(a - b) < 1e-9, f"{a} != {b}"
    doc.close()


def test_normalize_is_dpi_independent():
    """归一化结果不依赖渲染 DPI —— 前端任意缩放都能直接用。"""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "DPI TEST", fontsize=12)
    bb = page.get_text("dict")["blocks"][0]["lines"][0]["bbox"]

    norm = normalize_bbox(bb, page)
    # 用不同的"渲染宽度"反算，结果必须一致
    for scale in (1.0, 96 / 72, 150 / 72, 300 / 72):
        px = [norm[0] * page.rect.width * scale,
              norm[1] * page.rect.height * scale,
              norm[2] * page.rect.width * scale,
              norm[3] * page.rect.height * scale]
        back = [px[0] / scale / page.rect.width,
                px[1] / scale / page.rect.height,
                px[2] / scale / page.rect.width,
                px[3] / scale / page.rect.height]
        for a, b in zip(back, norm, strict=True):
            assert abs(a - b) < 1e-9
    doc.close()


def test_normalize_handles_degenerate_page():
    """宽高为 0 的页面不能崩（除零）。"""
    doc = pymupdf.open()
    page = doc.new_page()

    class FakePage:
        rect = type("R", (), {"width": 0, "height": 0})()

    assert normalize_bbox((1, 2, 3, 4), FakePage()) == [0.0, 0.0, 0.0, 0.0]
    doc.close()


# ============================================================
# 2. 段落判定（D56）
# ============================================================


def _line(text: str, x0: float, y0: float) -> dict:
    return {"text": text, "x0": x0, "y0": y0, "y1": y0 + 12, "bbox": (x0, y0, x0 + 200, y0 + 12)}


def test_numbered_prefix_starts_new_paragraph():
    base = 50.0
    prev = _line("上一段的结尾内容。", base + 15, 100)
    cur = _line("3.第三段开始了", base + 15, 130)
    assert is_paragraph_start(prev, cur, base)


def test_indent_back_to_flush_left_starts_new_paragraph():
    """缩进回退到顶格 = 新段落（实测这类表单最可靠的信号）。"""
    base = 50.0
    prev = _line("这是一段很长的续行文字继续", base + 15, 100)
    cur = _line("这是新段落的第一行", base, 130)
    assert is_paragraph_start(prev, cur, base)


def test_same_indent_continues_paragraph():
    """同样缩进的相邻行应属于同一段。"""
    base = 50.0
    prev = _line("第一行内容没有句号", base + 15, 100)
    cur = _line("第二行内容也没有句号", base + 15, 131)
    assert not is_paragraph_start(prev, cur, base)


def test_no_global_gap_threshold():
    """⚠️ 核心约束：判定**不能**依赖垂直间隙。

    实测该文档正文行距 13.1pt、表格 4pt，任何"间隙 > N"或
    "间隙 > 全局中位数×k"的判据都会对其中一种失效。
    这里的行间距故意设成 30pt（远大于任何常见阈值），缩进一致时仍应判为同段。
    """
    base = 50.0
    prev = _line("行一", base + 15, 100)
    cur = _line("行二", base + 15, 160)  # gap = 48pt
    assert not is_paragraph_start(prev, cur, base), "不应因间隙大而断段"


def test_sentence_end_with_equal_indent_breaks():
    base = 50.0
    prev = _line("这一段写完了。", base + 15, 100)
    cur = _line("下一段开始了", base + 15, 131)
    assert is_paragraph_start(prev, cur, base)


# ============================================================
# 3. 端到端：构造文档并验证坐标
# ============================================================


def _make_form_pdf(path, *, with_table: bool = True):
    """造一份"每行一个 block"的表单 PDF，复现预研里的真实结构。"""
    doc = pymupdf.open()
    page = doc.new_page()
    lines = [
        (50, "1.First item statement"),
        (50, "   continuation line of item one"),
        (50, "   another continuation"),
        (50, "2.Second item statement"),
        (50, "   continuation of item two"),
    ]
    # 逐行插入 + 分块，模拟"每行独立 block"
    for i, (x, text) in enumerate(lines):
        page.insert_text((x, 100 + i * 30), text, fontsize=11)
    if with_table:
        x0, y0, cw, ch = 50, 280, 100, 22
        for r in range(3):
            for c in range(3):
                page.draw_rect(
                    pymupdf.Rect(x0 + c * cw, y0 + r * ch,
                                 x0 + (c + 1) * cw, y0 + (r + 1) * ch),
                    color=(0, 0, 0), width=0.6,
                )
                page.insert_text((x0 + c * cw + 5, y0 + r * ch + 15),
                                 f"R{r}C{c}", fontsize=8)
    doc.save(path)
    doc.close()
    return path


def test_end_to_end_produces_locations(tmp_path):
    pdf = _make_form_pdf(tmp_path / "form.pdf")
    result = parse_pdf_with_locations(pdf)

    assert result.page_count == 1
    assert not result.is_scanned
    assert result.chunks, "应至少产出 chunk"

    for c in result.chunks:
        assert c.text.strip(), "chunk 文本不应为空"
        assert c.locations, f"chunk 必须有坐标（kind={c.kind}）"
        for loc in c.locations:
            assert 0.0 <= loc["bbox"][0] <= 1.0
            assert 0.0 <= loc["bbox"][1] <= 1.0
            assert 0.0 <= loc["bbox"][2] <= 1.0
            assert 0.0 <= loc["bbox"][3] <= 1.0
            assert loc["bbox"][0] <= loc["bbox"][2], "bbox 左边界应 <= 右边界"
            assert loc["bbox"][1] <= loc["bbox"][3], "bbox 上边界应 <= 下边界"


def test_text_chunks_have_exactly_one_box(tmp_path):
    """正文 chunk 恰好 1 个框（D55）。表格才多框。"""
    pdf = _make_form_pdf(tmp_path / "form.pdf")
    result = parse_pdf_with_locations(pdf)
    for c in result.chunks:
        if c.kind == "text":
            assert len(c.locations) == 1, "正文 chunk 应恰好 1 个框"
        elif c.kind == "table":
            assert len(c.locations) >= 1


def test_table_chunks_carry_row_col(tmp_path):
    """表格 chunk 的框必须带 row/col（D58），前端靠它排网格。"""
    pdf = _make_form_pdf(tmp_path / "form.pdf")
    result = parse_pdf_with_locations(pdf)
    tables = [c for c in result.chunks if c.kind == "table"]
    assert tables, "应识别出表格"
    for c in tables:
        assert c.table_index is not None
        for loc in c.locations:
            assert "row" in loc and "col" in loc
        # D203：同一张 extract() 网格还要投影成结构化 JSON + 一句描述。
        assert c.table_json is not None, "表格 chunk 缺结构化网格"
        assert len(c.table_json["header"]) == c.table_json["col_count"]
        assert len(c.table_json["rows"]) == c.table_json["row_count"] - 1
        assert c.text.startswith("表格描述："), "描述句要在正文最前面（便于检索）"


def test_no_chunk_spans_pages(tmp_path):
    """chunk 不跨页（D55）—— 跨页会让点引用画出满屏框。"""
    doc = pymupdf.open()
    for i in range(3):
        p = doc.new_page()
        p.insert_text((50, 100), f"PAGE {i + 1} content line", fontsize=12)
    path = tmp_path / "multi.pdf"
    doc.save(path)
    doc.close()

    result = parse_pdf_with_locations(path)
    for c in result.chunks:
        pages = {loc["page"] for loc in c.locations}
        assert len(pages) == 1, f"chunk 跨了 {pages}"


# ============================================================
# 4. 空白页 vs 扫描页（D60）
# ============================================================


def test_blank_page_is_not_scan_page(tmp_path):
    """⚠️ 空白页不能算扫描页 —— 那会让 is_scanned 假阳性，
    上层据此决定"整份文档走视觉路径"，白烧 colSmol 的 15s/图。"""
    doc = pymupdf.open()
    doc.new_page()  # 完全空白
    path = tmp_path / "blank.pdf"
    doc.save(path)
    doc.close()

    r = parse_pdf_with_locations(path)
    assert r.blank_pages == [1]
    assert r.scan_pages == []
    assert not r.is_scanned


def _page_with_photo(page) -> None:
    """往页面上贴一张位图 —— 模拟扫描件。

    ⚠️ 用 `draw_rect` 造"扫描页"是**错的**：矢量图形不产生文本层，
    `get_text()` 返回空串，模块会正确判为**空白页**而不是扫描页。
    真正的扫描页特征是"有位图 + 无文本层"，所以必须 `insert_image`。
    """
    # 造一张小位图（PNG 字节流）
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("L", (600, 800), color=200).save(buf, format="PNG")
    page.insert_image(pymupdf.Rect(50, 50, 550, 750), stream=buf.getvalue())


def test_image_only_page_is_scan_page(tmp_path):
    """有位图但无文本层 → 扫描页（D60：只高亮整页，不产坐标）。"""
    doc = pymupdf.open()
    p1 = doc.new_page()
    p1.insert_text((50, 80),
                   "This page carries a real text layer used as the baseline.",
                   fontsize=12)
    p2 = doc.new_page()
    _page_with_photo(p2)
    path = tmp_path / "scan.pdf"
    doc.save(path)
    doc.close()

    assert parse_pdf_with_locations(path).scan_pages == [2], "有位图无文本的页是扫描页"


def test_vector_graphics_page_is_blank_not_scan(tmp_path):
    """⚠️ 纯矢量图形页是**空白页**，不是扫描页。

    两者都要走"跳过"路径，但语义不同：空白页不该触发视觉编码
    （D37 限扫描件 50 页，白烧 colSmol 的 15s/图）。
    """
    doc = pymupdf.open()
    p = doc.new_page()
    p.draw_rect(pymupdf.Rect(50, 50, 500, 700), fill=(0.9, 0.9, 0.9))
    p.draw_circle(pymupdf.Point(300, 400), 100, fill=(0.5, 0.5, 0.5))
    path = tmp_path / "vector.pdf"
    doc.save(path)
    doc.close()

    r = parse_pdf_with_locations(path)
    assert r.blank_pages == [1]
    assert r.scan_pages == [], "矢量图形不构成扫描页"
    assert not r.is_scanned


def test_short_text_page_counts_as_scan_page(tmp_path):
    """文本少于阈值的页（只剩页码）也判为扫描页 —— 这正是扫描 PDF 的特征。"""
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((50, 100), "- 1 -", fontsize=10)  # 只有页码，8 字符
    path = tmp_path / "pagenum.pdf"
    doc.save(path)
    doc.close()

    r = parse_pdf_with_locations(path)
    assert r.scan_pages == [1]
    assert not r.blank_pages, "有页码就不是空白页"


def test_mixed_document_keeps_text_chunks(tmp_path):
    """混合文档（1 页文本 + 1 页扫描）不应整体判为扫描件。"""
    doc = pymupdf.open()
    p1 = doc.new_page()
    p1.insert_text((50, 100),
                   "This page has plenty of real text content to pass the threshold.",
                   fontsize=12)
    p2 = doc.new_page()
    _page_with_photo(p2)
    path = tmp_path / "mixed.pdf"
    doc.save(path)
    doc.close()

    r = parse_pdf_with_locations(path)
    assert r.scan_pages == [2]
    assert not r.is_scanned, "混合文档不应整体判为扫描件"
    assert [c for c in r.chunks if c.page == 1], "文本页仍应产出带坐标的 chunk"


# ============================================================
# 5. 框数上限（D59）
# ============================================================


def test_large_table_split_keeps_all_text(tmp_path):
    """超框数上限时按行拆分，**文本不能丢**。

    之前用"累计框数反推文本切点"，实测 1099+1199 字 > 原文，
    根因是空单元格被跳过时"第 n 个框"与"第 n 段文本"根本不对应。
    """
    doc = pymupdf.open()
    p = doc.new_page()
    x0, y0, cw, ch = 40, 40, 50, 20
    cols, rows = 10, 50
    for r in range(rows):
        for c in range(cols):
            p.draw_rect(
                pymupdf.Rect(x0 + c * cw, y0 + r * ch, x0 + (c + 1) * cw, y0 + (r + 1) * ch),
                color=(0, 0, 0), width=0.5,
            )
            p.insert_text((x0 + c * cw + 4, y0 + r * ch + 14), f"R{r}C{c}", fontsize=7)
    path = tmp_path / "big.pdf"
    doc.save(path)
    doc.close()

    # 原文的非空单元格
    d2 = pymupdf.open(path)
    tbl = d2[0].find_tables().tables[0]
    original = [(c or "").strip() for row in tbl.extract() for c in row if (c or "").strip()]
    d2.close()

    r = parse_pdf_with_locations(path, max_boxes=MAX_BOXES_PER_CHUNK)
    tables = [c for c in r.chunks if c.kind == "table"]
    assert len(tables) > 1, "应被拆成多个 chunk"

    for c in tables:
        assert len(c.locations) <= MAX_BOXES_PER_CHUNK, "每组框数不得超过上限"

    # D169 起表格文本是 **Markdown**（行列结构）而不是逐行 `\n` 拼接 ——
    # 所以判据改成"每个非空单元格都出现在文本里"（拆分后表头会重复出现，
    # 这是刻意的：每段都能独立阅读）。核心保证不变：**不丢单元格**。
    joined = "\n".join(c.text for c in tables)
    for cell_text in original:
        assert cell_text in joined, f"拆分后丢了单元格文本：{cell_text!r}"


# ============================================================
# 6. 旋转页
# ============================================================


def test_rotated_page_coordinates_still_align(tmp_path):
    """旋转页的 bbox 与 page.rect 同空间，归一化公式依然成立。

    实测：pymupdf 的 get_text("dict") 返回的 bbox 已在旋转后空间。
    """
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 150), "ROTATED PAGE TEXT LONG ENOUGH TO PASS THRESHOLD", fontsize=18)
    p.set_rotation(90)
    path = tmp_path / "rot.pdf"
    doc.save(path)
    doc.close()

    r = parse_pdf_with_locations(path)
    assert r.chunks, "旋转页也应产出 chunk（实测 bbox 与 page.rect 同空间）"
    for c in r.chunks:
        for loc in c.locations:
            assert all(0.0 <= v <= 1.0 for v in loc["bbox"])


# ============================================================
# 7. 边界与错误处理
# ============================================================


def test_missing_file_raises_clear_error():
    with pytest.raises(ValueError, match="文件不存在"):
        parse_pdf_with_locations("/nonexistent/xxx.pdf")


def test_chunk_dataclass_defaults():
    c = ParsedChunk(text="hello")
    assert c.kind == "text"
    assert c.locations == []
    assert c.table_index is None
    assert c.token_estimate > 0
