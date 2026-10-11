"""🔑 D177：清理「非当前文档」的向量点（顶替 / 软删之后旧点不该留 30 天）。

## 这一轮修的真 bug（R7，实测）

同名顶替（`supersede`）后旧点不会被删，要等 30 天 `purge` 才清。实测顶替一次
之后 `check_drift` 就报 `kb_dense 多 1 / kb_sparse 多 1`（旧点成了"孤儿"）⇒
巡检的「索引对账」会在**整整 30 天里**一直黄，且旧点占着召回名额。

修法：worker 空闲时周期性 `remove_noncurrent_points` —— 只删 Qdrant 点，
**不碰** df / blob / PG 行（那三样归 30 天 `purge` 的六步链，D79/D115）。
恢复一律重摄取（两个恢复端点都 `enqueue(KIND_INGEST)`），所以旧点**没有消费者**。

## 这一组钉

1. **判据**：只选 `deleted_at < cutoff` 的**非当前**文档（当前文档不许被碰）。
2. **动作**：对 dense / sparse / visual **三个** collection 都发删除，
   且过滤条件是 `doc_id any [...]`（不是别的字段 —— 见对齐报告 §4）。
3. **接线**：worker 真的调了它（纯函数对但没人调用 = 白写，D36 的教训）。
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _pg_alive() -> bool:
    from sqlalchemy import text

    from services.common.db import sync_session_scope

    try:
        with sync_session_scope() as s:
            s.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


pg = pytest.mark.skipif(not _pg_alive(), reason="Postgres 不在线")


def _mk(session):  # noqa: ANN001, ANN202
    """建 user + workspace + kb + blob + 两份文档（一当前、一已删）。返回各 id。"""
    import uuid

    from services.common.models import Blob, Document, KnowledgeBase, User, Workspace

    tag = uuid.uuid4().hex[:8]
    user = User(email=f"stale-{tag}@t.local", password_hash="x", display_name="清点测试")
    session.add(user)
    session.flush()
    ws = Workspace(name=f"stale-ws-{tag}", owner_id=user.id)
    session.add(ws)
    session.flush()
    kb = KnowledgeBase(
        workspace_id=ws.id, name=f"stale-{tag}", visibility="private",
        embed_model="BAAI/bge-small-zh-v1.5", chunk_size=500, chunk_overlap=50,
        created_by=user.id,
    )
    session.add(kb)
    session.flush()
    sha = f"deadbeef{tag}"
    session.add(Blob(sha256=sha, size_bytes=1, storage_path=f"{sha}/original"))
    session.flush()

    now = dt.datetime.now(dt.UTC)
    cur = Document(kb_id=kb.id, file_name="cur.txt", content_hash=sha, blob_sha=sha,
                   doc_source="upload", status="ready", is_current=True)
    old = Document(kb_id=kb.id, file_name="old.txt", content_hash=sha, blob_sha=sha,
                   doc_source="upload", status="ready", is_current=False,
                   deleted_at=now - dt.timedelta(hours=1))
    session.add_all([cur, old])
    session.flush()
    return {
        "kb_id": kb.id, "ws_id": ws.id, "user_id": user.id,
        "blob": sha, "cur_id": cur.id, "old_id": old.id,
    }


def _cleanup(session, ids) -> None:  # noqa: ANN001
    from services.common.models import Blob, Document, KnowledgeBase, User, Workspace

    session.query(Document).filter(Document.kb_id == ids["kb_id"]).delete(
        synchronize_session=False)
    session.query(KnowledgeBase).filter(KnowledgeBase.id == ids["kb_id"]).delete(
        synchronize_session=False)
    session.query(Workspace).filter(Workspace.id == ids["ws_id"]).delete(
        synchronize_session=False)
    session.query(User).filter(User.id == ids["user_id"]).delete(
        synchronize_session=False)
    session.query(Blob).filter(Blob.sha256 == ids["blob"]).delete(
        synchronize_session=False)
    session.flush()


# ============================================================
# 1. 判据：只选非当前（源码层，离线）
# ============================================================


def test_stmt_only_selects_deleted_docs():
    """🔑 判据含 `deleted_at IS NOT NULL` 且 `deleted_at < older_than`。"""
    from services.ingest.purge import noncurrent_doc_ids_stmt

    sql = str(noncurrent_doc_ids_stmt(
        older_than=dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
    ))
    assert "deleted_at IS NOT NULL" in sql
    assert "deleted_at <" in sql


# ============================================================
# 2. 动作：真 DB 上只删非当前（stub 掉 Qdrant）
# ============================================================


@pg
def test_remove_noncurrent_points_targets_only_old_docs(monkeypatch):
    from services.common.db import sync_session_scope
    from services.ingest.purge import remove_noncurrent_points

    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "services.common.qdrant.delete_by_filter",
        lambda coll, flt: calls.append((coll, flt)),
    )

    with sync_session_scope() as s:
        ids = _mk(s)
        try:
            n = remove_noncurrent_points(s, grace=dt.timedelta(0))
            assert n >= 1, "一份非当前文档都没命中"
            colls = {c for c, _ in calls}
            assert colls == {"kb_dense", "kb_sparse", "kb_visual"}, (
                f"三条腿都要清，实际清了 {colls}"
            )
            targets = [flt["must"][0]["match"]["any"] for _c, flt in calls]
            for _c, flt in calls:
                assert flt["must"][0]["key"] == "doc_id", (
                    f"过滤字段不是 doc_id：{flt['must'][0]['key']}（对齐报告 §4 的教训）"
                )
            # ⚠️ 开发库里已有别的非当前文档，所以按 **id 是否出现** 断言，不数份数
            assert any(ids["old_id"] in t for t in targets), "非当前文档没被清"
            assert all(ids["cur_id"] not in t for t in targets), (
                "当前文档的点被误清 —— 这是删活数据"
            )
        finally:
            _cleanup(s, ids)
            s.commit()


@pg
def test_grace_excludes_just_superseded_docs(monkeypatch):
    """🔑 收敛窗口：刚被顶替（`deleted_at` 很新）的不清，避免与在跑的摄取抢跑。"""
    from services.common.db import sync_session_scope
    from services.ingest.purge import remove_noncurrent_points

    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "services.common.qdrant.delete_by_filter",
        lambda coll, flt: calls.append((coll, flt)),
    )
    with sync_session_scope() as s:
        ids = _mk(s)          # old.deleted_at = now - 1h
        try:
            # grace=2h ⇒ cutoff=now-2h，旧文档的 deleted_at（now-1h）> cutoff → 不选
            remove_noncurrent_points(s, grace=dt.timedelta(hours=2))
            targets = [flt["must"][0]["match"]["any"] for _c, flt in calls]
            # ⚠️ 开发库里已有别的非当前文档，只断言"我这份"没被选
            assert all(ids["old_id"] not in t for t in targets), (
                "收敛窗口内的文档不该被清"
            )
            assert all(ids["cur_id"] not in t for t in targets)
        finally:
            _cleanup(s, ids)
            s.commit()


# ============================================================
# 3. 接线：worker 真的调了它
# ============================================================


def test_worker_sweeps_noncurrent_points():
    """🔑 worker 空闲循环里必须调用 `remove_noncurrent_points`（否则判据白写）。"""
    src = (ROOT / "services" / "worker" / "main.py").read_text(encoding="utf-8")
    assert "remove_noncurrent_points(" in src, (
        "worker 没有周期性清非当前文档的向量点 —— 顶替/软删后的旧点会留 30 天"
    )
