"""🔑 查询类型路由（`services/retrieval/routing.py`）。

## 为什么这些测试重要

路由是**猜用户意图**，猜错的后果不是"排得不好"，而是
"把结果过滤空"或"把权重调反"。所以除了"该识别的识别出来"，
更要验**不该发生的事**：

- 拿不准时**不路由**（保持等权）
- 过滤后为空**要退回**，而不是返回 0 条
- 多个意图同时命中时**取更具体的那个**，不是加权求和
- 脏 payload 不许炸

每一条都对应一种真实故障模式（PROGRESS §5.42 是同源的教训）。
"""

from __future__ import annotations

import pytest

from services.retrieval.routing import (
    INTENT_CHAT,
    INTENT_DATE,
    INTENT_GENERAL,
    INTENT_PHOTO,
    INTENT_SCAN,
    INTENT_TABLE,
    MIN_CONFIDENCE,
    Route,
    classify,
    prefer,
    route,
)
from services.retrieval.types import LEG_DENSE, LEG_SPARSE, LEG_TRGM, LEG_VISUAL


class _Hit:
    def __init__(self, **payload) -> None:
        self.payload = payload


# ============================================================
# 1. 意图识别
# ============================================================


@pytest.mark.parametrize(
    "q,intent",
    [
        ("第一次旅游的照片", INTENT_PHOTO),
        ("帮我找一下当时拍的合影", INTENT_PHOTO),
        ("那张照片墙在哪", INTENT_PHOTO),
        ("扫描版的申请表", INTENT_SCAN),
        ("我拍下来的那份表格截图", INTENT_SCAN),
        ("考察情况那栏怎么填", INTENT_TABLE),
        ("表格里第几栏填Remarks", INTENT_TABLE),
        ("我们聊天记录里说过什么", INTENT_CHAT),
        ("去年五月发生了什么", INTENT_DATE),
        ("第一次约会是哪天", INTENT_DATE),
    ],
)
def test_recognizes_intents(q, intent):
    """§4 那张表的每一类都要能被识别出来，否则路由等于没做。"""
    assert classify(q).intent == intent


def test_photo_query_boosts_visual_leg():
    """photo 类的最佳腿是 visual（§4：visual 1.00 / 其余 0.00）。"""
    r = route("第一次旅游的照片")
    assert r.weights[LEG_VISUAL] > 1.0
    assert r.prefer_source_kinds == frozenset({"photo"})


def test_chat_query_boosts_sparse_not_dense():
    """🔑 chat 类 **dense=0.10 / sparse=1.00**（§4）。

    所以 chat 意图必须抬 sparse、**不能抬 dense** ——
    抬 dense 就是把该类唯一有效的那条腿压下去。
    """
    r = route("我们聊天记录里聊过什么")
    assert r.weights.get(LEG_SPARSE, 1.0) > 1.0
    assert r.weights.get(LEG_DENSE, 1.0) == 1.0, "dense 在 chat 类上几乎没用，不该抬"


def test_date_query_boosts_literal_matching():
    """§4 的失败模式之一：dense 召不回"哪天/什么时候"。"""
    r = route("去年五月发生了什么")
    assert r.weights.get(LEG_TRGM, 1.0) > 1.0
    assert r.weights.get(LEG_DENSE, 1.0) == 1.0


# ============================================================
# 2. 拿不准就不路由（D71 的延续）
# ============================================================


def test_no_intent_means_no_routing():
    """普通问句必须**保持等权**：路由是优化，不是默认行为。

    ⚠️ 样本要选**真的没有意图词**的问句。别用"登记表怎么填写"这种 ——
    它命中 `table` 是**对的**（§4 的 doc 类里表格问题该走字面匹配）。
    """
    r = route("食堂几点关门")
    assert r.intent == INTENT_GENERAL
    assert r.weights == {}
    assert r.prefer_kinds is None and r.prefer_source_kinds is None


def test_empty_query_does_not_route():
    r = route("")
    assert r.intent == INTENT_GENERAL and r.confidence == 0.0
    r2 = route("   ")
    assert r2.intent == INTENT_GENERAL


def test_low_confidence_falls_back_to_equal_weights():
    """门限存在的原因：猜错的代价是把结果过滤空。"""
    r = classify("去年五月去旅游拍的照片")   # 同时命中 photo + date
    assert r.confidence < 1.0
    forced = route("去年五月去旅游拍的照片", minimum_confidence=0.99)
    assert forced.intent == INTENT_GENERAL
    assert forced.weights == {} and forced.prefer_kinds is None
    assert "门限" in forced.why, "退回必须说清为什么（D72：不许静默）"


def test_min_confidence_default_is_sane():
    """门限不能是 0（那就是永远不路由）也不能 >0.9（几乎永远退回）。"""
    assert 0.0 < MIN_CONFIDENCE <= 0.6


def test_multiple_intents_pick_the_more_specific_one():
    """"去年五月去旅游拍的照片" 同时命中 photo 与 date，但用户要的是照片。

    时间线页（D20）才是按日期找内容的场景，检索里"照片"更具体。
    """
    r = classify("去年五月去旅游拍的照片")
    assert r.intent == INTENT_PHOTO
    assert r.confidence == 0.5, "多义命中必须降置信度"
    assert "同时命中" in r.why, "降置信度要说明原因"


# ============================================================
# 3. 偏好过滤是**软**的
# ============================================================


def test_prefer_filters_when_the_subset_is_non_empty():
    """photo 意图按**库类型**过滤：`kind=text` 的照片库 chunk 也该保留。

    （chunk kind 是什么取决于还没写的 D48 导入器，按它过滤会把照片库自己的
    结果滤掉 —— 实测 3 条只剩 1 条。）
    """
    hits = [
        _Hit(kind="text", source_kind="file"),
        _Hit(kind="image", source_kind="photo"),
        _Hit(kind="text", source_kind="photo"),
    ]
    kept, note = prefer(hits, route("第一次旅游的照片"))
    assert len(kept) == 2
    assert all(h.payload["source_kind"] == "photo" for h in kept)
    assert "过滤" in note


def test_prefer_falls_back_when_nothing_matches():
    """🔑 库里没有 photo 库时**不能返回 0 条** —— 那让用户以为"搜索坏了"。"""
    hits = [_Hit(kind="text", source_kind="file")]
    kept, note = prefer(hits, route("第一次旅游的照片"))
    assert kept == hits, "过滤失效必须退回全部"
    assert "不做过滤" in note, "退回要说明（D72）"
    assert "photo" in note, "要说清缺的是什么类型"


def test_prefer_no_preference_returns_everything():
    hits = [_Hit(kind="text"), _Hit(kind="chat")]
    kept, note = prefer(hits, route("登记表怎么填"))
    assert kept == hits


def test_prefer_is_order_stable():
    """过滤后顺序不变 —— 顺序就是 RRF 分数，动了就是改了排序。"""
    hits = [_Hit(kind="image", id=1), _Hit(kind="image", id=2), _Hit(kind="image", id=3)]
    kept, _ = prefer(hits, route("照片"))
    assert [h.payload["id"] for h in kept] == [1, 2, 3]


# ============================================================
# 4. 脏数据不许炸（Qdrant payload 是 JSONB，什么都可能有）
# ============================================================


def test_dirty_payload_does_not_crash():
    for bad in (None, "x", 42, [], {"kind": 1}, {"source_kind": {}}):
        h = _Hit(kind="text") if bad is None else _Hit(**{k: v for k, v in
                                                          (("kind", "text"),)})
        h.payload = bad
        kept, _ = prefer([h], route("照片"))
        assert len(kept) == 1, f"脏 payload {bad!r} 不该让检索整体 500"


def test_hit_without_payload_attribute():
    class Weird:
        pass

    w = Weird()
    kept, note = prefer([w], route("第一次旅游的照片"))
    assert len(kept) == 1
    assert "不做过滤" in note


# ============================================================
# 5. 日期抽取
# ============================================================


@pytest.mark.parametrize(
    "q,hint",
    [
        ("2023-05-01 那天", "2023-05-01"),
        ("2023年5月1日", "2023-05-01"),
        ("2023/5/1", "2023-05-01"),
        ("2023年5月", "2023-05-01"),
        ("二〇二三年五月一日", "2023-05-01"),
    ],
)
def test_extracts_absolute_dates(q, hint):
    r = route(q)
    assert r.intent == INTENT_DATE
    assert r.date_hint == hint


@pytest.mark.parametrize("q", ["2023-13-01", "2023-05-40", "99999年"])
def test_rejects_impossible_dates(q):
    """匹配到了但不是合法日期 → 抽不出，而不是造一个假日期出来。"""
    r = route(q)
    assert r.date_hint is None


def test_relative_dates_are_not_faked():
    """"去年五月" 抽不出具体日期 —— 那需要语料，不是字符串能算的。"""
    r = route("去年五月发生了什么")
    assert r.date_hint is None
    assert r.intent == INTENT_DATE, "抽不出日期也不该放弃路由（权重照样有用）"


def test_date_only_intent_extracts():
    """"哪天/什么时候"类 query 也可能带绝对日期。"""
    assert route("2024年3月那天我们做了什么").date_hint == "2024-03-01"


# ============================================================
# 6. 契约面：为什么必须可解释
# ============================================================


def test_every_route_explains_itself():
    """`why` 非空是硬要求：路由是猜的，用户要能一眼看出系统为什么这么排。"""
    for q in ("照片", "怎么填", "去年五月", "登记表怎么填写", "", "什么时候"):
        r = route(q)
        assert r.why.strip(), f"{q!r} 的路由没有解释"


def test_route_is_serialisable():
    """`/search/debug` 要把路由结论塞进响应，所以不能带不可序列化的东西。"""
    import json

    r = route("第一次旅游的照片")
    json.dumps(
        {
            "intent": r.intent,
            "weights": r.weights,
            "kinds": sorted(r.prefer_kinds) if r.prefer_kinds else None,
            "date_hint": r.date_hint,
            "confidence": r.confidence,
            "why": r.why,
        }
    )


def test_default_route_is_exactly_equal_weights():
    """🔑 D71：默认必须与今天**完全一样**（`fusion.DEFAULT_WEIGHTS` 全 1.0）。

    这是"路由不能影响既有行为"的保证 —— 没有它，
    每次加关键词都会悄悄改变线上排序（而没人说得清为什么变了）。
    """
    from services.retrieval.fusion import DEFAULT_WEIGHTS

    r = route("食堂几点关门")
    effective = {**DEFAULT_WEIGHTS, **r.weights}
    assert effective == DEFAULT_WEIGHTS