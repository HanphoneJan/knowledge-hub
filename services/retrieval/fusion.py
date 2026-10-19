"""RRF 融合（D39：只用排名，不用分数）。

    score(d) = Σ_leg  weight_leg / (k + rank_leg(d))        k=60（论文原始取值）

## 为什么必须用 RRF 而不是加权求和

四条腿的**分数量纲互不相同**，实测：

| 腿 | 分数形态 | 实际观测范围 |
|---|---|---|
| dense | 余弦相似度 | 0.2 ~ 0.9（有界） |
| sparse | BM25 | 0 ~ 40+（**无界**，随语料增长） |
| trgm | `word_similarity` | 0.25 ~ 0.40（窄带） |
| visual | MaxSim | 0 ~ 1 |

要加权就得先归一化，而归一化就得选 min/max 或 z-score，
前者对**单次查询**的候选集敏感（同一段文本在两个 query 下 z-score 不同），
后者会被那条腿的离群值带跑。RRF 只看排名，天然规避（这就是 D39 的理由）。

## k=60 的来历

RRF 原论文（Cormack et al. 2009）的取值。它让 rank 1 与 rank 2 的分差
只有 `1/61` vs `1/62`（约 0.26%）—— 也就是说**头部的微小优势不会压倒第二条腿的共识**。
这正好匹配我们想要的性质：dense 与 sparse 都排第一的那个 chunk 应该赢过
"只有一条腿排第一、另一条腿排第 30"的那个。

## ⚠️ key 必须带命名空间

`kb_dense`（文本 chunk）与 `kb_visual`（扫描页/照片）的点 ID 空间独立，
直接拿点 ID 做 key 会让两者互相抵消（PROGRESS §5.10，真实踩过：
`mem_text` 与 `mem_visual` 的 ID 都从 0 开始，照片曾覆盖扫描页 0~23）。
`types.rrf_key()` 负责加前缀。

## 权重：**默认全 1.0，不要在这里调**

实测（`reports/retrieval_eval.json`）等权 RRF 在**每个类别上都没赢过该类最佳单腿**：
chat 1.00→0.90、photo 1.00→0.60、blog 1.00→0.88。
所以按类别加权/路由是阶段 3 的 `routing.py` 的事，不是"随手把 sparse 调成 1.5"。

⚠️ 调权重前先看那条结论：**继续调 RRF 的 k 或权重解决不了 photo 类 R@5=0.00**，
那是 query 类型路由问题（PROGRESS §4）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from .types import ALL_LEGS, FusedHit, LegHit, LegResult

#: RRF 的 k。论文原始取值，见模块 docstring。
RRF_K = 60

#: 各腿默认权重。**刻意全 1.0**，理由见模块 docstring 末尾。
DEFAULT_WEIGHTS: dict[str, float] = dict.fromkeys(ALL_LEGS, 1.0)


def rrf(
    legs: Iterable[LegResult],
    *,
    k: int = RRF_K,
    weights: Mapping[str, float] | None = None,
    limit: int = 20,
) -> list[FusedHit]:
    """多腿 → 一个排序。

    @param legs 各腿的原始结果。**没命中的腿可以不传**（`LegResult(hits=[])`），
        它对分母的贡献是 0，不会拉低其他腿 —— 这就是 RRF 相对加权求和的好处。
    @param weights 覆盖默认权重。⚠️ 见模块 docstring：调它之前先读那条实测结论。
    @return 按 RRF 分降序，取前 `limit`。`score` 保留 6 位小数
        （`1/60 + 1/61 ≈ 0.0326`，六位足够区分，再多是噪声）。
    """
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    picked: dict[str, FusedHit] = {}

    for leg_result in legs:
        leg = leg_result.leg
        # 跳过的腿不参与：视觉腿在 API 进程里恒为 skipped，
        # 若把它的空结果当成"排了 0 名"塞进分母，会稀释所有结果。
        if not leg_result.ok and not leg_result.hits:
            continue
        weight = w.get(leg, 1.0)

        for h in leg_result.hits:
            slot = picked.get(h.key)
            if slot is None:
                slot = FusedHit(
                    key=h.key,
                    point_id=h.point_id,
                    doc_id=h.doc_id,
                    chunk_index=h.chunk_index,
                    score=0.0,
                    payload=h.payload,
                )
                picked[h.key] = slot
            slot.legs.append(leg)
            slot.ranks[leg] = h.rank
            slot.leg_scores[leg] = round(h.score, 5)
            slot.score += weight / (k + h.rank)
            # payload 取信息量最大的那份。Qdrant 的那份带 mime/kind/table_index，
            # trgm 复算出来的那份是补齐的同构 dict；真有差异时以 Qdrant 为准
            # （它的 `text_raw` 与 `text_norm` 是分开存的，D35）。
            if len(h.payload) > len(slot.payload):
                slot.payload = h.payload

    ranked = sorted(
        picked.values(),
        # 排序键加上 key：RRF 分是浮点，理论上可能出现并列，
        # 不给确定性 tiebreaker 的话同一个 query 两次请求的顺序会抖动，
        # 前端的引用角标 [1][2] 就会在刷新后对调。
        key=lambda x: (-x.score, x.key),
    )
    for h in ranked:
        h.score = round(h.score, 6)
    return ranked[:limit]


def leg_scores(hits: Iterable[LegHit]) -> dict[str, float]:
    """一批命中的分数表（同一腿内可比，跨腿不可比）。调试面板用。"""
    return {h.key: h.score for h in hits}


def merge_fused(
    lists: Iterable[list[FusedHit]], *, k: int = RRF_K, limit: int = 20
) -> list[FusedHit]:
    """把**多次融合结果**再融一次（查询改写 Multi-Query 的用武之地）。

    每个输入列表按它自己的顺序当作一路"排名"，用同一套 RRF 累加。于是
    "被多条改写 query 都排在前面的 chunk"胜出 —— 这正是文章 Multi-Query
    想要的"多路共识"。⚠️ 原始 query 的列表**必须**在里面（调用方保证），
    否则改错时没有兜底。

    与 `rrf` 一样：key 带命名空间、排序有确定性 tiebreaker。
    """
    picked: dict[str, FusedHit] = {}
    for lst in lists:
        for rank, h in enumerate(lst, start=1):
            slot = picked.get(h.key)
            if slot is None:
                slot = FusedHit(
                    key=h.key,
                    point_id=h.point_id,
                    doc_id=h.doc_id,
                    chunk_index=h.chunk_index,
                    score=0.0,
                    payload=h.payload,
                )
                picked[h.key] = slot
            slot.score += 1.0 / (k + rank)
            for leg in h.legs:
                if leg not in slot.legs:
                    slot.legs.append(leg)
            for leg, r in h.ranks.items():
                slot.ranks[leg] = min(slot.ranks.get(leg, r), r)
            if len(h.payload) > len(slot.payload):
                slot.payload = h.payload
    ranked = sorted(picked.values(), key=lambda x: (-x.score, x.key))
    for h in ranked:
        h.score = round(h.score, 6)
    return ranked[:limit]