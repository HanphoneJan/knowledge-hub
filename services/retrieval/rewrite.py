"""查询改写（文章《RAG 优化》§Query Rewrite）—— **唯一**实现处。

## 为什么需要它

用户输入往往缺少检索需要的"可检索表达"：口语化、缩写、指代、含多个子问题。
文章给了六类策略（规范化 / Multi-Query / Query Decomposition / Step-back /
HyDE / Self-Query）。本项目按**外传与否**分成两层：

| 层 | 策略 | 外传？ |
|---|---|---|
| **规则层**（默认可开） | 规范化、**实体词抽取**（编号/错误码/SKU） | 否 |
| **LLM 层**（需显式配置外部 LLM） | Multi-Query 改写、HyDE | **是**（检索 query 会离开本机）|

## 🔴 两条不可违反的纪律

1. **原始 query 永远参与召回**，且排在第 0 位（`RewriteResult.queries[0]`）。
   改写模型把"退款"误解成"取消订阅"时，原始 query 仍是正确的召回入口
   （文章明确要求；`ask.expand_query` 的多轮扩展也是同一条精神）。
2. **失败即忽略，绝不影响原检索**：规则层是纯函数不会失败；LLM 层超时/报错/
   解析不出 JSON 一律返回 `[]` —— 与检索腿"挂了只 skipped"同一条纪律。

## 默认关闭

`rewrite_query` 的 LLM 分支要调用方显式 `use_llm=True` **且**库配置
`usable_for("rewrite")`。默认路径不调用它 ⇒ 行为与引入前逐字一致。
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field

from .bm25 import tokenize

logger = logging.getLogger(__name__)

#: 一次改写最多额外产出几条 query（含规则层与 LLM 层）。
#: ⚠️ 上限很重要：多查询 = 多轮召回 × 每轮三条腿，直接乘进 D46 的 500ms 预算。
DEFAULT_MAX_EXTRA = 2

#: 像"编号/错误码/SKU"的 token：字母数字混合、含连字符、长度够。
#: 这类词**必须**能在改写后保真（向量召不回它们，靠 BM25/trgm 兜）。
_ENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_\.]{3,}")

#: 数字串（学号、身份证、订单号）也归入实体。
_DIGIT_RE = re.compile(r"\d{5,}")


@dataclass
class RewriteResult:
    """改写结论。`queries[0]` **永远是原始 query**。"""

    queries: list[str]
    #: "off"（只原始）/ "rule" / "llm"
    source: str = "off"
    notes: list[str] = field(default_factory=list)


def normalize_query(q: str) -> str:
    """规范化：全角→半角（NFKC）、折叠空白。

    不改变词序、不删词 —— 只做"同一句话的另一种等价的写法"。
    """
    s = unicodedata.normalize("NFKC", q or "")
    return " ".join(s.split())


def entity_terms(q: str) -> list[str]:
    """抽出 query 里的**实体 token**（编号 / 错误码 / 长数字）。

    用途：把"设备 WIDGET-XYZ-8899 的固件版本"这类 query 抽出一个
    "只含实体"的变体，让 BM25/trgm 的精确通道不被自然语言词稀释。
    """
    seen: list[str] = []
    for tok in tokenize(q or ""):
        t = tok.strip()
        if len(t) < 4:
            continue
        if _ENTITY_RE.fullmatch(t) or _DIGIT_RE.fullmatch(t):
            if t not in seen:
                seen.append(t)
    return seen


def _dedup(seq: list[str]) -> list[str]:
    out: list[str] = []
    for x in seq:
        if x and x not in out:
            out.append(x)
    return out


def rule_variants(q: str, *, max_extra: int = DEFAULT_MAX_EXTRA) -> list[str]:
    """规则层变体（**不外传**）：规范化 + 实体词抽取。返回**不含**原始 query 的额外项。"""
    out: list[str] = []
    norm = normalize_query(q)
    if norm and norm != q:
        out.append(norm)
    ents = entity_terms(q)
    # 至少两个实体才值得单列一个"实体 query"，否则它等于原句里一个词
    if len(ents) >= 2 or (ents and any(_DIGIT_RE.fullmatch(e) for e in ents)):
        out.append(" ".join(ents))
    return _dedup(out)[:max_extra]


# ============================================================
# LLM 层（需显式配置外部 LLM）
# ============================================================

_REWRITE_SYSTEM = (
    "你是检索查询改写助手。为用户的检索问题生成若干**不同措辞**的检索查询，"
    "覆盖同义表达与可能的关键词，但**不得改变问题的本意**。"
    "⚠️ **不要重复原问题本身**，每条变体都要与原问题措辞不同。"
    "只输出一个 JSON 字符串数组，不要任何解释或 markdown 代码块。"
)


def _parse_variants(text: str, *, limit: int) -> list[str]:
    """从模型输出里解出变体列表。**任何异常都返回 `[]`**（不改写总比改错好）。"""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("[") :] if "[" in raw else raw
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        arr = json.loads(raw[start : end + 1])
    except (ValueError, TypeError):
        return []
    if not isinstance(arr, list):
        return []
    out = [str(x).strip() for x in arr if isinstance(x, str) and str(x).strip()]
    return _dedup(out)[:limit]


async def llm_variants(
    q: str, config, *, n: int = 3, timeout: float = 20.0
) -> list[str]:
    """调外部 LLM 产出 Multi-Query 变体。**失败一律 `[]`**（见模块纪律 2）。

    ⚠️ 用同步 `httpx.Client` + `anyio.to_thread`（与 `OpenAICompatGenerator`
    同一套）：`trust_env=False` 避开本机代理。凭据来自库配置。
    """
    if config is None or not getattr(config, "usable_for", lambda _f: False)("rewrite"):
        return []

    def _call() -> str:
        import httpx

        base = (config.base_url or "").rstrip("/")
        headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        }
        for k, v in (getattr(config, "extra_headers", None) or {}).items():
            if k and v is not None:
                headers[str(k)] = str(v)
        payload = {
            "model": config.model,
            "messages": [
                {"role": "system", "content": _REWRITE_SYSTEM},
                {
                    "role": "user",
                    "content": f"问题：{q}\n请给出最多 {n} 条变体。",
                },
            ],
            "temperature": 0.2,
            "stream": False,
        }
        with httpx.Client(timeout=timeout, trust_env=False) as c:
            r = c.post(f"{base}/chat/completions", json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
        return data["choices"][0]["message"]["content"]

    try:
        import anyio

        text = await anyio.to_thread.run_sync(_call)
    except Exception as exc:  # noqa: BLE001 —— 改写是增强，崩了只跳过
        logger.warning("查询改写（LLM）失败，忽略：%s", exc)
        return []
    return _parse_variants(text, limit=n)


async def rewrite_query(
    q: str,
    *,
    config=None,
    use_llm: bool = False,
    max_extra: int = DEFAULT_MAX_EXTRA,
) -> RewriteResult:
    """把一个问题改写成"原始 + 若干变体"。

    @param use_llm 调用方显式开启 LLM 改写。默认 False ⇒ 只做规则层。
    @param config 库里的 `LLMConfig`（`llm.load_llm_config`）。
    """
    queries = [q]
    notes: list[str] = []
    source = "off"

    if use_llm:
        vs = await llm_variants(q, config, n=max_extra)
        if vs:
            source = "llm"
            notes.append(f"LLM 生成 {len(vs)} 条改写")

    rule = rule_variants(q, max_extra=max_extra)
    for v in rule:
        if v not in queries:
            queries.append(v)
            if source == "off":
                source = "rule"

    # 原始 query 恒在第 0 位；总条数受限（见 DEFAULT_MAX_EXTRA 的成本说明）
    queries = _dedup(queries)[: 1 + max_extra]
    return RewriteResult(queries=queries, source=source, notes=notes)


__all__ = [
    "DEFAULT_MAX_EXTRA",
    "RewriteResult",
    "entity_terms",
    "llm_variants",
    "normalize_query",
    "rewrite_query",
    "rule_variants",
]
