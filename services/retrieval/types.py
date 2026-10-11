"""检索四条腿的公共数据结构。

单独抽出来是因为**四条腿必须产出同一种形状**，否则 RRF 没法融：
dense / sparse 出自 Qdrant（有点 ID），trgm 出自 Postgres（没点 ID），
visual 出自 multi-vector collection（点 ID 空间与文本腿会撞，PROGRESS §5.6）。

## 关键设计：trgm 腿用**复算**的点 ID 与其它腿对齐

Qdrant 的点 ID 是 `point_id(kb_id, doc_id, chunk_index)`（稳定哈希，D33），
trgm 腿查到的是 `(kb_id, doc_id, chunk_index)` 三元组 —— **可以直接算出同一个 ID**。
所以 trgm 的命中天然能和 dense/sparse 的命中在 RRF 里合并，不需要另开一套对齐逻辑。

这就是稳定哈希 ID 的第二层收益：第一层是"删索引再重建安全"，
这一层是"非向量存储的腿能和向量腿融合"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

#: 四条腿的名字。RRF 的 key 前缀与权重表都用它，不要写字面量。
LEG_DENSE = "dense"
LEG_SPARSE = "sparse"
LEG_TRGM = "trgm"
LEG_VISUAL = "visual"

ALL_LEGS: tuple[str, ...] = (LEG_DENSE, LEG_SPARSE, LEG_TRGM, LEG_VISUAL)

#: RRF 的命名空间前缀。
#:
#: ⚠️ 必须带（PROGRESS §5.10 / `DESIGN-DATA-MODEL.md` §3.1）：
#: `kb_dense` 的 chunk 点与 `kb_visual` 的页级点 ID 空间独立，
#: 直接拿点 ID 做 key 会让"文本 chunk 3"和"扫描页 3"互相抵消。
NS_TEXT = "text"
NS_VISUAL = "visual"


def rrf_key(ns: str, point_id: int) -> str:
    return f"{ns}:{point_id}"


@dataclass
class LegHit:
    """一条召回结果（融合前的原始形态）。

    @param point_id Qdrant 点 ID。trgm 腿没有 Qdrant 点，
        但用 `point_id(kb_id, doc_id, chunk_index)` 复算得到**同一个值**
        （见模块 docstring），所以这个字段对四条腿都非空。
    @param key RRF 融合键，带命名空间
    @param rank 该腿内的排名，**从 1 开始**。RRF 只用排名不用分数（D39）。
    @param score 该腿自己的分数。⚠️ **量纲互不相同**
        （dense 是余弦 0~1、sparse 是 BM25 无界、trgm 是 word_similarity 0~1），
        所以它只用于 `/search/debug` 展示与同一腿内排序，**绝不跨腿相加**。
    @param doc_id / chunk_index / payload 引用溯源与展示用
    """

    key: str
    point_id: int
    doc_id: int
    chunk_index: int
    rank: int
    score: float
    leg: str
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        """引用展示用原文（D35：原文与归一化文本分开存）。"""
        return self.payload.get("text_raw") or self.payload.get("text_norm") or ""


@dataclass
class LegResult:
    """一条腿的完整结果（含它跳过/失败的原因）。"""

    leg: str
    hits: list[LegHit] = field(default_factory=list)
    elapsed_ms: int = 0
    #: 该腿为什么没结果。None = 正常跑了但没命中。
    #: 视觉腿在 API 进程里恒为"skipped"（CONVENTIONS §4.1 禁止加载 colSmol）。
    skipped: str | None = None

    @property
    def ok(self) -> bool:
        return self.skipped is None


@dataclass
class FusedHit:
    """RRF 融合后的一条结果。"""

    key: str
    point_id: int
    doc_id: int
    chunk_index: int
    score: float
    #: 每条腿给它的排名与分数（调试面板用）
    ranks: dict[str, int] = field(default_factory=dict)
    leg_scores: dict[str, float] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    #: 命中它的腿（没被任何腿召回的 chunk 不会进来）
    legs: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return self.payload.get("text_raw") or self.payload.get("text_norm") or ""

    @property
    def locations(self) -> list[dict[str, Any]] | None:
        loc = self.payload.get("locations")
        return loc if isinstance(loc, list) else None

    @property
    def kind(self) -> str:
        """chunk 形态（`chunks.kind`，D58）。

        注意与 API 契约里 `SearchHit.kind`（`text`/`page`/`image`）**不是同一个枚举** ——
        前端画框要看这个（单框 vs 多框网格），而后者是展示类型。
        """
        return self.payload.get("kind") or "text"


#: 契约里 `SearchHit.kind` 的取值。chunk 是 `page`（扫描件）时映射成 `page`，
#: 其余一律 `text`。图片腿映射成 `image`。
CitationKind = Literal["text", "page", "image"]


def citation_kind(chunk_kind: str, *, visual: bool = False) -> CitationKind:
    """chunk 形态 → 契约的引用类型。

    D60：扫描件没有文本层、拿不到坐标，只高亮整页 → `page`。
    """
    if visual:
        return "image"
    return "page" if chunk_kind == "page" else "text"


def thumb_url_for(hit: FusedHit) -> str | None:
    """这条结果的缩略图 URL（D36）。**拿不到页就返回 `None`**。

    缩略图是**按页**存的（`data/blobs/{aa}/{sha}/thumbs/{page}.jpg`），
    所以只有能确定页码时才给 URL。

    ⚠️ 宁可给 `None` 也不要给一个"看起来会 404"的 URL：
    前端会把 `thumb_url` 非空的卡片按图片渲染，取不到就是一块碎图 ——
    比老实说"这张没有缩略图"更糟（D72 同一个道理）。

    | 情况 | 页码来源 |
    |---|---|
    | 正文/表格 chunk（PDF） | `locations[0].page` |
    | 页级向量（扫描页/照片，视觉腿） | `payload["page"]` |
    | 兜底 | `chunk_index < 0` 时按 `-(chunk_index) - 1` 反推（§2.2 的编码约定） |
    """
    for loc in hit.locations or []:
        # ⚠️ `locations` 来自 Qdrant payload（JSONB）—— 契约写的是
        # `list[dict] | None`，但那是**约定**不是保证。脏数据里混进字符串
        # 就该当作"没有页"，而不是让整个检索 500（tests/test_thumb_url.py 钉住）。
        if not isinstance(loc, dict):
            continue
        page = loc.get("page")
        if isinstance(page, int) and page > 0:
            return f"/api/v1/documents/{hit.doc_id}/thumbs/{page}"
    page = hit.payload.get("page")
    if isinstance(page, int) and page > 0:
        return f"/api/v1/documents/{hit.doc_id}/thumbs/{page}"
    if hit.chunk_index < 0:
        return f"/api/v1/documents/{hit.doc_id}/thumbs/{-hit.chunk_index - 1}"
    return None
