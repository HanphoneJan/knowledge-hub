"""D161：检索结果内联调试块（搜索页与调试页合并的地基）。

要守的三件事：

1. `include_debug` 默认 **关** —— 普通检索不该背上四条腿的原始命中。
2. `SearchResp.debug` 的形状 = `/search/debug` 去掉 `query`/`mode`。
3. 🔑 **过程数据只有一个构造处**：`/search/debug`、单库检索、跨库检索
   三处都调 `_debug_block`。若哪天有人在某个端点上又手写一份 `rrf`/`legs`，
   就会开始漂移——而"调试端点说 A、内联说 B"是最难查的一类
   （与 §0.6 可见性、D140 派发表同一条教训）。所以这里用
   **源码里这些字面量只出现一次**来钉住它。

不需要 PG / Qdrant / 起服务：全部是纯数据与源码判据。
"""

from __future__ import annotations

import inspect

from services.api.routers import search as search_router
from services.common.schemas import (
    CrossKbSearchReq,
    SearchReq,
    SearchResp,
)
from services.retrieval.engine import SearchOutcome
from services.retrieval.rerank import RerankOutcome
from services.retrieval.routing import Route
from services.retrieval.types import FusedHit, LegHit, LegResult


def _outcome() -> SearchOutcome:
    leg = LegResult(
        leg="dense",
        elapsed_ms=12,
        hits=[
            LegHit(
                key="text:1",
                point_id=1,
                doc_id=1,
                chunk_index=0,
                rank=1,
                score=0.9,
                leg="dense",
                payload={"file_name": "a.pdf", "text_raw": "hello 入团"},
            )
        ],
    )
    fused = FusedHit(
        key="text:1",
        point_id=1,
        doc_id=1,
        chunk_index=0,
        score=0.0325,
        ranks={"dense": 1},
        leg_scores={"dense": 0.9},
        payload={"file_name": "a.pdf", "text_raw": "hello 入团"},
        legs=["dense"],
    )
    return SearchOutcome(
        query="入团",
        mode="hybrid",
        items=[fused],
        legs={"dense": leg},
        elapsed_ms={"dense": 12},
        total_ms=20,
        filtered_out=0,
        route=Route(intent="general", why="等权"),
        rerank=RerankOutcome(items=[fused], model="", skipped="该库没配 rerank_model"),
    )


def test_include_debug_defaults_off():
    assert SearchReq(query="x").include_debug is False
    assert CrossKbSearchReq(query="x").include_debug is False


def test_cross_kb_supports_page():
    """D161：跨库端点补了 `page`，与单库对齐。"""
    assert CrossKbSearchReq(query="x").page == 1
    assert CrossKbSearchReq(query="x", page=3).page == 3


def test_debug_block_shape_matches_legacy_endpoint():
    block = search_router._debug_block(_outcome(), 5)
    assert set(block) == {
        "route",
        "rerank",
        "rewrite",
        "quality",
        "legs",
        "rrf",
        "elapsed",
        "filtered_out_deleted",
    }
    assert block["legs"]["dense"]["n"] == 1
    assert block["legs"]["dense"]["skipped"] is None
    assert block["legs"]["dense"]["items"][0]["text"] == "hello 入团"
    assert block["rrf"][0]["ranks"] == {"dense": 1}
    assert block["rrf"][0]["legs"] == ["dense"]
    # `elapsed` 的键是腿名 + total_ms（契约 §5.4 第 2 条）
    assert block["elapsed"] == {"dense": 12, "total_ms": 20}
    assert block["filtered_out_deleted"] == 0


def test_debug_block_is_serializable_in_search_resp():
    resp = SearchResp(
        items=[],
        total=1,
        mode="hybrid",
        page=1,
        debug=search_router._debug_block(_outcome(), 5),
    )
    dumped = resp.model_dump()
    assert dumped["debug"]["rrf"][0]["key"] == "text:1"
    # 默认（不传 debug）序列化成 null，而不是缺字段 —— 前端按 `debug == null` 判断
    assert SearchResp(items=[], total=0, mode="hybrid").model_dump()["debug"] is None


def test_debug_data_has_a_single_construction_site():
    """🔑 过程数据只许有 `_debug_block` 一份实现（见模块 docstring）。"""
    src = inspect.getsource(search_router)
    # rrf / leg_scores / filtered_out_deleted 这三处都是"过程数据形状"的指纹：
    # 只应出现在 `_debug_block` 里。散落第二份 = 开始漂移。
    assert src.count('"rrf"') == 1, "rrf 有第二份构造处（D161 要求唯一）"
    assert src.count('"leg_scores"') == 1, "leg_scores 有第二份构造处"
    assert src.count('"filtered_out_deleted"') == 1
    # 三处调用方：单库、跨库、/search/debug
    assert src.count("_debug_block(") >= 4  # 1 个定义 + 3 个调用
