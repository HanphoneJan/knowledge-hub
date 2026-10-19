"""`services/retrieval/quality.py` 的判据（纯函数，进 `deploy/ci.sh`）。

盯三条性质：
1. 无命中 ⇒ `none`（硬拒答的信号）；
2. 证据只来自一条腿 ⇒ `weak`（文章要的"低置信"）；
3. 有跨腿共识 ⇒ `ok`（RRF 想要的性质，见 `fusion.py`）。
"""

from __future__ import annotations

from services.retrieval.quality import assess
from services.retrieval.types import FusedHit


def _hit(legs: list[str]) -> FusedHit:
    return FusedHit(
        key="text:1", point_id=1, doc_id=1, chunk_index=0, score=0.03,
        legs=legs, ranks={leg: 1 for leg in legs}, payload={},
    )


def test_none_when_no_hits():
    q = assess([])
    assert q.level == "none"
    assert q.max_legs == 0
    assert q.weak is True


def test_weak_when_single_leg():
    q = assess([_hit(["dense"]), _hit(["sparse"])][:1])
    assert q.level == "weak"
    assert q.weak is True


def test_ok_when_cross_leg_consensus():
    q = assess([_hit(["dense", "sparse"])])
    assert q.level == "ok"
    assert q.max_legs == 2
    assert q.weak is False


def test_consensus_only_needs_the_head():
    """只看头部几条的共识强度 —— 长尾的单腿命中不该把结论拉回 weak。"""
    head = [_hit(["dense", "trgm"])]
    tail = [_hit(["dense"]) for _ in range(10)]
    assert assess([*head, *tail]).level == "ok"


def test_tolerates_missing_legs_attr():
    """脏对象（没有 legs 字段）不该抛 —— 当作单腿/无共识。"""
    class _Bare:
        pass

    assert assess([_Bare()]).level == "weak"
