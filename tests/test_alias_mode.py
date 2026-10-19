"""🔑 D193：蓝绿重建的**整合**验证（需要在线 Qdrant；离线时整组 skip）。

纯判据在 `tests/test_alias_pure.py`（进 CI 门禁）。
这里跑真正需要 Qdrant 的东西：建物理集合 / 复制点 / 原子切别名 / 解析别名，
以及"影子稀疏集合也能 upsert"的回归。

⚠️ 清理**只删自己命名空间里的东西**（`kb_dense__khtest*` / `kb_sparse__khtest*`），
   **绝不遍历所有 collection** —— 那样会连生产集合一起删掉。
"""

from __future__ import annotations

import pytest


def _qdrant_up() -> bool:
    """同步探活（不走 async，避免在收集期开 event loop）。"""
    try:
        from services.common.qdrant import get_client

        return get_client().get("/collections").status_code == 200
    except Exception:  # noqa: BLE001
        return False


_PREFIXES = ("kb_dense__khtest", "kb_sparse__khtest")


def _cleanup() -> None:
    """只清测试命名空间：别名 + 以 `_PREFIXES` 开头的物理集合。"""
    from services.common.qdrant import delete_alias, delete_collection, list_aliases, _existing_collections

    for a in list(list_aliases()):
        if a.startswith(_PREFIXES):
            delete_alias(a)
    for c in list(_existing_collections()):
        if c.startswith(_PREFIXES):
            delete_collection(c)


@pytest.mark.skipif(not _qdrant_up(), reason="Qdrant 不在线")
def test_alias_switch_and_copy_roundtrip_on_real_qdrant():
    """建物理集合 → 复制点 → 切别名 → 解析别名，全链在真 Qdrant 上跑通。"""
    from services.common.config import get_settings
    from services.common.qdrant import (
        create_collection,
        copy_points,
        resolve_target,
        switch_alias,
        upsert_points,
    )

    a, b, alias = "kb_dense__khtest_a", "kb_dense__khtest_b", "kb_dense__khtest_live"
    _cleanup()
    try:
        create_collection(a)
        create_collection(b)
        dim = get_settings().text_embed_dim
        upsert_points(a, [{"id": 1, "vector": [1.0] + [0.0] * (dim - 1),
                           "payload": {"kb_id": 1}}])
        assert copy_points(a, b) == 1
        switch_alias(alias, a)
        assert resolve_target(alias) == a
        switch_alias(alias, b)
        assert resolve_target(alias) == b
    finally:
        _cleanup()


@pytest.mark.skipif(not _qdrant_up(), reason="Qdrant 不在线")
def test_upsert_sparse_into_shadow_collection_succeeds():
    """🔴 回归：影子稀疏集合（`kb_sparse__khtest_upsert`）也能 upsert。

    `upsert_points` 若按精确名 `== "kb_sparse"` 判稀疏，影子集合不会被包向量名 · Qdrant 400。
    端点：传**未包名**的稀疏 dict（`_index_text_legs` 就是这么传的）。
    """
    from services.common.qdrant import create_collection, delete_collection, upsert_points

    coll = "kb_sparse__khtest_upsert"
    _cleanup()
    try:
        create_collection(coll)
        upsert_points(coll, [{
            "id": 1,
            "vector": {"indices": [1, 5], "values": [0.5, 1.2]},
            "payload": {"kb_id": 1},
        }])
    finally:
        delete_collection(coll)


@pytest.mark.skipif(not _qdrant_up(), reason="Qdrant 不在线")
def test_resolve_target_none_for_unknown():
    from services.common.qdrant import resolve_target

    assert resolve_target("kb_dense__definitely_not_here") is None


@pytest.mark.skipif(not _qdrant_up(), reason="Qdrant 不在线")
def test_swap_builds_shadow_and_switches_alias_then_retains_old():
    """🔴 **整条蓝绿路径**：`_swap_rebuild` 建影子 → 灌数据 → 验证 → 切别名 → 留旧。

    用隔离逻辑名（`kb_dense__khtest`）走 `_swap_rebuild` 的参数化入口，
    **不 monkeypatch 模块常量**（那会把"按种类识别稀疏"改坏）。
    生产集合不被触碰（清理只针对测试命名空间）。
    """
    import uuid

    from services.common import qdrant as Q
    from services.common.consistency import pg_point_ids, qdrant_point_ids
    from services.common.db import sync_session_scope
    from services.common.models import (
        Blob, Chunk, Document, KnowledgeBase, User, Workspace,
    )
    from services.worker.handlers import _swap_rebuild

    LD, LS = "kb_dense__khtest", "kb_sparse__khtest"
    _cleanup()
    Q.create_collection(LD + "__old")
    Q.switch_alias(LD, LD + "__old")
    Q.create_collection(LS + "__old")
    Q.switch_alias(LS, LS + "__old")

    tag = uuid.uuid4().hex[:8]
    try:
        with sync_session_scope() as s:
            u = User(email=f"bg-{tag}@t.local", password_hash="x", display_name="bg")
            s.add(u); s.flush()
            ws = Workspace(name=f"bg-{tag}", owner_id=u.id); s.add(ws); s.flush()
            kb = KnowledgeBase(
                workspace_id=ws.id, name=f"bg-{tag}", visibility="private",
                embed_model="BAAI/bge-small-zh-v1.5", chunk_size=500,
                chunk_overlap=50, created_by=u.id,
            )
            s.add(kb); s.flush()
            sha = f"bgsha{tag}"
            s.add(Blob(sha256=sha, size_bytes=10, storage_path=f"{sha}/original"))
            s.flush()
            d = Document(kb_id=kb.id, file_name="bg.txt", content_hash=sha,
                         blob_sha=sha, doc_source="upload", status="ready")
            s.add(d); s.flush()
            s.add(Chunk(doc_id=d.id, chunk_index=0,
                        content_norm="蓝绿重建验证内容", kind="text"))
            s.flush()
            kb_id, doc_id, ws_id, uid = kb.id, d.id, ws.id, u.id

            # 模拟 handle_reindex 的**全局登记**：真实路径先 register_chunks
            # （increment_df=True）再建影子（index_terms=False）。照做，df 才平衡。
            from services.retrieval import bm25
            from services.retrieval.terms import register_chunks, unregister_chunks
            reg_tokens = [bm25.tokenize("蓝绿重建验证内容")]
            register_chunks(s, reg_tokens)

            # ⚠️ `chunks_of` 传 **ORM `Chunk` 行**（与 `handle_reindex` 的调用点一致）——
            #   `_swap_rebuild` 必须自己过 `chunker.from_rows`。传 `chunker.Chunk`
            #   会**掩盖**那个转换缺失（实跑 swap 就是因此 AttributeError）。
            orm_chunks = s.query(Chunk).filter(Chunk.doc_id == doc_id).all()
            res = _swap_rebuild(
                s, [(doc_id, kb_id, "bg.txt", "text/plain", None)],
                {doc_id: orm_chunks},
                lambda _p: None, dense_logical=LD, sparse_logical=LS,
            )
            s.commit()
            assert Q.resolve_target(LD) == res["new"][LD]
            assert Q.resolve_target(LS) == res["new"][LS]
            assert res["old"][LD] in Q._existing_collections(), "旧集合应保留（回滚用）"

            exp = pg_point_ids(s, [kb_id])
            got = {leg: qdrant_point_ids([res["new"][leg]], [kb_id]) for leg in (LD, LS)}
            assert exp == got[LD] == got[LS] and len(exp) == 1

            # 清 PG（按外键顺序）：df 也要**对称**地减回去
            unregister_chunks(s, reg_tokens)
            s.query(Chunk).filter(Chunk.doc_id == doc_id).delete()
            s.query(Document).filter(Document.id == doc_id).delete()
            s.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).delete()
            s.query(Workspace).filter(Workspace.id == ws_id).delete()
            s.query(User).filter(User.id == uid).delete()
            s.query(Blob).filter(Blob.sha256 == sha).delete()
            s.commit()
    finally:
        _cleanup()
