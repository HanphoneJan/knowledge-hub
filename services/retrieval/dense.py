"""dense 腿：bge-small-zh-v1.5 向量召回（`kb_dense`）。

实测（`reports/retrieval_eval.json`）：R@10=0.585，56ms。
强项是语义泛化（blog 类 R@10=1.00），弱项是按日期提问（chat 类 0.10）。

## 为什么这一腿单独一个文件而不是塞进 engine

三条腿的"查询准备"完全不同：dense 要跑模型编码、sparse 要查 PG 词表、
trgm 要查 PG 子串。混在一个函数里会变成一坨 if/else，而且
**任一条腿慢就要拖慢全部**（D46 要求检索段 < 500ms）。

## ⚠️ 本模块是同步阻塞的，调用方必须放线程

两处阻塞都不可忽略：
- `pool.encode_query` 是 CPU 推理（实测 88ms/chunk 的单条版本）
- `httpx.Client` 是同步客户端

在 FastAPI 的 event loop 里直接调会**卡住整个 API**（D50 的核心动机就是
colSmol 吃满 12 核把 API 拖死 —— bge-small 轻得多，但 88ms × 并发同样致命）。
所以对外只暴露 `async` 版本，内部用 `anyio.to_thread` 挪走。
"""

from __future__ import annotations

import time
from typing import Any

import anyio.to_thread

from ..common.auth import UserContext, build_qdrant_filter
from ..common.qdrant import COLL_DENSE, get_client
from .types import LEG_DENSE, NS_TEXT, LegHit, LegResult, rrf_key


def _encode_and_query_sync(
    query: str, flt: dict[str, Any], limit: int
) -> list[dict[str, Any]]:
    """跑模型 + 查 Qdrant。**阻塞，只在 `to_thread` 里调。**"""
    from .pool import encode_query

    vec = encode_query(query)
    r = get_client().post(
        f"/collections/{COLL_DENSE}/points/query",
        json={
            "query": vec,
            "limit": limit,
            "with_payload": True,
            "with_vector": False,
            # 租户过滤的唯一入口（CONVENTIONS §0.2）。这里**不允许**自己拼 filter。
            "filter": flt,
        },
    )
    r.raise_for_status()
    return r.json()["result"]["points"]


def _to_hits(points: list[dict[str, Any]]) -> list[LegHit]:
    out: list[LegHit] = []
    for rank, p in enumerate(points, start=1):
        payload = p.get("payload") or {}
        pid = int(p["id"])
        out.append(
            LegHit(
                key=rrf_key(NS_TEXT, pid),
                point_id=pid,
                doc_id=int(payload.get("doc_id") or 0),
                chunk_index=int(payload.get("chunk_index") or 0),
                rank=rank,
                score=float(p.get("score") or 0.0),
                leg=LEG_DENSE,
                payload=payload,
            )
        )
    return out


async def search_dense(
    ctx: UserContext,
    query: str,
    *,
    kb_id: int | None = None,
    kb_ids: list[int] | None = None,
    limit: int = 30,
) -> LegResult:
    """dense 召回。

    @param kb_id 单库检索时指定；None = 跨库
    @param kb_ids 跨库检索时指定子集（**与授权集合求交**，见 `build_qdrant_filter`）
    @param limit 每腿取多少候选。RRF 用 `top_k * 3` 左右更稳
        （取浅了真值可能在第 11 名，取深了白付编码成本 —— 但编码只对 query 做一次，
        所以加深几乎免费，**加宽 limit 是这里最划算的调参**）。
    """
    t0 = time.perf_counter()
    flt = build_qdrant_filter(ctx, kb_id, kb_ids=kb_ids)
    try:
        points = await anyio.to_thread.run_sync(
            _encode_and_query_sync, query, flt, limit
        )
    except Exception as exc:  # noqa: BLE001
        # 单腿失败不能拖垮整体：三路里还有两路（D15 问答/搜索两种入口都要能出结果）
        return LegResult(
            leg=LEG_DENSE,
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
            skipped=f"dense 腿失败: {exc}",
        )
    return LegResult(
        leg=LEG_DENSE,
        hits=_to_hits(points),
        elapsed_ms=int((time.perf_counter() - t0) * 1000),
    )