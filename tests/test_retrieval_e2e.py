"""检索四腿的端到端集成测试（需要 PG + Qdrant 在线）。

与 `eval/api_1d_check.py` 的分工：
- **这里**：腿的正确性与 fail-closed 行为，用**合成数据**做精确断言
- **那里**：真实文档上的召回质量（验收问题"入职日期那栏填什么"）

合成数据是必要的：本机的 `kb_dense` 里堆着历轮测试的 666 个点，
真值不可控，断言只能写成"至少命中一个"那种没有意义的检查。

## 为什么测试是**同步函数** + 自己管 loop

PROGRESS §5.17：`get_async_engine()` 是模块级单例，asyncpg 连接池绑定
**创建它的那个 loop**。anyio 的 pytest 插件**默认每个测试函数一个新 loop** ——
于是第二个测试就报 `got Future ... attached to a different loop`，
而且报错完全指不到真正的原因（看起来像并发问题，实际是 fixture 生命周期）。

所以这里显式开**一个模块级 loop** 跑完全部测试：

```python
_loop = asyncio.new_event_loop()          # 一个，就一个
def run(coro): return _loop.run_until_complete(coro)
```

这与生产环境一致（FastAPI 单 loop），也让测试能跨用例复用连接池。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import uuid

import httpx
import pytest

from services.common.auth import UserContext
from services.common.db import dispose_engines, get_sync_sessionmaker, session_scope
from services.common.models import (
    Blob,
    Chunk,
    Document,
    KnowledgeBase,
    User,
    Workspace,
)
from services.common.qdrant import (
    COLL_DENSE,
    COLL_SPARSE,
    delete_by_filter,
    sparse_vector,
    upsert_points,
)
from services.common.qdrant import point_id as q_point_id
from services.retrieval import bm25
from services.retrieval.engine import search
from services.retrieval.sparse import build_query_vector
from services.retrieval.trgm import search_trgm
from services.retrieval.types import LEG_DENSE, LEG_SPARSE, LEG_TRGM

#: 三份合成文档。内容刻意包含可精确断言的子串（`WIDGET-XYZ-8899`、
#: `入职日期那栏填什么`），这样召回正确性可以精确判定。
DOC_A = "入职日期那栏填什么"
DOC_B = "学号 20230001001 的毕业设计题目是稀疏检索的工程实现"
DOC_C = "WIDGET-XYZ-8899 是这台设备的序列号，出厂时贴在铭牌上"

DOCS = [("alpha.pdf", DOC_A), ("beta.pdf", DOC_B), ("gamma.pdf", DOC_C)]

#: 唯一一个 event loop（§5.17）。模块级 = 全部测试共用 = 与 FastAPI 一致。
_LOOP = asyncio.new_event_loop()


def run(coro):
    return _LOOP.run_until_complete(coro)


def _qdrant_alive() -> bool:
    try:
        r = httpx.get("http://127.0.0.1:6333/collections", timeout=5, trust_env=False)
        return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


# ============================================================
# 语料 fixture
# ============================================================


def _build_corpus():
    """建 1 用户 + 1 workspace + 1 KB + 3 文档 + 三路索引。返回上下文 dict。"""
    tag = uuid.uuid4().hex[:10]

    async def _mk():
        async with session_scope() as s:
            user = User(
                email=f"ret1d-{tag}@t.local", password_hash="x", display_name="检索测试",
            )
            s.add(user)
            await s.flush()
            ws = Workspace(name=f"ws-{tag}", owner_id=user.id)
            s.add(ws)
            await s.flush()
            kb = KnowledgeBase(
                workspace_id=ws.id, name=f"kb-{tag}", visibility="private",
                embed_model="BAAI/bge-small-zh-v1.5", chunk_size=500,
                chunk_overlap=50, created_by=user.id,
            )
            s.add(kb)
            await s.flush()

            docs: list[tuple[int, str]] = []
            dense_pts, sparse_pts = [], []
            for i, (name, text) in enumerate(DOCS):
                # ⚠️ `documents.blob_sha` 有外键指向 `blobs(sha256)`，必须先建 blob 行。
                # 早先直接写了个假 sha，插入被外键拒绝 ——
                # 而报错完全指向不到真正的原因。
                sha = hashlib.sha256(f"{tag}-{name}".encode()).hexdigest()
                s.add(Blob(
                    sha256=sha, size_bytes=len(text.encode()),
                    mime_type="application/pdf", storage_path=f"test://{sha}", ref_count=1,
                ))
                doc = Document(
                    kb_id=kb.id, file_name=name, content_hash=f"{tag}-{i}",
                    blob_sha=sha, mime_type="application/pdf", status="ready",
                    size_bytes=len(text.encode()), uploaded_by=user.id, chunk_count=1,
                )
                s.add(doc)
                await s.flush()
                s.add(Chunk(
                    doc_id=doc.id, chunk_index=0, content_norm=text,
                    token_count=len(text), kind="text",
                    locations=[{"page": 1, "bbox": [0.1, 0.2, 0.9, 0.25]}],
                ))
                await s.flush()
                docs.append((doc.id, text))
                pid = q_point_id(kb.id, doc.id, 0)
                dense_pts.append({
                    "id": pid,
                    "vector": _embed(text),
                    "payload": {
                        "kb_id": kb.id, "doc_id": doc.id, "chunk_index": 0,
                        "workspace_id": ws.id, "visibility": "private",
                        "source_kind": "file", "owner_id": user.id,
                        "file_name": name, "mime": "application/pdf",
                        "kind": "text", "page": 1,
                        "text_norm": text, "text_raw": text,
                        "locations": [{"page": 1, "bbox": [0.1, 0.2, 0.9, 0.25]}],
                    },
                })
            return kb.id, ws.id, user.id, docs, dense_pts, sparse_pts

    kb_id, ws_id, uid, docs, dense_pts, sparse_pts = run(_mk())

    chunk_tokens = [bm25.tokenize(text) for _d, text in docs]
    weights = _register_and_weight(chunk_tokens)
    for (doc_id, _t), weight in zip(docs, weights, strict=True):
        pid = q_point_id(kb_id, doc_id, 0)
        sparse_pts.append({
            "id": pid,
            "vector": sparse_vector(weight),
            "payload": {"kb_id": kb_id, "doc_id": doc_id, "chunk_index": 0},
        })

    upsert_points(COLL_DENSE, dense_pts)
    upsert_points(COLL_SPARSE, sparse_pts)

    return {
        "kb_id": kb_id, "uid": uid, "ws_id": ws_id, "docs": docs,
        "chunk_tokens": chunk_tokens,
        "ctx": UserContext(
            user_id=uid, display_name="t", email="t@t.local", api_key_id=None,
            workspace_ids=frozenset({ws_id}), readable_kb_ids=frozenset({kb_id}),
        ),
    }


def _teardown_corpus(c: dict) -> None:
    # ⚠️ 词表的 df 与 corpus_stats 是**全局**的（三个 KB 共用一份），
    # 不减回去每跑一轮就漂一点，攒够会影响其它测试的召回。
    _unregister(c["chunk_tokens"])
    kb_id = c["kb_id"]
    for coll in (COLL_DENSE, COLL_SPARSE):
        delete_by_filter(coll, {"must": [{"key": "kb_id", "match": {"value": kb_id}}]})

    async def _rm():
        async with session_scope() as s:
            await s.execute(
                User.__table__.delete().where(User.id == c["uid"])
            )   # 级联清 ws/kb/doc/chunk

    run(_rm())


def _embed(text: str) -> list[float]:
    from services.retrieval.pool import encode_query

    return encode_query(text)


def _register_and_weight(chunk_tokens: list[list[str]]) -> list[dict[int, float]]:
    """登记词表 + 算**文档侧** BM25 权重。

    用**同步** Session：`register_chunks` 是 worker 写路径的函数。
    在 AsyncSession 上调它不报错，只是返回未 await 的 coroutine，
    症状是 `AttributeError: 'coroutine' object has no attribute 'all'`
    （§5.28 同一根因）。
    """
    from services.retrieval.terms import get_corpus_stats, register_chunks

    s = get_sync_sessionmaker()()
    try:
        reg = register_chunks(s, chunk_tokens)
        s.commit()
        n_docs, avgdl = get_corpus_stats(s)
        dfs = reg.dfs_by_id({t for tk in chunk_tokens for t in tk})
        return [
            bm25.sparse_to_dict(toks, reg.ids, dfs, n_docs, avgdl) for toks in chunk_tokens
        ]
    finally:
        s.close()


def _unregister(chunk_tokens: list[list[str]]) -> None:
    from services.retrieval.terms import unregister_chunks

    s = get_sync_sessionmaker()()
    try:
        unregister_chunks(s, chunk_tokens)
        s.commit()
    finally:
        s.close()


@pytest.fixture
def corpus():
    if not _qdrant_alive():
        pytest.skip("Qdrant 不在线")
    c = _build_corpus()
    try:
        yield c
    finally:
        _teardown_corpus(c)


def teardown_module(_mod):
    """模块结束：关掉唯一的 loop 与两个引擎。"""
    dispose_engines()
    if not _LOOP.is_closed():
        _LOOP.close()


# ============================================================
# 三路各自
# ============================================================


def test_trgm_finds_exact_substring(corpus):
    """🔑 trgm 这一腿存在的理由之一：BM25 搜不到非词字符串。

    `WIDGET-XYZ-8899` 被 jieba 切成碎片，BM25 的 idf 权重救不回来；
    字符级三 gram 却能精确定位。
    """

    async def _q():
        async with session_scope() as s:
            return await search_trgm(
                s, corpus["ctx"], "WIDGET-XYZ-8899", kb_id=corpus["kb_id"]
            )

    res = run(_q())
    assert res.ok, res.skipped
    assert res.hits, "应命中 gamma.pdf"
    assert "WIDGET-XYZ-8899" in res.hits[0].text
    assert res.hits[0].score >= 0.25, "分数是 word_similarity"


def test_trgm_finds_the_answer_for_a_natural_language_query(corpus):
    """🔑🔑 锁住 1d 最关键的一个修复：召回条件不能用 `ILIKE '%整句%'`。

    第一版这么写，结果整条腿返回 0 条。真实 PDF 上实测（222 chunk）：

    | 召回条件 | 命中 | 含真值？ |
    |---|---|---|
    | `ILIKE '%入职日期那栏填什么%'` | 0 | ❌ |
    | `similarity(chunk,q) >= 0.1` | 0 | ❌ |
    | `word_similarity(q,chunk) >= 0.25` | **3** | ✅ 0.400 |

    根因：前两者都要求 query 与**整段文本**可比（分别是"必须是连续子串"
    与"按整段长度归一化"），而自然语言提问两者都不满足。

    这条测试用合成数据复现同样的结构：真值 chunk 里含"入职日期"但**不含**
    整句 query，所以只有 `word_similarity` 能召回来。
    """

    async def _q():
        async with session_scope() as s:
            return await search_trgm(
                s, corpus["ctx"], "入职日期那栏填什么", kb_id=corpus["kb_id"]
            )

    res = run(_q())
    assert res.ok, res.skipped
    assert res.hits, (
        "自然语言提问必须能被 trgm 腿召回。"
        "若为 0 条，几乎一定是把召回条件写成了 ILIKE 或 similarity()"
    )
    # 真值按 word_similarity 排第一（它包含 query 的关键词 + 答案）
    assert "入职日期那栏填什么" in res.hits[0].text
    assert res.hits[0].score > 0.25


def test_trgm_skips_too_short_query(corpus):
    """3 个字是分界：够跑，但"学号"这种词不一定有高相似度的 chunk。

    断言只锁**长度门槛**这件事，不锁"3 个字一定有命中" ——
    那取决于语料内容，与本测试要验证的东西无关。
    """

    async def _q(q: str):
        async with session_scope() as s:
            return await search_trgm(s, corpus["ctx"], q, kb_id=corpus["kb_id"])

    assert run(_q("稀疏检索的工程")).ok, "长 query 应该正常跑"
    res = run(_q("号"))
    assert not res.ok, "2 个字应被 MIN_QUERY_LEN 拦下"
    assert res.hits == []


def test_trgm_uses_the_same_point_id_as_qdrant(corpus):
    """🔑 复算的点 ID 必须与向量腿一致 —— RRF 合并的前提。

    `point_id(kb_id, doc_id, chunk_index)` 是纯函数，所以 trgm 命中的
    `(kb_id, doc_id, chunk_index)` 能算出**完全相同**的点 ID。
    这是稳定哈希 ID（D33）的第二层收益。
    """

    async def _q():
        async with session_scope() as s:
            return await search_trgm(
                s, corpus["ctx"], "WIDGET-XYZ-8899", kb_id=corpus["kb_id"]
            )

    res = run(_q())
    assert res.hits
    h = res.hits[0]
    assert h.point_id == q_point_id(corpus["kb_id"], h.doc_id, h.chunk_index)
    assert h.key == f"text:{h.point_id}"


def test_sparse_query_vector_is_positive_and_nonempty(corpus):
    """🔑 查询向量必须有非零分量，且全为正。

    全为正来自 `bm25.idf` 里的 `+1`：它保证 idf 恒为正，
    罕见词不会被 BM25 惩罚成负分。
    """

    async def _q():
        async with session_scope() as s:
            return await build_query_vector(s, "入职日期那栏填什么")

    vec, n_tok = run(_q())
    assert n_tok > 0
    assert vec, "查询向量化必须有非零分量"
    assert all(v > 0 for v in vec.values())


def test_sparse_and_dense_hit_overlapping_docs(corpus):
    """两腿应命中共同的文档 —— RRF 融合的前提。"""

    async def _q():
        async with session_scope() as s:
            return await search(
                s, corpus["ctx"], "入职日期", kb_id=corpus["kb_id"], top_k=5
            )

    out = run(_q())
    d = {h.doc_id for h in out.legs[LEG_DENSE].hits}
    sp = {h.doc_id for h in out.legs[LEG_SPARSE].hits}
    assert d, "dense 腿应有命中"
    assert sp, "sparse 腿应有命中"
    assert d & sp, "两腿应有共同命中的文档"


# ============================================================
# 融合
# ============================================================


def test_hybrid_fuses_and_reports_ranks(corpus):
    async def _q():
        async with session_scope() as s:
            return await search(
                s, corpus["ctx"], "入职日期", kb_id=corpus["kb_id"], top_k=5
            )

    out = run(_q())
    assert out.mode == "hybrid"
    assert out.items
    top = out.items[0]
    assert top.ranks, "每个命中都要记录各腿的排名"
    assert set(top.legs) <= {LEG_DENSE, LEG_SPARSE, LEG_TRGM}
    assert top.score > 0


def test_semantic_mode_only_uses_dense(corpus):
    async def _q():
        async with session_scope() as s:
            return await search(
                s, corpus["ctx"], "入职日期", kb_id=corpus["kb_id"],
                mode="semantic", top_k=5,
            )

    out = run(_q())
    assert out.mode == "semantic"
    assert LEG_TRGM not in out.legs, "semantic 模式不该跑 trgm"


def test_exact_mode_only_uses_trgm(corpus):
    async def _q():
        async with session_scope() as s:
            return await search(
                s, corpus["ctx"], "WIDGET-XYZ-8899", kb_id=corpus["kb_id"],
                mode="exact", top_k=5,
            )

    out = run(_q())
    assert out.mode == "exact"
    assert LEG_DENSE not in out.legs, "exact 模式不该跑 dense"
    assert out.items and "WIDGET-XYZ-8899" in out.items[0].text


#: 🔑 D182/R9：小黄金集 = `(在 corpus["docs"] 里的下标, query, 期望出现的唯一子串)`。
#: 语料是**合成且确定**的，所以期望是确定的 —— **不需要人工维护外部数据集**。
#: 三条刻意覆盖三类：自然语言提问 / 学号这类"非词数字串" / 带连字符的设备号。
_GOLDEN: list[tuple[int, str, str]] = [
    (0, "入职日期那栏填什么", "入职日期那栏填什么"),      # alpha.pdf
    (1, "学号 20230001001 的毕业设计", "20230001001"),  # beta.pdf
    (2, "这台设备的序列号 WIDGET-XYZ-8899", "WIDGET-XYZ-8899"),  # gamma.pdf
]


def test_golden_set_r_at_1(corpus):
    """🔑 D182（R9）：小黄金集的 **R@1 回归门**，每次基线都跑。

    盯的是**检索质量整体退化** —— 换 embedding 模型、切分策略、融合权重、
    路由权重的改动，或 df/n_docs 漂移。单腿正确性由本文件其余测试覆盖，
    这一条只管"提问能不能把**对的文档**排第一"（端到端，四条腿一起）。

    ⚠️ 期望来自**合成的确定语料**，不是外部维护的黄金集 ⇒ 没有"期望集过期"
    这类维护负担（对比 `eval/` 里那些跑真实文档的脚本）。
    """
    async def _q(q: str):
        async with session_scope() as s:
            return await search(
                s, corpus["ctx"], q, kb_id=corpus["kb_id"], mode="hybrid", top_k=5
            )

    misses: list[str] = []
    for doc_idx, query, marker in _GOLDEN:
        want_id = corpus["docs"][doc_idx][0]
        out = run(_q(query))
        top = out.items[0] if out.items else None
        ok = top is not None and top.doc_id == want_id and marker in top.text
        if not ok:
            got = (top.doc_id, top.text[:40]) if top is not None else None
            misses.append(f"{query!r} 期望 doc={want_id}，实际 top1={got}")
    assert not misses, "黄金集 R@1 掉了：\n  " + "\n  ".join(misses)


def test_hybrid_runs_all_three_text_legs_on_a_cold_session(corpus):
    """🔑 回归门：三条文本腿在**冷会话**上也要真的跑（不许静默降级成单腿）。

    这是 `eval/retrieval_eval.py` 第一次跑就抓到的 bug 的守卫：三条文本腿
    共享一个 `AsyncSession` 并发 `execute` 会抛
    `This session is provisioning a new connection; concurrent operations
    are not permitted`，于是 sparse / trgm 被记成 `skipped`、
    `hybrid` **静默降级成 dense 单腿** —— 检索照样 200、debug 也不报错。

    ⚠️ **必须用冷会话**（`session_scope()` 新开、不先查任何东西）：API 的单库
    路径因为 `assert_kb_access` 预热过会话而**碰巧**不触发，所以"在 API 上
    手动试一下"发现不了它。守卫要复现的是**最坏**调用点（跨库 / MCP / A2A /
    直接调 `engine.search`）。
    """
    async def _q():
        async with session_scope() as s:
            return await search(
                s, corpus["ctx"], "入职日期那栏填什么",
                kb_id=corpus["kb_id"], mode="hybrid", top_k=5,
            )

    out = run(_q())
    for leg in (LEG_DENSE, LEG_SPARSE, LEG_TRGM):
        r = out.legs[leg]
        assert r.skipped is None, f"{leg} 腿被跳过（hybrid 静默降级）：{r.skipped}"
    assert out.legs[LEG_SPARSE].hits, "sparse 腿在 hybrid 里必须参与召回"
    assert out.legs[LEG_TRGM].hits, "trgm 腿在 hybrid 里必须参与召回"


def test_extra_queries_merge_and_keep_original_first(corpus):
    """🔑 查询改写变体：多路融合，且**原始 query 恒在 `rewrite_queries[0]`**。

    合成语料里 `WIDGET-XYZ-8899`（gamma）与 `入职日期那栏填什么`（alpha）
    是两份不同文档。给"设备序列号"这条 query 配一条"表怎么填"的变体后，
    融合结果应**同时**出现两份文档 —— 证明变体真的参与了召回，而不是被丢弃。
    """
    async def _q(extra):
        async with session_scope() as s:
            return await search(
                s, corpus["ctx"], "WIDGET-XYZ-8899",
                kb_id=corpus["kb_id"], mode="hybrid", top_k=10,
                extra_queries=extra,
            )

    plain = run(_q(None))
    assert plain.rewrite_queries == ["WIDGET-XYZ-8899"], "默认路径不该有改写"

    both = run(_q(["入职日期那栏填什么"]))
    assert both.rewrite_queries[0] == "WIDGET-XYZ-8899", "原始 query 必须在第 0 位"
    docs = {h.doc_id for h in both.items}
    assert corpus["docs"][0][0] in docs, "变体召回的文档（alpha）应进入结果"
    assert corpus["docs"][2][0] in docs, "原始 query 召回的文档（gamma）不应丢"


def test_visual_leg_degrades_without_breaking_the_search(corpus, monkeypatch):
    """🔑 视觉服务不可用时**如实报告跳过 + 可操作的原因**，且检索照常。

    `/search/debug` 显示 `skipped` 的原因，比静默不显示好 ——
    后者会让人以为"这个库没有照片"。

    ## ⚠️ 必须**显式制造**降级，不能靠"服务恰好没起"

    D158 之后视觉服务默认由 `deploy/dev.sh` 起着 ⇒ 这条测试在
    "服务活着"时量到的是 `skipped=None`（正常路径），而断言写的是降级路径
    ⇒ **测试的结果取决于机器上有没有那个进程**。
    症状极其有欺骗性：CI 上绿、本机上红，而红的原因与被测代码无关
    （MEMORY.md §6 第 3 条那一族）。

    ⇒ 这里把 HTTP 层换成"永远连不上"的假客户端，让降级**必然**发生。
    视觉服务真活着时另有一条判据（`tests/test_visual_service.py` 的正向用例）。

    🔑 这里**不钉具体文案**（那是 `tests/test_visual_service.py` 的职责，
    且它可以脱离 PG/Qdrant 跑）：这里只钉**性质** ——
    「有 skipped、有原因、原因里有一个能照着做的动作」。
    """
    from services.retrieval import pool

    class _DeadClient:
        """`get`/`post` 一律抛连接错误（模拟"视觉服务没起"）。

        ⚠️ 必须是**普通类**而不是一个 `async def`：
        `_get_async_client()` 返回的是**客户端对象**，
        把 `async def` 换上去的话，`.get("/health")` 会变成
        `coroutine.get(...)` ⇒ `AttributeError` 而连接错误**根本没发生** ——
        症状是"测试过了但它验的不是那件事"，还带一条
        `coroutine ... was never awaited` 警告（我第一版就这么写的）。
        """

        async def get(self, *a, **k):  # noqa: ANN002, ANN003, ANN202
            raise ConnectionError("visual service is down (test)")

        async def post(self, *a, **k):  # noqa: ANN002, ANN003, ANN202
            raise ConnectionError("visual service is down (test)")

    pool.invalidate_visual_probe()
    monkeypatch.setattr(pool, "_get_async_client", lambda: _DeadClient())
    try:

        async def _q():
            async with session_scope() as s:
                return await search(
                    s, corpus["ctx"], "入职日期", kb_id=corpus["kb_id"], top_k=5
                )

        out = run(_q())
    finally:
        pool.invalidate_visual_probe()

    assert "visual" in out.legs
    vis = out.legs["visual"]
    assert not vis.ok, "视觉腿不可用时不该报 ok"
    assert vis.skipped, "跳过时必须给出原因（否则会被读成'这个库没有照片'）"
    assert "8020" in vis.skipped or "dev.sh start" in vis.skipped, (
        f"跳过原因不可操作（没说是哪个服务、也没说怎么起）：{vis.skipped}"
    )
    assert vis.hits == []
    # 🔑 降级**不能**影响其它三条腿 —— 那才是"视觉腿是增强项"的含义
    assert out.legs["dense"].ok, "视觉腿降级把 dense 腿也带崩了"
    assert out.items, "视觉腿降级导致整次检索没有结果"


# ============================================================
# 🔑 fail-closed
# ============================================================

_STRANGER = UserContext(
    user_id=9999, display_name="x", email="x@t.local", api_key_id=None,
    workspace_ids=frozenset(), readable_kb_ids=frozenset(),
)


def test_unauthorized_kb_returns_empty_not_error(corpus):
    """🔑 无权限的 kb_id → 空集。空集是 Qdrant 侧的 fail-closed。

    路由层才抛 KB_NOT_FOUND；检索内核本身不泄漏存在性。
    """

    async def _q():
        async with session_scope() as s:
            return await search(
                s, _STRANGER, "入职日期", kb_id=corpus["kb_id"], top_k=5
            )

    out = run(_q())
    assert out.items == []
    assert all(not leg.hits for leg in out.legs.values())


def test_every_leg_is_tenant_filtered(corpus):
    """🔑 **每一条腿**都必须返回空，而不是"过滤后为空但有响应"。

    这是 §0.2 的核心保证：漏一处就是一个越权漏洞，
    而这类漏洞在单用户时测不出来（D54）。
    """

    async def _q():
        async with session_scope() as s:
            return await search(
                s, _STRANGER, "入职日期", kb_id=corpus["kb_id"], top_k=5
            )

    out = run(_q())
    for name, leg in out.legs.items():
        assert leg.hits == [], f"{name} 腿泄漏了无权限的结果"


def test_trgm_scope_is_applied(corpus):
    """🔑 trgm 腿的 SQL 侧独立于 Qdrant，也要 fail-closed。"""

    async def _q():
        async with session_scope() as s:
            return await search_trgm(
                s, _STRANGER, "入职日期那栏填什么", kb_id=corpus["kb_id"]
            )

    assert run(_q()).hits == [], "trgm 腿泄漏了无权限的结果"


def test_soft_deleted_document_is_dropped(corpus):
    """🔑 PG 已软删但 Qdrant 点还在 → 检索必须挡掉。

    D7 允许 PG 与 Qdrant 短暂不一致（先改 PG、Qdrant 由 cleanup 异步清），
    所以**必须**在检索侧二次过滤。这条失效就是真的数据泄漏。
    """
    doc_id = corpus["docs"][0][0]

    async def _soft_delete():
        async with session_scope() as s:
            await s.execute(
                Document.__table__.update()
                .where(Document.id == doc_id)
                .values(is_current=False, deleted_at=dt.datetime.now(dt.UTC))
            )

    async def _restore():
        async with session_scope() as s:
            await s.execute(
                Document.__table__.update()
                .where(Document.id == doc_id)
                .values(is_current=True, deleted_at=None)
            )

    async def _q():
        async with session_scope() as s:
            return await search(
                s, corpus["ctx"], "入职日期那栏填什么",
                kb_id=corpus["kb_id"], top_k=5,
            )

    run(_soft_delete())
    try:
        out = run(_q())
        assert doc_id not in [h.doc_id for h in out.items], (
            "已软删的文档还能被检索到 —— 这是真实的数据泄漏"
        )
        assert out.filtered_out >= 1, "应记录被过滤掉的数量"
    finally:
        run(_restore())