"""切分策略注册表（D164）的单元测试。

覆盖六种策略的**行为**与"默认逐字不变"的守卫，以及流派生/契约不漂移。
不加载任何模型：`embed` / `llm` 全部注入假实现。
"""

from __future__ import annotations

from typing import get_args

from services.ingest.chunker import Chunk
from services.ingest.strategies import (
    DEFAULT_STRATEGY,
    STRATEGY_NAMES,
    ChunkContext,
    apply_strategy,
    attach_parents,
    semantic_split,
    spans_for,
    strategy_names,
)


def _chunk(text: str, **kw) -> Chunk:
    return Chunk(text=text, **kw)


# ============================================================
# 注册表与契约
# ============================================================


def test_registry_names_match_the_schema_literal():
    """`STRATEGIES` 与 `schemas.ChunkStrategy` 的取值必须一致。

    common 不能 import ingest（会成环），所以两边各写一份 —— 用这条钉住。
    """
    from services.common.schemas import ChunkStrategy

    assert set(get_args(ChunkStrategy)) == set(strategy_names())
    assert set(strategy_names()) == set(STRATEGY_NAMES)


def test_default_strategy_is_recursive():
    assert DEFAULT_STRATEGY == "recursive"


# ============================================================
# recursive = 原样（基线守卫）
# ============================================================


def test_recursive_returns_chunks_untouched():
    chunks = [_chunk("a"), _chunk("b")]
    res = apply_strategy(chunks, strategy="recursive", context=ChunkContext())
    assert res.chunks == chunks
    assert res.encoding == "standard"
    assert res.notes == []


def test_unknown_strategy_falls_back_with_note():
    chunks = [_chunk("a")]
    res = apply_strategy(chunks, strategy="lol", context=ChunkContext())
    assert res.chunks == chunks
    assert res.notes, "未知策略必须留下一条说明，不能静默"


# ============================================================
# fixed
# ============================================================


def test_fixed_split_respects_size_and_overlap():
    res = apply_strategy(
        [], strategy="fixed",
        context=ChunkContext(full_text="甲乙丙丁戊己庚辛壬癸" * 5, size=10, overlap=2),
    )
    assert res.chunks
    assert all(len(c.text) <= 10 for c in res.chunks)


# ============================================================
# semantic
# ============================================================


def _topic_embed(sents):
    return [[1.0, 0.0] if ("苹果" in s or "香蕉" in s) else [0.0, 1.0] for s in sents]


def test_semantic_groups_by_topic():
    text = "苹果很甜。香蕉很黄。Python 是编程语言。Java 也是编程语言。"
    chunks = semantic_split(text, size=30, overlap=0, embed=_topic_embed)
    assert len(chunks) == 2, [c.text for c in chunks]
    assert "苹果" in chunks[0].text and "香蕉" in chunks[0].text
    assert "Python" in chunks[1].text


def test_semantic_without_embed_degrades_on_pdf():
    chunks = [_chunk("原文")]
    res = apply_strategy(
        chunks, strategy="semantic",
        context=ChunkContext(full_text="原文", is_pdf=True, embed=None),
    )
    assert res.chunks == chunks
    assert res.notes


def test_semantic_exception_falls_back_to_default():
    def boom(_sents):
        raise RuntimeError("模型炸了")

    res = apply_strategy(
        [_chunk("原文一。原文二。")], strategy="semantic",
        context=ChunkContext(full_text="原文一。原文二。", embed=boom),
    )
    assert res.chunks
    assert any("失败" in n for n in res.notes), res.notes


# ============================================================
# parent_child
# ============================================================


def test_parent_child_groups_by_section_path():
    chunks = [
        _chunk("第一节 A", section_path="H1"),
        _chunk("第一节 B", section_path="H1"),
        _chunk("第二节 C", section_path="H2"),
    ]
    attach_parents(chunks)
    assert chunks[0].parent_text == chunks[1].parent_text
    assert "第一节 A" in chunks[0].parent_text
    assert "第二节 C" not in chunks[0].parent_text
    assert chunks[2].parent_text == "第二节 C"


def test_parent_child_by_page():
    chunks = [
        _chunk("p1a", page=1), _chunk("p1b", page=1), _chunk("p2", page=2),
    ]
    attach_parents(chunks, by_page=True)
    assert "p1a" in chunks[1].parent_text and "p1b" in chunks[1].parent_text
    assert "p2" not in chunks[1].parent_text
    assert chunks[2].parent_text == "p2"


def test_parent_child_caps_parent_text():
    big = _chunk("x" * 5000, section_path="S")
    attach_parents([big], max_parent=100)
    assert len(big.parent_text) == 100


# ============================================================
# late
# ============================================================


def test_late_sets_encoding_flag():
    res = apply_strategy([_chunk("a")], strategy="late", context=ChunkContext())
    assert res.encoding == "late"


def test_spans_for_locates_substrings():
    chunks = [_chunk("AAA"), _chunk("BBB")]
    assert spans_for("AAABBB", chunks) == [(0, 3), (3, 6)]


def test_spans_for_falls_back_when_not_found():
    # 文本被清洗改写过，找不回 —— 仍要返回**等长**的区间（下标一一对应）
    chunks = [_chunk("AAA"), _chunk("ZZZ")]
    spans = spans_for("AAABBB", chunks)
    assert len(spans) == 2


# ============================================================
# contextual
# ============================================================


def test_contextual_prepends_context():
    res = apply_strategy(
        [_chunk("正文")], strategy="contextual",
        context=ChunkContext(full_text="全文", llm=lambda d, c: "这是关于 X 的一段"),
    )
    assert res.chunks[0].text.startswith("[上下文] 这是关于 X 的一段")


def test_contextual_without_llm_degrades_with_note():
    chunks = [_chunk("正文")]
    res = apply_strategy(chunks, strategy="contextual", context=ChunkContext())
    assert res.chunks == chunks
    assert any("LLM" in n for n in res.notes)
