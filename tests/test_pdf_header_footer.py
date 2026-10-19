"""PDF 页眉/页脚清洗（P6 / 文章 §数据治理「去噪」）—— 纯函数，进 `deploy/ci.sh`。

判据（都指向**性质**）：
1. 跨页重复的短行（≥3 页、<40 字）被识别为页眉/页脚；
2. 它**不进** chunk 文本（否则污染词表、拉低罕见词 idf）；
3. 正文行**保留**、且仍带坐标（清洗不能破坏 L3 不变量）。
"""

from __future__ import annotations

import pymupdf

from services.ingest.locations import _repeated_lines, parse_pdf_with_locations

HEADER = "CONFIDENTIAL HEADER"


def _make_pdf(tmp_path):
    doc = pymupdf.open()
    for i in range(3):
        page = doc.new_page()
        page.insert_text((72, 40), HEADER)              # 每页相同的页眉
        page.insert_text(
            (72, 120),
            f"Body line for page {i} with enough characters to pass the threshold.",
        )
    path = tmp_path / "hf.pdf"
    doc.save(path)
    doc.close()
    return path


def test_repeated_line_is_detected(tmp_path):
    path = _make_pdf(tmp_path)
    doc = pymupdf.open(path)
    try:
        assert HEADER in _repeated_lines(doc)
    finally:
        doc.close()


def test_header_dropped_body_kept_with_coordinates(tmp_path):
    path = _make_pdf(tmp_path)
    res = parse_pdf_with_locations(path)
    text = " ".join(c.text for c in res.chunks)
    assert HEADER not in text, "页眉没被清掉（会污染检索）"
    assert "Body line for page 0" in text, "正文被误删了"
    # 正文仍带坐标（清洗发生在分段之前，不影响剩余行的 bbox）
    body = next(c for c in res.chunks if "Body line for page 0" in c.text)
    assert body.locations and body.locations[0].get("bbox")


def test_short_doc_is_not_cleaned(tmp_path):
    """少于 3 页时不做重复判定（没有"重复"可言）—— 免得误删唯一的那行。"""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 40), "ONLY HEADER")
    page.insert_text((72, 120), "Only body line that is long enough to keep.")
    path = tmp_path / "one.pdf"
    doc.save(path)
    doc.close()
    reopened = pymupdf.open(path)
    try:
        assert _repeated_lines(reopened) == set()
    finally:
        reopened.close()
