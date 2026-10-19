"""🔑 查询类型路由（阶段 3 · M6）—— **photo 类 R@5=0.00 的正解**。

## 为什么路由而不是调 RRF 权重

实测（`PROGRESS.md` §4，41 条人工标注 query；⚠️ **视觉腿接通前**测的，
`visual` 那一列当时是写进去的 —— 重测结论见下方「重测状态」）：

| 类别 | dense | sparse | visual | 等权 RRF |
|---|---|---|---|---|
| chat | 0.10 | **1.00** | 0.00 | 0.90 |
| photo | 0.00 | 0.00 | **1.00** | **0.60** |
| blog | **1.00** | 0.88 | 0.00 | 0.88 |

等权 RRF 在**每个类别上都没赢过该类的最佳单腿** —— 因为一条固定权重
不可能同时是 chat 的最优和 photo 的最优。所以问题不是"k 值不对"，
而是"该按 query 的类型换一套权重"。

## 三条设计原则

### 1. 拿不准就**不路由**（D71 的延续）

`confidence < MIN_CONFIDENCE` 时返回**等权 + 不过滤**，
与今天的行为完全一致。宁可少优化一点，也不要用一个猜错的意图
把结果过滤空 —— 那是"看起来在优化、实际在制造召回事故"。

### 2. 过滤是**软偏好**，不是硬排除

`prefer_kinds` / `prefer_source_kinds` 先算一遍过滤后的结果；
**过滤后为空就退回未过滤**，并在 `why` 里如实说明
（"意图是找照片，但库里没有 photo 库"）。硬排除 + 空结果 = 用户看到 0 条。

### 3. 路由只调**腿权重 + 偏好**，不调 `k`

`RRF_K` 保持 60：那是对"排名尺度"的定义，不是分类器旋钮（D71 实测：
调 k 解决不了 photo 问题）。

## 权重从哪来

每个意图的权重都对应上面那张表里**该类的最佳腿**：
photo → visual 3.0（**实测值**，D159：能让 photo 点把 `top_k*2` 的融合池装满的
最小权重；机制见 `_INTENT_SPEC` 里的注释），chat → sparse 1.6，
scan → visual 2.0 + sparse 1.2。
**不是拍脑袋的整数**，而是"让最佳腿的贡献不低于其它腿之和"这个方向。

## 重测状态（D194 的 `eval/retrieval_eval.py`，2026-10-15）

- ✅ **photo 行已重测**（接通视觉腿之后）：在持久 keeper 库的 8 张图（形状/配色可判）上，
  每条腿 doc 级命中：`dense/sparse/trgm = 0.000`，**`visual` Hit@1 = 0.750 / Hit@5 = 1.000**。
  ⇒ `photo → visual` 这条路由**成立且必要**（文本腿一条都召不回照片），权重无需改动。
- ✅ **date 行已重测并修正**：8 条真实聊天按天查询，单腿 Hit@1 `sparse` **0.875** /
  `dense` 0.250 / `trgm` **0.000**。旧的 `{trgm:1.5, sparse:1.3}` 让 dense 压过 sparse
  （plain hybrid 的 date Hit@1 = **0.000**）；改成 **`{sparse: 2.5}`** 后 hybrid date
  Hit@1 = **0.625**、Hit@5 = 1.000，总体 hybrid Hit@1 **0.400 → 0.543**。
- ✅ 文本类别（doc/blog/entity）四条腿的每腿数字见 `reports/retrieval_eval.json`。
- ✅ **table 行已重测**（3 条"哪一栏/怎么填"真实表格查询）：单腿 Hit@5 `sparse` **0.333** /
  `dense` 0.333 / `trgm` **0.000**；`hybrid` Hit@5 = 0.333（**不低于最佳单腿**）。
  ⚠️ 样本小（3 条），且 `trgm` 仍是 0 —— 与 date 行同一现象（字面 boost 对"填表"没召回），
  但**先不改**（3 条不足以拍板；再改就是过拟合）。
- ✅ **scan 行已重测**（`eval/scan_recheck.py`）：把**真实 PDF** 的前 4 页栅格化成**无文本层**
  的 PDF（真扫描件形态）、用视觉服务编码 → 每条腿 doc 级命中 `dense/sparse/trgm = 0.000`、
  **`visual` Hit@1 = 1.000 / Hit@5 = 1.000** ⇒ `scan → visual` 成立且必要。

## 不做什么

- ❌ 不做"学习出来的"路由（要标注数据 + 训练管线，40 条 query 不够）
- ❌ 不改 `MODE_LEGS`（semantic/exact 的语义由用户选，不由路由器猜）
- ❌ 不猜 LLM（那要在 colSmol 的 15s/图之外再加一次 LLM 调用，见 D73 的立场）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .types import LEG_DENSE, LEG_SPARSE, LEG_TRGM, LEG_VISUAL

#: 低于这个置信度**不路由**（原则 1）。
MIN_CONFIDENCE = 0.35

INTENT_GENERAL = "general"
INTENT_PHOTO = "photo"
INTENT_SCAN = "scan"
INTENT_TABLE = "table"
INTENT_CHAT = "chat"
INTENT_DATE = "date"


@dataclass(frozen=True)
class Route:
    """一次路由的结论（纯数据，可序列化给 `/search/debug` 看）。

    @param weights 腿权重。**没列出的腿按 1.0**（`fusion.rrf` 的行为）。
    @param prefer_kinds 偏好这些 chunk kind（`text`/`table`/`page`/`chat`）。
        软偏好：过滤后为空会自动退回，`why` 里会说明。
    @param prefer_source_kinds 偏好这些 KB 的 `source_kind`
        （`file`/`chat`/`photo`）。⚠️ KB 级的属性（`kb.source_kind`），
        不是文档的 mime —— 一个 photo 库里全是照片。
    @param date_hint date 意图时抽出的日期（`YYYY-MM-DD`），仅用于展示与后续过滤
    @param why 人话解释。⚠️ **必填**：路由是"猜的"，猜错了用户要能一眼看出
        系统为什么这么排（D72 的精神：不许静默做决定）
    """

    intent: str
    weights: dict[str, float] = field(default_factory=dict)
    prefer_kinds: frozenset[str] | None = None
    prefer_source_kinds: frozenset[str] | None = None
    date_hint: str | None = None
    confidence: float = 0.0
    why: str = ""


# ============================================================
# 词表
# ============================================================

#: 照片/图片类。⚠️ 关键词要覆盖**口语**（"照片""拍的""合影"），
#: 只写"照片"会让"我拍的合影在哪里"路由不到（实测踩过这类漏判）。
_PHOTO = r"照片|图片|相片|合影|自拍|拍照|拍的|镜头|照片墙"
#: 扫描件类。
#: ⚠️ **不要把 `申请表`/`登记表`/`表格截图` 放进来**（第一版放过，踩了）：
#:   "候选人登记表怎么填写" 会命中 scan → 被路由成 `visual 2.0` +
#:   偏好 `page`/`table` 类，而 §4 实测 doc 类（13 条）dense/sparse 都是 0.92
#:   —— 那是文本腿的强项。**表单的名字不等于"我要找扫描件那一份"**。
_SCAN = r"扫描|扫描件|扫描版|影印件|拍下来的那份|拍的那份|复印的"
#: 表格结构类：问的是"哪一栏/怎么填"而不是"这份文档说了什么"。
_TABLE = r"哪一栏|哪栏|第几栏|怎么填|填写|哪一列|几行几列|表头|行高|单元格"
#: 聊天记录类：D71 实测 chat 类 sparse=1.00 而 dense=0.10，
#: 因为聊天是口语 + 大量专有名词，语义嵌入反而不如字面匹配。
_CHAT = r"聊天|对话|我们说|他说|她说|提过|聊过|说过什么|记不记得"
#: 日期类：dense 召不回"哪天/什么时候"（§4 的失败模式之一）。
_DATE = r"哪(天|年|月|个?时候)|什么时候|几月|日期|那天|当天|时间线|哪次|去年|今年|明年|纪念日"

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (INTENT_PHOTO, re.compile(_PHOTO)),
    (INTENT_SCAN, re.compile(_SCAN)),
    (INTENT_TABLE, re.compile(_TABLE)),
    (INTENT_CHAT, re.compile(_CHAT)),
    (INTENT_DATE, re.compile(_DATE)),
)

#: 每个意图 → (权重, 偏好 kind, 偏好 source_kind, 解释模板)
#: 权重方向来自 §4 的分类别实测，见模块 docstring。
_INTENT_SPEC: dict[str, tuple[dict[str, float], frozenset[str] | None,
                              frozenset[str] | None, str]] = {
    INTENT_PHOTO: (
        # 🔑 **3.0 是实测出来的，不是拍的**（D159，2026-10-06）。
        #
        # 这一项原先注释写着"visual 腿在 API 进程里恒 skipped，所以真正起作用的
        # 是 `prefer_source_kinds`；给 visual 高权重是为了将来视觉腿可用时立刻
        # 生效" —— 那是**纸面数字**。D158 接通读侧后实测（60 photo + 60 text
        # 点 · top_k=10 ⇒ 融合池上限 20）：
        #
        # | visual 权重 | 池里 photo 点数 | top10 里 photo |
        # |---|---|---|
        # | 0.0 / 1.0 | 0/20 | 0/10 |
        # | 2.0 | 10/20 | 5/10 |
        # | **3.0** | **20/20** | **10/10** |
        # | 4.0 / 6.0 | 20/20 | 10/10（与 3.0 等价）|
        #
        # ⇒ 3.0 是**能让 photo 点把融合池装满的最小值**，再往上没有收益。
        #
        # 🔑 **关键机制：`prefer_source_kinds` 管不到池外的东西。**
        # `engine.search` 的顺序是
        #     fused = rrf(..., limit=top_k * 2, weights=...)   ← 权重在这
        #     prefer(filtered, rte)                            ← photo 过滤在这
        # 所以权重决定"**哪些结果能进池**"，`prefer` 只能重排**已在池里**的。
        # ⇒ 本项在 photo 库**大于 `top_k * 2`** 时才有意义；
        # 库小的时候（keeper 8 个点）它与任何权重**结果完全一样** ——
        # 那正是前六版实测量不出差异的原因，别再据此下结论。
        {LEG_VISUAL: 3.0},
        # ⚠️ **不按 chunk kind 过滤**：照片库里的 chunk 是 `text` 还是 `image`
        #   取决于 D48 的导入器怎么写，而它还不存在 —— 按一个"还不存在的分类"
        #   过滤，等于把照片库自己的结果也滤掉（实测：3 条里只剩 1 条）。
        #   `source_kind`（库类型）是**已存在的 schema 字段**，过滤它才可靠。
        None,
        frozenset({"photo"}),
        "识别为「找照片」：优先照片库",
    ),
    INTENT_SCAN: (
        {LEG_VISUAL: 2.0, LEG_SPARSE: 1.2},
        frozenset({"page", "table"}),
        None,
        "识别为「找扫描件/表单」：偏向整页与表格类结果",
    ),
    INTENT_TABLE: (
        {LEG_SPARSE: 1.3, LEG_TRGM: 1.2},
        frozenset({"table"}),
        None,
        "识别为「问表格结构」：偏向表格类结果（表格 chunk 带网格坐标，D58）",
    ),
    INTENT_CHAT: (
        {LEG_SPARSE: 1.6},
        frozenset({"chat"}),
        frozenset({"chat"}),
        "识别为「问聊天记录」：偏向聊天库与 chat 类结果"
        "（§4 实测 chat 类 sparse 1.00 / dense 0.10）",
    ),
    INTENT_DATE: (
        # 🔑 **2026-10-15 按真实语料重测后修正**（`eval/retrieval_eval.py` 的 date 类，
        #   8 条真实聊天按天查询）：单腿 Hit@1 —— `sparse` **0.875**、`dense` 0.250、
        #   `trgm` **0.000**。旧值 `{trgm:1.5, sparse:1.3}` 让 dense 的头部命中
        #   压过 sparse（融合后 Hit@1 掉到 0.000）。⇒ 改成 **sparse 主导**、
        #   去掉 trgm 加成（日期字面量不是文档子串，trgm 对这一类没有召回）。
        {LEG_SPARSE: 2.5},
        None,
        None,
        "识别为「按时间问」：偏向字面匹配（BM25 的日期归一化，实测 sparse 0.875）",
    ),
}

#: 中文数字 → 阿拉伯数字，只为抽日期用（"二〇二三年五月"）。
_CN_DIGITS = str.maketrans("〇零一二三四五六七八九十", "001234567890")

#: 日期抽取：`2023-05-01`、`2023年5月`、`2023/5`、`去年5月`…
_DATE_RE = re.compile(
    r"(?P<y>(19|20)\d{2})\s*[-/年]\s*(?P<m>\d{1,2})\s*[-/月]?\s*(?P<d>\d{1,2})?"
)


def _extract_date(query: str) -> str | None:
    """抽出 `YYYY-MM-DD`，抽不出给 `None`。

    ⚠️ 抽不出**不是错误**：`date_hint` 只用于展示与后续过滤，
    路由本身照样生效（权重已按 date 意图调好）。
    只支持绝对日期；"去年五月"这类相对表达留给 D20 的时间线端点去做
    （那里有完整语料可查，相对表达需要语料而不是字符串）。
    """
    m = _DATE_RE.search(query.translate(_CN_DIGITS))
    if not m:
        return None
    y, mo = int(m["y"]), int(m["m"])
    d = int(m["d"] or 1)
    # 边界要挡：2023-13-40 是匹配到了但不是日期（"13 月"不可能存在）
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}"


def classify(query: str) -> Route:
    """判断 query 属于哪一类，给出腿权重与偏好。

    ⚠️ **拿不准就返回 `general`（等权 + 不过滤）**：猜错的代价是把结果过滤空，
    而那对用户来说等于"搜索坏了"（原则 1）。
    """
    q = (query or "").strip()
    if not q:
        return Route(
            intent=INTENT_GENERAL, confidence=0.0, why="空查询：不路由"
        )

    hits: list[str] = [name for name, pat in _PATTERNS if pat.search(q)]
    # 一个**日期字面量本身就是** date 意图（置信度 0.6，低于单词命中）：
    # 用户搜 "2023年5月1日" 就是按时间找，dense 召不回它（§4 的失败模式）。
    # 不加这条的话，纯日期串会落到 general，抬不了 trgm/sparse。
    has_date_literal = _DATE_RE.search(q.translate(_CN_DIGITS)) is not None
    if has_date_literal and INTENT_DATE not in hits:
        hits.append(INTENT_DATE)
    if not hits:
        return Route(
            intent=INTENT_GENERAL,
            confidence=0.0,
            why="没有识别到明确意图：保持等权（§4 实测调权必须按类型来）",
        )

    # 🔑 多个意图同时命中时**取更具体的那一个**，而不是加权求和：
    #   "去年五月去旅游拍的照片" 同时命中 photo 与 date，但用户要的是照片
    #   （D20 的时间线页才是按日期找的场景）。顺序即优先级。
    intent = hits[0]
    weights, kinds, src_kinds, why = _INTENT_SPEC[intent]
    if len(hits) > 1:
        confidence = 0.5
    elif has_date_literal and not _PATTERNS[4][1].search(q):
        confidence = 0.6   # 只有日期字面量，没有"哪天/什么时候"这类词
    else:
        confidence = 0.9
    date_hint = _extract_date(q) if intent == INTENT_DATE else None
    note = "" if len(hits) == 1 else f"（同时命中 {'/'.join(hits)}，取 {intent}）"

    return Route(
        intent=intent,
        weights=dict(weights),
        prefer_kinds=kinds,
        prefer_source_kinds=src_kinds,
        date_hint=date_hint,
        confidence=confidence,
        why=f"{why}{note}",
    )


def route(query: str, *, minimum_confidence: float = MIN_CONFIDENCE) -> Route:
    """对外入口：`classify` + 置信度门限。

    @param minimum_confidence 低于它就退回 `general`。测试用它模拟"门限调到很高"
        —— 生产上不要调高，宁可少优化。
    """
    r = classify(query)
    if r.confidence < minimum_confidence:
        return Route(
            intent=INTENT_GENERAL,
            confidence=r.confidence,
            why=f"置信度 {r.confidence:.2f} 低于门限 {minimum_confidence:.2f}："
                "保持等权（宁可少优化，也不要用猜错的意图过滤结果）",
        )
    return r


def prefer(hits, route_obj: Route, *, fallback_note: str | None = None):
    """按路由的偏好过滤结果；**过滤后为空就退回全部**（原则 2）。

    @param hits 融合后的命中（`Sequence[FusedHit]`）
    @param route_obj `route()` 的结论
    @param fallback_note 过滤失效时追加到 `why` 的说明
    @return `(保留的 hits, 说明文字)`
    """
    kept = list(hits)
    note = route_obj.why

    if route_obj.prefer_kinds:
        sub = [h for h in kept if _hit_kind(h) in route_obj.prefer_kinds]
        if sub:
            kept, note = sub, note + f"；按 kind 过滤成 {len(sub)}/{len(kept)} 条"
        else:
            note += f"；库里没有 {sorted(route_obj.prefer_kinds)} 类结果，不做过滤"

    if route_obj.prefer_source_kinds:
        sub = [h for h in kept if _hit_source_kind(h) in route_obj.prefer_source_kinds]
        if sub:
            kept = sub
            note += f"；按库类型过滤成 {len(sub)} 条（photo/chat 库）"
        else:
            note += (
                f"；库里没有 {sorted(route_obj.prefer_source_kinds)} 类型的库，"
                "不做过滤"
            )

    if fallback_note and len(kept) == len(hits):
        note += "；" + fallback_note
    return kept, note


def _hit_kind(h) -> str | None:
    """chunk 形态。⚠️ `payload` 来自 Qdrant（JSONB），脏数据不许炸（§5.42 同源）。"""
    pl = getattr(h, "payload", None)
    if not isinstance(pl, dict):
        return None
    k = pl.get("kind")
    return k if isinstance(k, str) else None


def _hit_source_kind(h) -> str | None:
    pl = getattr(h, "payload", None)
    if not isinstance(pl, dict):
        return None
    k = pl.get("source_kind")
    return k if isinstance(k, str) else None


__all__ = [
    "MIN_CONFIDENCE",
    "Route",
    "classify",
    "route",
    "prefer",
    "INTENT_GENERAL",
    "INTENT_PHOTO",
    "INTENT_SCAN",
    "INTENT_TABLE",
    "INTENT_CHAT",
    "INTENT_DATE",
]