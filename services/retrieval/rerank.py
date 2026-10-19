"""Reranker（阶段 3 · M6）：cross-encoder 精排。

## 为什么默认**关闭**

1. **它不能重排召回，只能重排候选**：它拿到的是 RRF 之后的 top-N，
   真值不在候选里就救不回来。所以它是"提升"而不是"解决"。
2. **延迟**：CPU 上 bge-reranker-base 每对 (query, doc) 约 30~60ms，
   20 个候选就是 0.6~1.2s —— 而 `hybrid` 现在 P95 已经 1s 量级。
   默认开启等于把每次检索都变慢，而**收益依赖语料**（§4 的 photo 类
   靠路由解决更划算）。
   → 所以按 KB 配置开启（`rerank_id`，与 RAGFlow 对齐，见 REQUIREMENTS 的
   外部方案对比表），默认空 = 完全不改变今天的行为。

## 不可用时怎么办：如实 skipped，不静默降级

与视觉腿同一套约定（§4.1）：`rerank_available()` 为假时
`rerank()` 返回 `RerankOutcome(skipped=...)` 并且**不动原顺序**。

⚠️ 绝不能"静默降级成抽取式"那种做法（D73 拒绝 `openai_compat` 时也是
同一个理由）：用户会以为自己在用 Reranker，实际没有。

## 排序稳定性：同分按原 RRF 顺序

`bge-reranker-base` 对长度相近的短文本经常给出相同/极近的分数。
Python 的 `sorted` 是稳定的，所以只要**不给它新的比较键**，
同分就保持原顺序。⚠️ 这里排序键是 `(score, -原下标)` 的组合 —— 
直接 `sorted(key=lambda i: -scores[i])` 也稳定，但显式写成
`key=(-score, index)` 能让"同分保持原序"变成**可读的性质**而不是巧合。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: 默认模型。⚠️ 换模型会**改变排序**，所以它是配置项而不是常量，
#: 且改动要按 D22 的口径（全局统一 + 记进 PROGRESS）。
#:
#: 🔑 **为什么是 v2-m3 而不是更小的 base**（实测，`eval/rerank_probe.py`，
#: 8 条样本 = 4 组 query×正负例）：成对判别 base **2/4**、v2-m3 **4/4**。
#: base 翻车的正是本项目语料的主体（中文公文"入职日期那栏该填什么"、
#: "候选人登记表怎么填写"）—— 它把**标题行**排在**答案段**之上。
#: 代价是单对 53ms → 114ms（2.2×），这正是"默认关闭、按库开启"的另一个理由。
DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-v2-m3"

#: 单次打分的最大字符数。⚠️ 必须截断：BERT 的位置编码上限是 512 token，
#: 超了会静默截断到 512 —— 那会让"第 3 段的关键信息"排在"第 1 段的废话"
#: 后面，而看起来一切正常（这也是长文档 rerank 最常见的坑）。
#: ⚠️ 实测：1200 个中文字 ≈ 1800 token > 512，所以真正生效的是 tokenizer 的
#: `truncation=True`（512 token）。这个值只防"整段被丢光"，不负责精确控制。
MAX_CHARS = 1200


@dataclass
class RerankOutcome:
    """精排结果（含**为什么没排**）。"""

    items: list = field(default_factory=list)
    model: str = ""
    elapsed_ms: int = 0
    skipped: str | None = None
    #: 每个候选的精排分（`key` → 分），`/search/debug` 用来看"谁被提上来了"
    scores: dict[str, float] = field(default_factory=dict)

    @property
    def applied(self) -> bool:
        return self.skipped is None


_model = None
_model_name = ""
_model_err: str | None = None
_lock = threading.Lock()


def model_name(kb_rerank: str | None = None) -> str:
    """要用的模型名。**空串 = 不启用**。

    🔑 ⚠️ `kb_rerank=None` 返回**空串**而不是全局默认值。第一版
    `if kb_rerank is not None: return kb_rerank` 写成了"None 就用默认"，
    于是没配 `rerank_model` 的库**也在跑精排**（实测 `applied=true`、5.2s）——
    正是这条注释自己警告的"per-KB 开关名存实亡"。
    教训：把 `None` 定义成"未指定→用默认"很方便，但一旦有"per-KB 开关"
    这个语义，`None` 就必须是**明确的不启用**，否则用户关不掉。

    想要"全局默认模型"请显式传 `default_model()`。
    """
    return (kb_rerank or "").strip()


def default_model() -> str:
    """全局默认模型名（配置项，可被环境变量覆盖）。"""
    from ..common.config import get_settings

    return get_settings().rerank_model


def available(name: str | None = None) -> bool:
    """能不能跑 Reranker（**不触发加载**）。

    @param name 某个模型名。⚠️ 传了它就问"那个模型加载过没有" ——
        没说"这个进程里存在任何一个模型"（那对 per-KB 开关毫无意义）。
    """
    if name is not None:
        return _model is not None and _model_name == name
    return _model is not None or (not _model_err and not _DISABLED)


#: `RERANK_MODEL=""` = 全局显式关掉。⚠️ 与"没装模型"要能区分：
#: 前者是运维的决定（报"已禁用"），后者是环境问题（报"加载失败"+原因）。
_DISABLED = False


def _load(name: str | None = None):
    """懒加载模型，**每个模型名只加载一次**，进程内共享。

    🔑 ⚠️ `name` 由**调用方**给（`rerank()` 从 per-KB 配置传下来）。
    第一版这个函数自己去读全局默认，于是 `kb.rerank_model` 写什么都跑
    同一个模型 —— 配置成了装饰，而 `/search/debug` 显示的模型名又是"真的"，
    所以从界面上完全看不出没生效。

    ⚠️ 失败不抛异常而是记下原因：Reranker 是可选增强，
    一个库没装模型不该让整个检索 500。
    """
    global _model, _model_name, _model_err, _DISABLED
    want = model_name(name) or default_model()
    with _lock:
        # ⚠️ 换模型要重新加载：缓存命中判断必须比**模型名**，
        #   只判"有没有加载过"会让第二个库配的模型静默不生效
        if _model is not None and _model_name == want:
            return _model
        if _model_err is not None and _model_name == want:
            return None
        if not want:
            _DISABLED = True
            _model_name = ""
            _model_err = "已通过 RERANK_MODEL=\"\" 显式禁用"
            return None
        _model, _model_name, _model_err = None, want, None
        try:
            from transformers import AutoModelForSequenceClassification, AutoTokenizer

            tok = AutoTokenizer.from_pretrained(want)
            mdl = AutoModelForSequenceClassification.from_pretrained(want)
            mdl.eval()
            _model, _model_name = (tok, mdl), want
            logger.info("Reranker 已加载：%s", want)
        except Exception as exc:  # noqa: BLE001
            _model_err = f"{type(exc).__name__}: {exc}"
            logger.warning("Reranker 加载失败（不重试，检索照常工作）：%s", _model_err)
        return _model


def score_passages(query: str, passages: list[str]) -> list[float]:
    """(query, passage) → 相关性分，越大越相关。

    ⚠️ 截断到 `MAX_CHARS`：超长输入会被模型静默截到 512 token，
    表现为"后面的段落排序异常低"而没有任何报错。
    """
    tok, mdl = _load()
    assert tok is not None and mdl is not None, "score_passages 前必须先 rerank()"
    import torch

    # 再截一次是**防御**：本函数是公开原语，不能假设调用方都截过了
    pairs = [[query, p[:MAX_CHARS]] for p in passages]
    with torch.inference_mode():
        batch = tok(
            pairs, padding=True, truncation=True, max_length=512, return_tensors="pt"
        )
        logits = mdl(**batch).logits.view(-1).float()
    return logits.tolist()


def rerank(
    query: str,
    hits: list,
    *,
    limit: int = 20,
    kb_rerank: str | None = None,
) -> RerankOutcome:
    """对融合后的候选做 cross-encoder 精排。

    @param limit 只精排前 `limit` 个候选。⚠️ 候选之外的**保持原顺序接在后面**
        （不是丢掉）—— 精排是"提升"不是"过滤"（模块 docstring 第一条）。
    @param kb_rerank 该库的 `rerank_model`。⚠️ 传 `None`/`""` = **不启用**
        （直接 skipped），不 fallback 到全局默认 —— 否则 per-KB 开关名存实亡。
    @return `items` 是**新列表**，入参 `hits` 不改（调用方可能还要用旧顺序）。
    """
    t0 = time.perf_counter()
    name = model_name(kb_rerank)
    if not name:
        # ⚠️ 早期返回（不碰模型、不加载）："没配置"是最常见的分支，
        #   每次请求都去问一遍"能不能加载"纯属浪费
        return RerankOutcome(
            items=list(hits),
            model="",
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
            skipped="该库未配置 rerank_model（不启用精排）",
        )
    items = list(hits)
    if not items:
        return RerankOutcome(items=items, model=name,
                             elapsed_ms=int((time.perf_counter() - t0) * 1000),
                             skipped="没有候选可排")

    head, tail = items[:limit], items[limit:]
    if _load(name) is None:
        return RerankOutcome(
            items=items,
            model=name,
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
            skipped=_model_err or "Reranker 不可用",
        )

    try:
        passages = [_passage_text(h) for h in head]
        scores = score_passages(query, passages)
    except Exception as exc:  # noqa: BLE001
        # ⚠️ 精排失败**保持原顺序**，不抛 —— 排序是增强，
        #   崩掉整个检索是本末倒置（D73 同款理由）
        return RerankOutcome(
            items=items,
            model=name,
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
            skipped=f"精排失败: {type(exc).__name__}: {exc}",
        )

    order = sorted(range(len(head)), key=lambda i: (-scores[i], i))
    return RerankOutcome(
        items=[head[i] for i in order] + tail,
        model=name,
        elapsed_ms=int((time.perf_counter() - t0) * 1000),
        scores={getattr(h, "key", str(i)): scores[i]
                for i, h in enumerate(head)},
    )


def _passage_text(hit) -> str:
    """候选的可读文本（**已截断到 `MAX_CHARS`**）。

    ⚠️ 优先 `text_norm`（归一化正文，D35）：Reranker 要判的是"这段讲了什么"，
    而 `text_raw` 可能带页眉页脚噪声。⚠️ payload 可能是脏的（JSONB），
    取不到就给空串（会被截断成短 passage，得分低但不会崩）。

    🔑 **截断在这一层做，不藏在 `score_passages` 里**：截断是"候选怎么变成
    输入"的决定，属于边界；而 `score_passages` 是个"给这些文本打分"的薄原语。
    藏在里面的话，从外面（测试、别的调用方）根本看不到它截了。
    """
    pl = getattr(hit, "payload", None)
    if not isinstance(pl, dict):
        return ""
    text = str(pl.get("text_norm") or pl.get("text_raw") or "")
    return text[:MAX_CHARS]


__all__ = [
    "DEFAULT_RERANK_MODEL",
    "default_model",
    "MAX_CHARS",
    "RerankOutcome",
    "available",
    "model_name",
    "rerank",
    "score_passages",
]