"""🔑 缩略图的页码推导（`retrieval/types.py::thumb_url_for`）。

D36 的价值全在"点开前先看到那页长什么样"，而缩略图**按页存**。
所以这个函数只有一件事要做对：**别给出错的页，也别给出错的 URL**。

三条判据：
1. 有 `locations[].page` → 用它（表格 40 个框都在同一页，取第一个）
2. 页级向量 → `payload["page"]`
3. 兜底 `chunk_index < 0` → 按 `-(chunk_index) - 1` 反推（§2.2 的编码约定）
4. **都没有 → `None`**：宁可没有，不要一个会 404 的 URL（前端会渲染成碎图）
"""

from __future__ import annotations

from services.retrieval.types import FusedHit, thumb_url_for


def hit(**kw) -> FusedHit:
    payload = kw.pop("payload", {})
    return FusedHit(
        key="text:1:0",
        point_id=1,
        doc_id=kw.pop("doc_id", 345),
        chunk_index=kw.pop("chunk_index", 3),
        score=0.03,
        payload=payload,
        **kw,
    )


def test_text_chunk_uses_first_location_page():
    h = hit(payload={"locations": [{"page": 2, "bbox": [0.1, 0.2, 0.3, 0.4]}]})
    assert thumb_url_for(h) == "/api/v1/documents/345/thumbs/2"


def test_table_chunk_takes_the_first_of_many_boxes():
    """表格 chunk 有 40 个框，但它们**都在同一页** —— 取第一个就够。"""
    locs = [{"page": 7, "bbox": [0.1, 0.1, 0.2, 0.2], "row": r, "col": c}
            for r in range(9) for c in range(7)]
    assert thumb_url_for(hit(payload={"locations": locs})) == (
        "/api/v1/documents/345/thumbs/7"
    )


def test_visual_page_hit_uses_payload_page():
    h = hit(chunk_index=-3, payload={"page": 3, "source_kind": "photo"})
    assert thumb_url_for(h) == "/api/v1/documents/345/thumbs/3"


def test_negative_chunk_index_is_the_fallback():
    """页级向量的 chunk_index 是 `-(page+1)`（§2.2）→ page = -idx - 1。"""
    h = hit(chunk_index=-1)  # → 第 0 页？不对：-(1)-1 = 0，页码从 1 起
    assert thumb_url_for(h) == "/api/v1/documents/345/thumbs/0"
    h2 = hit(chunk_index=-6)
    assert thumb_url_for(h2) == "/api/v1/documents/345/thumbs/5"


def test_no_page_means_no_url():
    """🔑 聊天 chunk / docx / 图片文档：给 `None`，**不要**编一个 URL。

    给错的 URL 的后果：前端按图片渲染 → 一块碎图，
    比老实说"这张没有缩略图"更糟（D72 同一个道理）。
    """
    assert thumb_url_for(hit(payload={"locations": None})) is None
    assert thumb_url_for(hit(payload={})) is None
    assert thumb_url_for(hit(payload={"locations": []})) is None


def test_page_zero_or_negative_is_not_a_page():
    """`page=0` 不是页码（页码从 1 起）—— 这种情况要落到兜底或 None。"""
    assert thumb_url_for(hit(payload={"locations": [{"page": 0}]})) is None
    assert thumb_url_for(hit(payload={"page": -2})) is None


def test_malformed_locations_do_not_crash():
    """坐标是 `list[dict] | None`，脏数据不该让检索整体 500。"""
    for bad in ([{"bbox": [0.1, 0.2, 0.3, 0.4]}], ["x"], [None], [{}]):
        assert thumb_url_for(hit(payload={"locations": bad})) is None