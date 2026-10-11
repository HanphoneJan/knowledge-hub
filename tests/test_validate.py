"""分层校验（D166）单元测试。"""

from __future__ import annotations

from services.ingest.chunker import Chunk
from services.ingest.validate import (
    chunk_quality,
    format_mismatch,
    garbled_rate,
    validate_parse,
)

# ============================================================
# 第一层：格式嗅探（保守）
# ============================================================


def test_format_mismatch_detects_png_named_pdf():
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    assert format_mismatch(".pdf", png) is not None


def test_format_mismatch_detects_pdf_named_png():
    pdf = b"%PDF-1.7\n" + b"x" * 32
    assert format_mismatch(".png", pdf) is not None


def test_format_mismatch_allows_unrecognized_text():
    # "内容看着不像 PDF，但识别不出是别的二进制" ⇒ 放行（保守原则）
    assert format_mismatch(".pdf", b"hello world this is not really a pdf") is None


def test_format_mismatch_allows_real_pdf_and_zip():
    assert format_mismatch(".pdf", b"%PDF-1.7") is None
    assert format_mismatch(".docx", b"PK\x03\x04rest") is None


def test_format_mismatch_empty_and_unknown_suffix():
    assert format_mismatch(".pdf", b"") is None
    assert format_mismatch(".xyz", b"\x00\x01") is None


# ============================================================
# 第二层：解析校验
# ============================================================


def test_validate_parse_empty_is_fatal():
    rep = validate_parse("   \n  ")
    assert rep.fatal and not rep.ok
    assert rep.issues


def test_validate_parse_garbled():
    text = ("正常内容" * 10) + ("\ufffd" * 20)
    rep = validate_parse(text)
    assert rep.garbled_rate > 0.05
    assert not rep.ok


def test_validate_parse_too_short():
    rep = validate_parse("短")
    assert not rep.ok
    assert any("过短" in i for i in rep.issues)


def test_validate_parse_ok():
    rep = validate_parse("这是一段足够长的正常中文内容。" * 20)
    assert rep.ok and not rep.fatal


def test_garbled_rate_ignores_newlines():
    assert garbled_rate("a\nb\tc") == 0.0


# ============================================================
# 第三层：Chunking 质量
# ============================================================


def test_chunk_quality_ok():
    q = chunk_quality([Chunk(text="x" * 500), Chunk(text="y" * 480)])
    assert q.acceptable
    assert q.total == 2


def test_chunk_quality_flags_tiny_chunk():
    q = chunk_quality([Chunk(text="x" * 500), Chunk(text="yy")])
    assert not q.acceptable
    assert any("过小" in e for e in q.errors)


def test_chunk_quality_warns_large_chunk():
    q = chunk_quality([Chunk(text="x" * 500), Chunk(text="y" * 9000)])
    assert q.acceptable, "过大只是警告，不阻断"
    assert any("过大" in w for w in q.warnings)


def test_chunk_quality_empty_is_error():
    q = chunk_quality([])
    assert not q.acceptable
