"""`services/retrieval/rewrite.py` 的判据（纯函数，进 `deploy/ci.sh`）。

盯三条**性质**（不是某串输出）：
1. 规则层只做"等价改写"：不改词序/不删词，实体词保真；
2. **原始 query 恒在 `queries[0]`**（文章硬要求，也是改错时的兜底）；
3. LLM 层失败一律 `[]`，绝不抛 —— 改写是增强，不能拖垮原检索。
"""

from __future__ import annotations

from services.retrieval.rewrite import (
    RewriteResult,
    _parse_variants,
    entity_terms,
    llm_variants,
    normalize_query,
    rewrite_query,
    rule_variants,
)


class _FakeConfig:
    """最小 LLMConfig 替身：只实现 `usable_for`（`llm_variants` 用到的唯一方法）。"""

    def __init__(self, usable: bool) -> None:
        self._usable = usable

    def usable_for(self, feature: str) -> bool:
        return self._usable and feature == "rewrite"


def test_normalize_nfkc_fullwidth():
    """全角→半角（NFKC）+ 折叠空白；不改变词。"""
    assert normalize_query("２０２５年 政策") == "2025年 政策"
    assert normalize_query("  a   b  ") == "a b"


def test_entity_terms_keeps_ids():
    """长数字/编号要被识别为实体（向量召不回它们）。"""
    assert "123456789" in entity_terms("订单号 123456789 怎么查")
    # ⚠️ 用**明显合成**的 18 位串，别放真实证件号（本仓库公开）。
    assert "123456789012345678" in entity_terms("身份证 123456789012345678")


def test_rule_variants_extracts_entity_query():
    """实体含量足够时产出一个"只含实体"的变体（喂 BM25/trgm）。"""
    out = rule_variants("订单号 123456789 怎么查")
    assert "123456789" in out


def test_rule_variants_never_changes_wording_order():
    """规范化变体 = 等价写法；不含任何新词（只可能全角转半角）。"""
    out = rule_variants("２０２５年政策")
    joined = " ".join(out)
    assert "2025年政策" in joined


def test_rewrite_puts_original_first_and_rules():
    """🔑 原始 query 恒在第 0 位；规则层附加，source=rule。"""
    import functools

    import anyio

    rr = anyio.run(
        functools.partial(rewrite_query, "订单号 123456789 怎么查", use_llm=False)
    )
    assert isinstance(rr, RewriteResult)
    assert rr.queries[0] == "订单号 123456789 怎么查"
    assert "123456789" in rr.queries
    assert rr.source == "rule"


def test_rewrite_no_variant_when_nothing_to_add():
    """没有可加的变体时不硬造（source=off，只有原始一条）。"""
    import functools

    import anyio

    rr = anyio.run(
        functools.partial(rewrite_query, "入职日期那栏填什么", use_llm=False)
    )
    assert rr.queries[0] == "入职日期那栏填什么"
    assert rr.source in ("off", "rule")


def test_llm_variants_returns_empty_without_usable_config():
    """配置不可用（未配外部 LLM / 该功能开关关）⇒ 不调用模型，返回 []。"""
    import anyio

    assert anyio.run(llm_variants, "问题", None) == []
    assert anyio.run(llm_variants, "问题", _FakeConfig(usable=False)) == []


def test_llm_variants_swallows_failure():
    """可用配置但请求失败（这里 base_url 无效）⇒ 返回 []，绝不抛。"""
    import anyio

    class _BadConfig(_FakeConfig):
        base_url = "http://127.0.0.1:1/v1"
        model = "x"
        api_key = "k"
        extra_headers: dict = {}

    assert anyio.run(llm_variants, "问题", _BadConfig(usable=True)) == []


def test_parse_variants_handles_code_fences_and_garbage():
    """模型输出格式不听话时也要解得出来；解不出就 []。"""
    assert _parse_variants('```json\n["退款流程","退钱"]\n```', limit=5) == [
        "退款流程",
        "退钱",
    ]
    assert _parse_variants("抱歉，我无法回答", limit=5) == []
    assert _parse_variants("", limit=5) == []


def test_rewrite_llm_fallback_to_rules_when_config_missing():
    """开了 use_llm 但没有可用配置 ⇒ 退回规则层，不报错。"""
    import functools

    import anyio

    rr = anyio.run(
        functools.partial(rewrite_query, "订单号 123456789", config=None, use_llm=True)
    )
    assert rr.queries[0] == "订单号 123456789"
    assert "123456789" in rr.queries
