"""`eval/retrieval_metrics.py` 的判据（免基础设施，进 `deploy/ci.sh`）。

判据指向**性质**，不是某组魔数：
- 空真值 / 无命中 / 多真值三种边界各自的行为；
- `hit@k` 与 `recall@k` 在**多真值**时必须分道扬镳（这正是文章把
  Context Recall / Precision 拆开的理由）；
- `mrr` 只认**第一个**真值的位置；
- `summarize` 必须同时给 overall 与 by_category（分类别是纪律）。
"""

from __future__ import annotations

from eval.retrieval_metrics import (
    QueryScore,
    context_precision,
    context_recall,
    hit_at_k,
    recall_at_k,
    reciprocal_rank,
    score_query,
    summarize,
)

# 一组"真值在第 3 名"的固定输入，多处复用
RANKED = ["a", "b", "gold", "c", "d"]
GOLD = {"gold"}


def test_hit_and_recall_single_gold_agree():
    """单真值时 hit@k 与 recall@k 相等（命中即找全）。"""
    assert hit_at_k(RANKED, GOLD, 3) == 1.0
    assert recall_at_k(RANKED, GOLD, 3) == 1.0
    # k=2 时真值还没出现
    assert hit_at_k(RANKED, GOLD, 2) == 0.0
    assert recall_at_k(RANKED, GOLD, 2) == 0.0


def test_multiple_gold_hit_and_recall_diverge():
    """🔑 多真值：命中一个就让 hit@k=1，但 recall@k 只反映"找全了没有"。"""
    ranked = ["g1", "x", "y", "z"]
    gold = {"g1", "g2", "g3"}
    assert hit_at_k(ranked, gold, 4) == 1.0        # 命中了一个
    assert recall_at_k(ranked, gold, 4) == 1 / 3   # 但只找全了 1/3
    # 两个真值
    ranked2 = ["g1", "g2", "x"]
    assert recall_at_k(ranked2, gold, 3) == 2 / 3


def test_empty_gold_is_zero_not_error():
    """空真值一律 0（而不是除零 / 恒 1）—— 防止"恒真"的假指标。"""
    assert hit_at_k(RANKED, set(), 5) == 0.0
    assert recall_at_k(RANKED, set(), 5) == 0.0
    assert reciprocal_rank(RANKED, set(), 5) == (0.0, None)
    assert context_recall(set(), RANKED, 5) == 0.0


def test_mrr_uses_first_gold_only():
    """mrr 只认第一个真值：排第 3 → 1/3，排第 1 → 1.0。"""
    assert reciprocal_rank(RANKED, GOLD, 5) == (1 / 3, 3)
    assert reciprocal_rank(["gold", "a"], GOLD, 5) == (1.0, 1)
    # 真值在 k 之外 → 未命中
    assert reciprocal_rank(["a", "b", "c"], GOLD, 2) == (0.0, None)


def test_context_recall_and_precision():
    """上下文完整性（相对真值全集）与纯度（相对实际取回量）是不同的东西。"""
    gold = {"c1", "c2"}
    retrieved = ["c1", "noise", "c2", "noise2"]
    assert context_recall(gold, retrieved, 4) == 1.0        # 两个真值都进了
    assert context_precision(gold, retrieved, 4) == 0.5     # 四条里两条相关
    # 只取前两条
    assert context_recall(gold, retrieved, 2) == 0.5
    assert context_precision(gold, retrieved, 2) == 0.5


def test_context_precision_empty_retrieval_is_zero():
    """没取回任何东西时纯度是 0（不是 1）—— 否则"不检索"会显得最纯。"""
    assert context_precision({"c1"}, [], 5) == 0.0


def test_score_query_shape():
    """`score_query` 把多 k 的度量打包好，`first_rank` 可读。"""
    s = score_query("q", "doc", RANKED, GOLD, ks=(1, 5))
    assert isinstance(s, QueryScore)
    assert s.first_rank == 3
    assert s.hit_at[5] == 1.0 and s.hit_at[1] == 0.0
    assert s.n_gold == 1


def test_summarize_groups_by_category():
    """汇总必须同时给 overall 与 by_category（分类别是纪律）。"""
    rows = [
        score_query("a", "doc", ["gold"], {"gold"}),
        score_query("b", "doc", ["x"], {"gold"}),
        score_query("c", "chat", ["gold"], {"gold"}),
    ]
    out = summarize(rows)
    assert set(out) == {"overall", "by_category"}
    assert out["overall"]["n_queries"] == 3
    assert out["by_category"]["doc"]["n_queries"] == 2
    assert out["by_category"]["doc"]["hit_at"][1] == 0.5   # doc 一半命中
    assert out["by_category"]["chat"]["hit_at"][1] == 1.0
