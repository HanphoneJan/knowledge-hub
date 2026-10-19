"""`ask.to_evidences` 的证据组装（纯函数，进 `deploy/ci.sh`）。

盯两件事（文章 §上下文排序）：
1. **同文档相邻片段按 `chunk_index` 保持原文顺序**（融合序会被打散）；
2. **文档之间的顺序取首次出现**、重复命中**去重且角标不跳号**。
"""

from __future__ import annotations

from services.retrieval.ask import to_evidences
from services.retrieval.types import FusedHit


def _hit(doc_id: int, ci: int) -> FusedHit:
    return FusedHit(
        key=f"text:{doc_id}:{ci}", point_id=doc_id * 1000 + ci, doc_id=doc_id,
        chunk_index=ci, score=0.03, legs=["dense"],
        payload={"file_name": f"doc{doc_id}.md", "text_norm": f"片段{ci}"},
    )


def test_same_doc_adjacent_chunks_are_in_original_order():
    # 融合序把 doc1 的 chunk 5 排在 chunk 3 前面
    evs = to_evidences([_hit(1, 5), _hit(1, 3), _hit(2, 0)])
    assert [(e.doc_id, e.chunk_index) for e in evs] == [(1, 3), (1, 5), (2, 0)]
    assert [e.idx for e in evs] == [1, 2, 3]


def test_document_order_is_first_appearance():
    evs = to_evidences([_hit(2, 1), _hit(1, 0)])
    assert [e.doc_id for e in evs] == [2, 1]


def test_dedup_keeps_no_gaps_in_idx():
    evs = to_evidences([_hit(1, 0), _hit(1, 0), _hit(2, 0)])
    assert [(e.doc_id, e.chunk_index) for e in evs] == [(1, 0), (2, 0)]
    assert [e.idx for e in evs] == [1, 2]


def test_empty():
    assert to_evidences([]) == []
