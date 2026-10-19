"""检索质量指标（**纯函数**）—— 评测回放的度量层。

## 为什么单独一个模块

文章《RAG 优化》反复强调：**没有回放就不知道是变好了还是换了一种错法**。
回放的两个前提是「固定的问题集」与「可复现的度量」。前者是 `eval/_qset.py`，
后者就是这里 —— **只做算术，不碰 DB / 模型 / 网络**，所以能直接在
`deploy/ci.sh` 的免基础设施门禁里跑（`tests/test_retrieval_metrics.py`）。

## 指标口径

| 指标 | 衡量 | 定义 |
|---|---|---|
| `hit@k` | 召回 | top-k 里**有没有**任一真值 |
| `recall@k` | 召回完整性 | top-k 覆盖了多少比例的真值（真值可能是多个 chunk）|
| `mrr@k` | 排序 | 第一个真值排名的倒数（1/rank，未命中 0）|
| `context_recall@k` | 上下文完整性 | 应该进上下文的 chunk 有多少真的进了 |
| `context_precision@k` | 上下文纯度 | 进上下文的 chunk 有多少是真的相关 |

🔑 **`hit@k` 与 `recall@k` 分开**：单个真值时二者相等，多个真值时
`hit@k=1` 只说明"命中了一个"，`recall@k` 才说明"找全了没有"。
文章把 Context Recall 与 Context Precision 拆开，正是要区分这两件事。

🔑 **必不带副作用**：所有函数只吃"已排好的标识列表"与"真值集合"，
排序/融合/租户过滤发生在调用方（`services/retrieval/`），
本模块**不 import 任何服务代码** —— 否则它就渗进了检索实现，失去"可独立验证"的价值。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field


@dataclass
class QueryScore:
    """一条 query 的度量结果。`ident` 用于可读报告（问题原文 / id）。"""

    ident: str
    category: str
    hit_at: dict[int, float] = field(default_factory=dict)
    recall_at: dict[int, float] = field(default_factory=dict)
    mrr: float = 0.0
    first_rank: int | None = None
    n_gold: int = 0


def hit_at_k(ranked: Sequence, gold: Iterable, k: int) -> float:
    """top-`k` 里有没有任一真值（1.0 / 0.0）。"""
    g = set(gold)
    if not g:
        return 0.0
    return 1.0 if any(x in g for x in ranked[:k]) else 0.0


def recall_at_k(ranked: Sequence, gold: Iterable, k: int) -> float:
    """top-`k` 覆盖的真值比例 = |gold ∩ topk| / |gold|。"""
    g = set(gold)
    if not g:
        return 0.0
    got = {x for x in ranked[:k] if x in g}
    return len(got) / len(g)


def reciprocal_rank(ranked: Sequence, gold: Iterable, k: int) -> tuple[float, int | None]:
    """第一个真值的 `(1/rank, rank)`；`k` 内没有则 `(0.0, None)`。"""
    g = set(gold)
    for i, x in enumerate(ranked[:k], start=1):
        if x in g:
            return 1.0 / i, i
    return 0.0, None


def context_recall(gold_chunks: Iterable, retrieved: Sequence, k: int) -> float:
    """应该进上下文的 chunk 有多少真的进了（相对 `gold_chunks` 全集）。"""
    g = set(gold_chunks)
    if not g:
        return 0.0
    return len(g & set(retrieved[:k])) / len(g)


def context_precision(gold_chunks: Iterable, retrieved: Sequence, k: int) -> float:
    """进上下文的 chunk 有多少是真的相关（相对实际取回的量）。"""
    top = list(retrieved[:k])
    if not top:
        return 0.0
    g = set(gold_chunks)
    return len([x for x in top if x in g]) / len(top)


def score_query(
    ident: str,
    category: str,
    ranked: Sequence,
    gold: Iterable,
    *,
    ks: Sequence[int] = (1, 5, 10),
    mrr_k: int = 10,
) -> QueryScore:
    """一条 query 在多个 `k` 上的度量。`ranked` 是**已按相关性排好**的标识列表。"""
    gold_set = set(gold)
    mrr, first = reciprocal_rank(ranked, gold_set, mrr_k)
    return QueryScore(
        ident=ident,
        category=category,
        hit_at={k: hit_at_k(ranked, gold_set, k) for k in ks},
        recall_at={k: recall_at_k(ranked, gold_set, k) for k in ks},
        mrr=mrr,
        first_rank=first,
        n_gold=len(gold_set),
    )


def mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def summarize(scores: Sequence[QueryScore], *, ks: Sequence[int] = (1, 5, 10)) -> dict:
    """把一组 `QueryScore` 汇总成 `{overall, by_category}`（**分类别是纪律**）。

    🔑 分类别汇总不是锦上添花：本项目已实测"一条固定权重不可能是所有类别的
    最优"，把 chat / photo / doc 混成一个 overall 数只会得到一个不可解释的值
    （见 `docs/retrieval.md` §1 的纪律）。所以这里**永远**同时给 overall 与 by_category。
    """

    def _block(rows: Sequence[QueryScore]) -> dict:
        return {
            "n_queries": len(rows),
            "hit_at": {k: round(mean([r.hit_at.get(k, 0.0) for r in rows]), 4) for k in ks},
            "recall_at": {
                k: round(mean([r.recall_at.get(k, 0.0) for r in rows]), 4) for k in ks
            },
            "mrr": round(mean([r.mrr for r in rows]), 4),
        }

    by_cat: dict[str, list[QueryScore]] = {}
    for r in scores:
        by_cat.setdefault(r.category, []).append(r)
    return {
        "overall": _block(scores),
        "by_category": {c: _block(rows) for c, rows in sorted(by_cat.items())},
    }


def per_query_rows(scores: Sequence[QueryScore], *, ks: Sequence[int] = (1, 5, 10)) -> list[dict]:
    """逐条明细（报告里人可读那一半）。"""
    return [
        {
            "q": r.ident,
            "cat": r.category,
            "first_rank": r.first_rank,
            "mrr@10": round(r.mrr, 4),
            **{f"hit@{k}": r.hit_at.get(k, 0.0) for k in ks},
            **{f"recall@{k}": round(r.recall_at.get(k, 0.0), 4) for k in ks},
        }
        for r in scores
    ]


__all__ = [
    "QueryScore",
    "context_precision",
    "context_recall",
    "hit_at_k",
    "mean",
    "per_query_rows",
    "recall_at_k",
    "reciprocal_rank",
    "score_query",
    "summarize",
]
