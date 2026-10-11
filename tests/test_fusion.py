"""RRF 融合与腿编排的单元测试。

**覆盖的是实际会错的地方，不是理想路径**（CONVENTIONS §9）：
- 命名空间前缀：忘了加前缀 → 文本 chunk 3 与扫描页 3 互相抵消（PROGRESS §5.10）
- 跳过的腿不参与分母：视觉腿在 API 进程恒为 skipped，
  若把它的空结果当"排了 0 名"，会稀释所有结果
- 确定性 tiebreaker：RRF 分并列时不排序 → 刷新一次角标 [1][2] 就对调
- 权重公式：**故意测出"默认全 1.0"这个事实**，因为调它之前必须先看
  `reports/retrieval_eval.json` 里那条结论（等权 RRF 每个类别都没赢过最佳单腿）
"""

from __future__ import annotations

import pytest

from services.retrieval.fusion import DEFAULT_WEIGHTS, RRF_K, rrf
from services.retrieval.types import (
    ALL_LEGS,
    LEG_DENSE,
    LEG_SPARSE,
    LEG_TRGM,
    LEG_VISUAL,
    LegHit,
    LegResult,
    citation_kind,
    rrf_key,
)


def hit(leg: str, pid: int, rank: int, *, score: float = 1.0, doc_id=None, ci=0):
    ns = "visual" if leg == LEG_VISUAL else "text"
    return LegHit(
        key=rrf_key(ns, pid),
        point_id=pid,
        doc_id=doc_id if doc_id is not None else pid,
        chunk_index=ci,
        rank=rank,
        score=score,
        leg=leg,
        payload={"file_name": f"{leg}.pdf", "text_norm": "正文", "text_raw": "正文"},
    )


# ============================================================
# RRF 公式
# ============================================================


def test_rrf_score_matches_formula():
    """score = Σ weight / (k + rank)"""
    out = rrf([LegResult(leg=LEG_DENSE, hits=[hit(LEG_DENSE, 1, 1)])], limit=5)
    assert out[0].score == pytest.approx(1.0 / (RRF_K + 1), abs=1e-6)


def test_rrf_sums_contributions_across_legs():
    """同一条腿集合中靠前的 chunk 赢。"""
    out = rrf(
        [
            LegResult(leg=LEG_DENSE, hits=[hit(LEG_DENSE, 1, 1), hit(LEG_DENSE, 2, 2)]),
            LegResult(leg=LEG_SPARSE, hits=[hit(LEG_SPARSE, 1, 1), hit(LEG_SPARSE, 2, 5)]),
        ],
        limit=5,
    )
    # chunk1: dense rank1 + sparse rank1；chunk2: dense rank2 + sparse rank5
    assert out[0].point_id == 1
    assert out[0].score == pytest.approx(1 / 61 + 1 / 61, abs=1e-6)
    assert out[1].score == pytest.approx(1 / 62 + 1 / 65, abs=1e-6)
    assert out[0].score > out[1].score


def test_rrf_consensus_beats_single_leg_winner():
    """🔑 RRF 的核心性质：两条腿都认可 > 一条腿排第一另一条垫底。

    这是 k=60 的意义（rank1 与 rank2 只差 0.26%）——
    微小的单腿优势不会压倒跨腿共识。
    """
    out = rrf(
        [
            LegResult(leg=LEG_DENSE, hits=[hit(LEG_DENSE, 1, 1), hit(LEG_DENSE, 2, 30)]),
            LegResult(leg=LEG_SPARSE, hits=[hit(LEG_SPARSE, 1, 1), hit(LEG_SPARSE, 2, 2)]),
        ],
        limit=5,
    )
    assert out[0].point_id == 1, "chunk1 两条腿都第一，应该赢过 chunk2 的 30+2"


def test_rrf_merges_same_chunk_from_three_legs():
    """dense/sparse/trgm 命中同一个 chunk（点 ID 相同）→ 合并成一条，分数累加。"""
    legs = [
        LegResult(leg=leg, hits=[hit(leg, 42, 1)])
        for leg in (LEG_DENSE, LEG_SPARSE, LEG_TRGM)
    ]
    out = rrf(legs, limit=5)
    assert len(out) == 1, "同一点 ID 不该被当成三条结果"
    assert out[0].score == pytest.approx(3 / 61, abs=1e-6)
    assert set(out[0].legs) == {LEG_DENSE, LEG_SPARSE, LEG_TRGM}


# ============================================================
# 命名空间（PROGRESS §5.10）
# ============================================================


def test_text_and_visual_points_with_same_id_do_not_cancel():
    """🔑 文本 chunk 3 与扫描页 3 是**不同**的东西，不能互相抵消。

    `kb_dense` 的点 ID 与 `kb_visual` 的页级点 ID 空间独立
    （旧 `mem_text`/`mem_visual` 都从 0 开始，照片曾覆盖扫描页 0~23）。
    """
    out = rrf(
        [
            LegResult(leg=LEG_DENSE, hits=[hit(LEG_DENSE, 3, 1)]),
            LegResult(leg=LEG_VISUAL, hits=[hit(LEG_VISUAL, 3, 1)]),
        ],
        limit=5,
    )
    assert len(out) == 2, "同 ID 不同命名空间必须保持两条"
    assert out[0].key == "text:3"
    assert out[1].key == "visual:3"
    assert out[0].point_id == out[1].point_id == 3


# ============================================================
# 跳过的腿
# ============================================================


def test_skipped_leg_does_not_dilute_others():
    """🔑 视觉腿在 API 进程恒为 skipped（CONVENTIONS §4.1）。

    若把它的空结果当成"排了 0 名"塞进分母，所有 chunk 的分数都被压低，
    跨腿的相对关系也会失真。
    """
    skipped = LegResult(leg=LEG_VISUAL, skipped="colSmol 未启用（D50）")
    with_skip = rrf(
        [LegResult(leg=LEG_DENSE, hits=[hit(LEG_DENSE, 1, 1)]), skipped], limit=5
    )
    without = rrf([LegResult(leg=LEG_DENSE, hits=[hit(LEG_DENSE, 1, 1)])], limit=5)
    assert with_skip[0].score == without[0].score


def test_leg_that_ran_but_found_nothing_is_not_skipped():
    """跑了但没命中 ≠ 跳过。前者的 `skipped is None`。"""
    empty = LegResult(leg=LEG_TRGM, hits=[])
    assert empty.ok
    out = rrf([LegResult(leg=LEG_DENSE, hits=[hit(LEG_DENSE, 1, 1)]), empty], limit=5)
    assert out[0].score == pytest.approx(1 / 61, abs=1e-6)


# ============================================================
# 确定性
# ============================================================


def test_tie_is_broken_deterministically():
    """🔑 RRF 分并列时必须有确定性 tiebreaker。

    没有它的话同一个 query 两次请求的顺序会抖动，
    前端的引用角标 `[1][2]` 会在刷新后对调 —— 那是很难被测出来的 bug。
    """
    legs = [
        LegResult(leg=LEG_DENSE, hits=[hit(LEG_DENSE, 7, 1), hit(LEG_DENSE, 3, 1)]),
    ]
    a = [h.key for h in rrf(legs, limit=5)]
    b = [h.key for h in rrf(list(legs), limit=5)]
    assert a == b == ["text:3", "text:7"], "同分时按 key 升序，保证可复现"


# ============================================================
# 权重：默认必须是全 1.0
# ============================================================


def test_default_weights_are_all_one():
    """🔑 记录"默认等权"这个事实，因为**调它之前要先看实测结论**。

    `reports/retrieval_eval.json`：等权 RRF 在每个类别上都没赢过该类最佳单腿
    （chat 1.00→0.90、photo 1.00→0.60、blog 1.00→0.88）。
    photo 类 R@5=0.00 是 **query 类型路由**问题（阶段 3 的 `routing.py`），
    继续调 RRF 的 k 或权重解决不了。
    """
    assert DEFAULT_WEIGHTS == dict.fromkeys(ALL_LEGS, 1.0)
    assert DEFAULT_WEIGHTS[LEG_TRGM] == 1.0, "trgm 是新增的腿，不该偷偷给高权重"


def test_weights_override_is_respected():
    out = rrf(
        [LegResult(leg=LEG_DENSE, hits=[hit(LEG_DENSE, 1, 1)])],
        weights={LEG_DENSE: 2.0},
        limit=5,
    )
    assert out[0].score == pytest.approx(2.0 / 61, abs=1e-6)


# ============================================================
# payload / 引用类型
# ============================================================


def test_richer_payload_wins_on_merge():
    """dense 的 payload 带 mime/kind/table_index，trgm 复算的那份是补齐的同构 dict。

    合并时取信息量最大的那份 —— 但**同长度时先到先得**（dense 先跑），
    所以 dense 的那份不会被 trgm 的覆盖。
    """
    rich = LegHit(
        key=rrf_key("text", 1), point_id=1, doc_id=1, chunk_index=0,
        rank=1, score=1.0, leg=LEG_DENSE,
        payload={"mime": "application/pdf", "kind": "table", "table_index": 2},
    )
    thin = LegHit(
        key=rrf_key("text", 1), point_id=1, doc_id=1, chunk_index=0,
        rank=2, score=0.5, leg=LEG_TRGM,
        payload={"kind": "table"},
    )
    out = rrf(
        [LegResult(leg=LEG_DENSE, hits=[rich]), LegResult(leg=LEG_TRGM, hits=[thin])],
        limit=5,
    )
    assert out[0].payload["mime"] == "application/pdf"
    assert out[0].payload["table_index"] == 2


def test_citation_kind_maps_chunk_kind_to_contract_kind():
    """`chunks.kind`（前端画框用）≠ 契约的引用类型（展示用）。

    D60：扫描件没有文本层、拿不到坐标，只高亮整页 → `page`。
    """
    assert citation_kind("text") == "text"
    assert citation_kind("table") == "text"
    assert citation_kind("page") == "page"
    assert citation_kind("text", visual=True) == "image"
    assert citation_kind("page", visual=True) == "image"


# ============================================================
# limit
# ============================================================


def test_limit_truncates():
    legs = [LegResult(leg=LEG_DENSE, hits=[hit(LEG_DENSE, i, i) for i in range(1, 21)])]
    assert len(rrf(legs, limit=5)) == 5