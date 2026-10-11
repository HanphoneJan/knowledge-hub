"""切分策略注册表（D164）—— 六种切分方式的**唯一**实现处。

仿 `parsers.PARSERS`：**加一种策略 = 在 `STRATEGIES` 加一条**，API 白名单
与 KB 校验都从它派生（见 `strategy_names()`），不会再出现"白名单里有、实现
没有"的漂移。

## 为什么要有这一层

审计（`DESIGN-HISTORY.md §2`）发现：项目原来只有**一种**切分方式
（`chunker.chunks_from_markdown` 的 标题 > 段落 > 硬切），而 JavaGuide
《RAG 文档处理与切分策略》列了六种。不同文档/查询需要不同切法，
把它们做成 **KB 级单选**（默认 `recursive` = 现行为，基线逐字不变）。

| 策略 | 边界 | 编码 | 备注 |
|---|---|---|---|
| `fixed` | 定长 + overlap | 标准 | 评测基线 |
| `recursive`（默认）| 标题 > 段落 > 硬切 | 标准 | 现行为 |
| `semantic` | 句子 embedding 相似度 | 标准 | ⚠️ PDF 保持结构切分 |
| `parent_child` | 子＝递归块，父＝章节/页 | 标准 | 召回小子块、上下文用父块 |
| `late` | 同 recursive | **Late** | 整篇前向、按 span 池化（`pool.encode_late_spans`）|
| `contextual` | 同 recursive | 标准 | 每块前置 LLM 上下文；未配 LLM 则降级 |

## 设计要点（都是踩过/想过才会立的规矩）

- **正交性取舍**：`chunk_strategy` 是**单选 profile**。Parent-Child 与 Late
  理论上可叠加，但组合爆炸，本期不做（记在计划文档「后续」）。
- **PDF 保坐标**：`semantic` 会重排文字，破坏 L3 坐标 ⇒ 对 PDF **不执行**，
  记一条 note 后保持结构切分（结构与坐标都由 `locations.py` 产出）。
- **依赖注入**：`embed` / `llm` 都是可注入的 callable —— 单测给假实现，
  生产由 pipeline 传 `retrieval.pool.encode_texts` 与 `contextualizer`。
  **本模块不 import 任何模型**，只调注入进来的东西。
- **失败必降级**：任何一种策略抛错都退回 `recursive` 并记 note，
  绝不因为"想用新切法"让摄取失败（与视觉腿"挂了只 skipped"同一条纪律）。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from .chunker import Chunk, _hard_split

logger = logging.getLogger(__name__)

# ============================================================
# 常量
# ============================================================

#: 支持的策略名。**加一种就加到这里 + `STRATEGIES`**（两处，但由本模块唯一持有）。
STRATEGY_NAMES: tuple[str, ...] = (
    "fixed",
    "recursive",
    "semantic",
    "parent_child",
    "late",
    "contextual",
)

#: 默认策略 = 现行为。改这个等于改基线，**不要**。
DEFAULT_STRATEGY = "recursive"

#: 语义切分：相邻句子的余弦低于它就认为"话题转了"，在够大时断开。
SEMANTIC_THRESHOLD = 0.55

#: 父块文本上限（payload / PG 都存这一份）。文章说父块是上下文，
#: 不是全文；太大反而淹没证据（`ask.MAX_EVIDENCE_CHARS` 也会截）。
MAX_PARENT_CHARS = 2000

#: 句子边界：中文句末标点 + 换行。保留标点，切出来能原样拼回原文。
_SENT_SPLIT = re.compile(r"[^。！？!?\n]+[。！？!?]?|\n")


# ============================================================
# 数据结构
# ============================================================


@dataclass
class ChunkContext:
    """一次切分需要的外部信息。默认值让"只想跑 recursive"的调用方好写。"""

    #: 整篇归一化文本（semantic / parent_child / late 用）。
    #: ⚠️ 对文本格式它是各 chunk 用空行拼回的近似；对 PDF 由调用方决定。
    full_text: str | None = None
    size: int = 500
    overlap: int = 50
    #: 句向量编码器（`list[str] -> list[list[float]]`）。不给则 semantic 降级。
    embed: Callable[[list[str]], list[list[float]]] | None = None
    #: 上下文生成器（`(doc_text, chunk_text) -> context`）。不给则 contextual 降级。
    llm: Callable[[str, str], str] | None = None
    #: 这份是不是 PDF（决定能不能重排边界 —— PDF 重排会丢坐标）。
    is_pdf: bool = False


@dataclass
class StrategyResult:
    """策略产物：切好的块 + 编码方式 + **降级原因**（要能被人看到）。"""

    chunks: list[Chunk]
    #: "standard" = `pool.encode_texts`；"late" = `pool.encode_late_spans`。
    encoding: str = "standard"
    notes: list[str] = field(default_factory=list)


# ============================================================
# 工具：句切分 / 余弦
# ============================================================


def split_sentences(text: str) -> list[str]:
    """切句子。**保留标点**，拼回去等于原文（去掉纯空白片段）。"""
    out: list[str] = []
    for m in _SENT_SPLIT.finditer(text or ""):
        piece = m.group(0)
        if piece.strip():
            out.append(piece)
    return out


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ============================================================
# 各策略实现
# ============================================================


def fixed_split(text: str, size: int, overlap: int) -> list[Chunk]:
    """定长硬切（评测基线）。复用 `chunker._hard_split` 的标点回退逻辑。"""
    return [Chunk(text=p, kind="text") for p in _hard_split(text or "", size, overlap) if p.strip()]


def semantic_split(
    text: str,
    *,
    size: int,
    overlap: int,
    embed: Callable[[list[str]], list[list[float]]],
    threshold: float = SEMANTIC_THRESHOLD,
) -> list[Chunk]:
    """语义切分：句子按 embedding 相似度分组。

    规则（**先满足大小，再看语义**）：
    1. 一个块的字符数达到 `size` 就断开（硬上限，避免句向量主题一路连下去）；
    2. 当前块已达到 `min_size`（= size//3）且与下一句相似度 < `threshold` 时断开；
    3. 结尾剩余句子即便没达到 `min_size` 也收尾（不留尾巴）。

    ⚠️ **不做 overlap**：语义边界本身就是"完整句子组"，再叠 overlap 只会
    把上一话题的句子塞进下一块，抵消语义切分的意义。这一点是刻意的。
    """
    sents = split_sentences(text)
    if not sents:
        return []
    if len(sents) == 1:
        return [Chunk(text=sents[0].strip(), kind="text")]

    vecs = embed(sents)
    min_size = max(size // 3, 1)

    groups: list[list[str]] = []
    cur: list[str] = [sents[0]]
    cur_len = len(sents[0])

    for i in range(1, len(sents)):
        s = sents[i]
        sim = _cosine(vecs[i - 1], vecs[i])
        would_overflow = cur_len + len(s) > size
        topic_turn = sim < threshold and cur_len >= min_size
        if would_overflow or topic_turn:
            groups.append(cur)
            cur, cur_len = [], 0
        cur.append(s)
        cur_len += len(s)
    if cur:
        groups.append(cur)

    return [
        Chunk(text="".join(g).strip(), kind="text")
        for g in groups
        if "".join(g).strip()
    ]


def attach_parents(
    chunks: list[Chunk],
    *,
    by_page: bool = False,
    max_parent: int = MAX_PARENT_CHARS,
) -> list[Chunk]:
    """Parent-Child：给每个子块挂上父块文本（就地改并返回）。

    分组键：
    - `by_page=True`（PDF）：按 `chunk.page` —— 一页就是一个父块；
    - 否则：按 `chunk.section_path`；没有章节的块按 ~`max_parent` 字符
      **顺序分桶**（避免整篇变成一个巨大父块）。

    父块文本 = 同组子块文本用换行拼接，超 `max_parent` 截断。
    🔑 子块本体**一个字段都不改**（`chunk_index` / 坐标 / 文本都不动）——
    召回靠子块、上下文靠父块，正是文章要的折中。
    """
    if not chunks:
        return chunks

    groups: list[list[Chunk]] = []
    if by_page:
        buckets: dict[int, list[Chunk]] = {}
        for c in chunks:
            buckets.setdefault(c.page or 1, []).append(c)
        groups = list(buckets.values())
    else:
        cur: list[Chunk] = []
        cur_len = 0
        cur_key: str | None = None
        for c in chunks:
            key = c.section_path or ""
            # 有明确章节：同一章节归一组（章节变了就收口）
            if key and key != cur_key:
                if cur:
                    groups.append(cur)
                cur, cur_len, cur_key = [c], len(c.text), key
                continue
            if not key and cur_len + len(c.text) > max_parent:
                if cur:
                    groups.append(cur)
                cur, cur_len, cur_key = [], 0, key
            cur.append(c)
            cur_len += len(c.text)
        if cur:
            groups.append(cur)

    for group in groups:
        parent = "\n".join(c.text for c in group).strip()
        if len(parent) > max_parent:
            parent = parent[:max_parent]
        for c in group:
            c.parent_text = parent
    return chunks


def contextualize(
    chunks: list[Chunk],
    *,
    full_text: str,
    llm: Callable[[str, str], str],
) -> list[Chunk]:
    """Contextual Chunking：给每块前置一句 LLM 生成的上下文。

    ⚠️ 这会**改变 chunk 文本**（`content_norm`），从而改变 dense/sparse 两条腿的
    向量 —— 这正是目的（让块"知道自己属于哪篇的哪一段"）。原始片段不再单独
    保留，因此**只在用户显式选了 contextual 且配了 LLM** 时才走这条路。
    """
    for c in chunks:
        try:
            ctx = (llm(full_text, c.text) or "").strip()
        except Exception:  # noqa: BLE001 — 单块失败不能毁整篇
            logger.warning("contextual：生成上下文失败，跳过这一块", exc_info=True)
            continue
        if ctx:
            c.text = f"[上下文] {ctx}\n{c.text}"
    return chunks


def spans_for(full_text: str, chunks: list[Chunk]) -> list[tuple[int, int]]:
    """子块在整篇文本里的字符区间（Late Chunking 池化用）。

    顺序 `find` 累加游标；找不到（被清洗改写过）就退回"沿用上一区间末尾"，
    保证**长度一致**（下游按下标一一对应，长度错了会静默错位）。
    """
    spans: list[tuple[int, int]] = []
    pos = 0
    for c in chunks:
        idx = full_text.find(c.text, pos)
        if idx < 0:
            idx = min(pos, len(full_text))
        end = min(idx + len(c.text), len(full_text))
        spans.append((idx, end))
        pos = end
    return spans


# ============================================================
# 派发
# ============================================================


def _pdf_fallback(chunks: list[Chunk], reason: str) -> StrategyResult:
    return StrategyResult(chunks, notes=[reason])


def apply_strategy(
    chunks: list[Chunk],
    *,
    strategy: str,
    context: ChunkContext,
) -> StrategyResult:
    """把策略应用到**已经切好的块**上。

    @param chunks 解析器给出的初始块（PDF 带坐标、文本格式无坐标）
    @param context 见 `ChunkContext`

    🔑 **`recursive` 原样返回**：初始块本来就是"标题 > 段落 > 硬切"的产物，
    再套一层等于重复切。这也保证默认路径的字节级不变（基线守卫）。
    """
    strategy = (strategy or DEFAULT_STRATEGY).lower()
    if strategy not in STRATEGY_NAMES:
        logger.warning("未知切分策略 %r，退回 %s", strategy, DEFAULT_STRATEGY)
        return StrategyResult(chunks, notes=[f"未知策略 {strategy!r}，已退回默认"])

    if strategy == "recursive":
        return StrategyResult(chunks)

    text = context.full_text if context.full_text is not None else "\n\n".join(
        c.text for c in chunks
    )

    # PDF 会因重排丢坐标 —— fixed / semantic 对它不执行（保持结构切分）。
    if context.is_pdf and strategy in ("fixed", "semantic"):
        return _pdf_fallback(
            chunks,
            f"策略 {strategy} 会重排文字、破坏 L3 坐标，PDF 保持结构切分",
        )

    try:
        if strategy == "fixed":
            return StrategyResult(fixed_split(text, context.size, context.overlap))

        if strategy == "semantic":
            if context.embed is None:
                return _pdf_fallback(
                    chunks, "semantic 需要句向量编码器，本进程没提供，退回结构切分"
                )
            return StrategyResult(
                semantic_split(
                    text, size=context.size, overlap=context.overlap,
                    embed=context.embed,
                )
            )

        if strategy == "parent_child":
            return StrategyResult(attach_parents(chunks, by_page=context.is_pdf))

        if strategy == "late":
            # 边界不变，只是告诉调用方"用 late 编码"。
            return StrategyResult(chunks, encoding="late")

        if strategy == "contextual":
            if context.llm is None:
                return StrategyResult(
                    chunks,
                    notes=["contextual 需要外部 LLM，未配置 ⇒ 退回 recursive 切分"],
                )
            return StrategyResult(
                contextualize(chunks, full_text=text, llm=context.llm)
            )
    except Exception as exc:  # noqa: BLE001 — 策略失败不能毁摄取
        logger.warning("切分策略 %s 失败，退回默认：%s", strategy, exc, exc_info=True)
        return StrategyResult(chunks, notes=[f"策略 {strategy} 失败，已退回默认：{exc}"])

    return StrategyResult(chunks)  # pragma: no cover — 上面的分支已穷尽


#: 派发记录（供守卫扫"每个名字都有实现"）。值只是标签，真正分派在 `apply_strategy`。
STRATEGIES: dict[str, str] = {
    "fixed": "fixed_split",
    "recursive": "identity",
    "semantic": "semantic_split",
    "parent_child": "attach_parents",
    "late": "identity+late_encoding",
    "contextual": "contextualize",
}


def strategy_names() -> tuple[str, ...]:
    """给 API 白名单/KB 校验用的名字集合（**从注册表派生**，别手写第二份）。"""
    return tuple(STRATEGIES)


__all__ = [
    "ChunkContext",
    "StrategyResult",
    "STRATEGIES",
    "STRATEGY_NAMES",
    "DEFAULT_STRATEGY",
    "apply_strategy",
    "attach_parents",
    "contextualize",
    "fixed_split",
    "semantic_split",
    "spans_for",
    "split_sentences",
    "strategy_names",
]
