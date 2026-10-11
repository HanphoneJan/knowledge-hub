"""sparse 腿：BM25（jieba）词元级召回，查 `kb_sparse`。

实测（`reports/retrieval_eval.json`）：R@10=0.756，是四路里**最强的一条**，
MRR@10=0.681 也是最高。关键词/人名/学号基本都靠它。

## 写侧在 1c 就有了，这里只补"读"侧

| | 写侧（1c 已完成） | 读侧（本模块） |
|---|---|---|
| 词表 | `terms.py::register_chunks` 增量维护 | `TermRegistry.ensure_loaded` 只查不改 |
| 权重 | `bm25.doc_weight(...)` 文档侧公式 | `bm25.query_weight(...)` 查询侧公式 |

⚠️ **两侧公式不一样**（分母是 `tf + k1` 而不是 `tf + norm`），
用错一侧检索质量明显下降。见 `bm25.py::sparse_to_dict(is_query=True)`。

## ⚠️ 日期归一化 token 是这一腿的关键补丁

`2025-02-14` 会被 ASCII 正则切成 2025/02/14，而查询里的「2025年2月14日」
切成 2025/年/2/月/14/日 —— 两边**没有任何共同 token** 表达"这一天"，
而 BM25 无法表达多词共现。加上 `d20250214` 后该类 query 的 R@10
从 **0.20 → 1.00**（实测，`bm25.py::date_tokens`）。

所以这里**必须**走 `bm25.tokenize()`，不能自己写一套分词。

## idf 用的是全局统计量 —— 已知折中

`corpus_stats` 的 `n_docs` 是**全库**的 chunk 数，不是本知识库的
（三个 KB 共用一个 `kb_sparse`，稀疏向量下标空间必须全局一致）。
副作用：一个罕见词在别的 KB 出现过，这边 idf 会被拉低 ——
只泄漏统计量，不泄漏内容。修法是定期跑 `KIND_REINDEX` 重算。
"""

from __future__ import annotations

import time
from typing import Any

import anyio.to_thread
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..common.auth import UserContext, build_qdrant_filter
from ..common.qdrant import COLL_SPARSE, get_client, sparse_query
from .bm25 import sparse_to_dict, tokenize
from .terms import TermRegistryAsync, get_corpus_stats_async
from .types import LEG_SPARSE, NS_TEXT, LegHit, LegResult, rrf_key


async def build_query_vector(
    session: AsyncSession, query: str
) -> tuple[dict[int, float], int]:
    """`query` → `{term_id: weight}`。返回 `(向量, token 数)`。

    单独抽出来是因为 eval 脚本要单独量它的耗时与分量数 ——
    这一步是"PG 查询"（网络往返），和后面的 Qdrant 查询是两段，
    混在一起就看不出 500ms 花在哪了。

    ⚠️ 用 `TermRegistryAsync` 而不是同步的 `TermRegistry`：
    在 AsyncSession 上调同步版的 `session.execute()` **不报错**，
    只是返回未 await 的 coroutine，症状是
    `AttributeError: 'coroutine' object has no attribute 'all'`（§0.9 / §5.28）。

    ⚠️ 这里**不** flush/不插新词：查询时查不到就是 OOV，让它在这个 query 上
    不贡献分数即可。把用户的一个随机查询写进全局词表会污染 idf。
    """
    tokens = tokenize(query)
    reg = TermRegistryAsync(session)
    await reg.ensure_loaded(set(tokens))
    n_docs, avgdl = await get_corpus_stats_async(session)
    dfs = reg.dfs_by_id(set(tokens))
    vec = sparse_to_dict(tokens, reg.ids, dfs, n_docs, avgdl, is_query=True)
    return vec, len(tokens)


def _query_sync(vec: dict[int, float], flt: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """⚠️ 稀疏向量的**查询**格式与写入不同，见 `qdrant.sparse_query` 的坑表。

    写入  `{"bm25": {"indices": [...], "values": [...]}}`
    查询  `{"name": "bm25", "indices": [...], "values": [...]}`   ← 没有外层向量名
    """
    r = get_client().post(
        f"/collections/{COLL_SPARSE}/points/query",
        json={
            "query": sparse_query(vec),
            "using": "bm25",
            "limit": limit,
            "with_payload": True,
            "with_vector": False,
            "filter": flt,   # 租户过滤唯一入口（CONVENTIONS §0.2）
        },
    )
    r.raise_for_status()
    return r.json()["result"]["points"]


async def search_sparse(
    session: AsyncSession,
    ctx: UserContext,
    query: str,
    *,
    kb_id: int | None = None,
    kb_ids: list[int] | None = None,
    limit: int = 30,
) -> LegResult:
    t0 = time.perf_counter()
    try:
        vec, n_tok = await build_query_vector(session, query)
        if not vec:
            # OOV：查询词全不在词表。全是停用词或纯符号时会走到这里。
            # 这**不是错误**，是一条腿 legitimately 没有候选 —— RRF 里少一路参与。
            return LegResult(
                leg=LEG_SPARSE,
                elapsed_ms=int((time.perf_counter() - t0) * 1000),
                skipped=f"查询词全不在词表（分词后 {n_tok} 个 token）",
            )
        points = await anyio.to_thread.run_sync(
            _query_sync, vec, build_qdrant_filter(ctx, kb_id, kb_ids=kb_ids), limit
        )
    except Exception as exc:  # noqa: BLE001
        return LegResult(
            leg=LEG_SPARSE,
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
            skipped=f"sparse 腿失败: {exc}",
        )

    hits: list[LegHit] = []
    for rank, p in enumerate(points, start=1):
        payload = p.get("payload") or {}
        pid = int(p["id"])
        hits.append(
            LegHit(
                key=rrf_key(NS_TEXT, pid),
                point_id=pid,
                doc_id=int(payload.get("doc_id") or 0),
                chunk_index=int(payload.get("chunk_index") or 0),
                rank=rank,
                score=float(p.get("score") or 0.0),
                leg=LEG_SPARSE,
                payload=payload,
            )
        )
    return LegResult(
        leg=LEG_SPARSE, hits=hits, elapsed_ms=int((time.perf_counter() - t0) * 1000)
    )


async def terms_table_size(session: AsyncSession) -> int:
    """词表规模。`/health` 与 eval 脚本用来确认词表不是空的。"""
    from sqlalchemy import func

    from ..common.models import Term

    return int(
        (await session.execute(select(func.count()).select_from(Term))).scalar_one()
    )