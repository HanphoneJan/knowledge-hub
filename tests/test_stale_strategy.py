"""🔑 D181：KB 详情里的「重摄取欠账」= 还停在旧切分策略的当前文档数。

对应文章「坑三：Chunk 策略变了，但历史数据不重建 —— 一个库里混着多套切分逻辑」。
本项目改 `chunk_strategy` **只对之后重新摄取的文档生效**（PATCH 不自动重跑），
所以 `documents.quality.strategy`（D166 记下的）≠ 库当前策略 的文档数，
就是需要重摄取的那批。

## 这一组钉

1. 判据**性质**：只有"当前、未删、策略与库不符"的文档被数到。
2. 端点真的把它放进 `KbDetail`（不是只在 schema 里躺着一个没人填的字段）。
3. `quality` 为 NULL（D166 之前）按默认 `recursive` 计 —— 默认库不虚报。
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


def test_kb_detail_has_the_field():
    from services.common.schemas import KbDetail

    assert "stale_strategy_docs" in KbDetail.model_fields


def test_endpoint_computes_and_returns_the_field():
    """🔑 字段必须被**算出来并塞进响应**，否则只是一个恒 0 的摆设。"""
    src = (ROOT / "services" / "api" / "routers" / "kb.py").read_text(encoding="utf-8")
    assert "stale_strategy_condition(" in src, "端点没有算重摄取欠账"
    assert "stale_strategy_docs=" in src, "算出来了却没塞进 KbDetail"


def _mk(session):  # noqa: ANN001, ANN202
    import uuid

    from services.common.models import Blob, Document, KnowledgeBase, User, Workspace

    tag = uuid.uuid4().hex[:8]
    user = User(email=f"stale-strat-{tag}@t.local", password_hash="x", display_name="欠账测试")
    session.add(user)
    session.flush()
    ws = Workspace(name=f"stale-strat-ws-{tag}", owner_id=user.id)
    session.add(ws)
    session.flush()
    kb = KnowledgeBase(
        workspace_id=ws.id, name=f"stale-strat-{tag}", visibility="private",
        embed_model="BAAI/bge-small-zh-v1.5", chunk_size=500, chunk_overlap=50,
        created_by=user.id, chunk_strategy="semantic",
    )
    session.add(kb)
    session.flush()
    sha = f"stratsha{tag}"
    session.add(Blob(sha256=sha, size_bytes=1, storage_path=f"{sha}/original"))
    session.flush()

    def _doc(name, quality, *, deleted=False, current=True):
        d = Document(
            kb_id=kb.id, file_name=name, content_hash=sha, blob_sha=sha,
            doc_source="upload", status="ready", is_current=current,
            quality=quality,
            deleted_at=dt.datetime.now(dt.UTC) if deleted else None,
        )
        session.add(d)
        return d

    _doc("recursive.txt", {"strategy": "recursive"})
    _doc("semantic.txt", {"strategy": "semantic"})
    _doc("pre166.txt", None)                      # D166 之前：quality 为空
    _doc("deleted.txt", {"strategy": "recursive"}, deleted=True, current=False)
    session.flush()
    return {"kb_id": kb.id, "ws_id": ws.id, "user_id": user.id, "blob": sha}


@pg
def test_condition_counts_only_current_mismatched_docs():
    from sqlalchemy import func, select

    from services.api.routers.kb import stale_strategy_condition
    from services.common.db import sync_session_scope
    from services.common.models import Document

    with sync_session_scope() as s:
        ids = _mk(s)
        try:
            def count(strategy: str) -> int:
                return int(s.execute(
                    select(func.count(Document.id)).where(
                        stale_strategy_condition(ids["kb_id"], strategy)
                    )
                ).scalar_one())

            # 库现策略 semantic ⇒ recursive.txt + pre166.txt(NULL→recursive) 是欠账；semantic.txt 不是
            assert count("semantic") == 2, "应数到 recursive.txt 与 pre166.txt 两份"
            # 库现策略 recursive ⇒ 只有 semantic.txt 是欠账（NULL 兜底成 recursive，不算）
            assert count("recursive") == 1, "NULL 应按默认 recursive 计，不该虚报"
            # 已删/非当前的 deleted.txt 永远不算
            assert count("semantic") == 2
        finally:
            from services.common.models import Blob, KnowledgeBase, User, Workspace

            s.query(Document).filter(Document.kb_id == ids["kb_id"]).delete(
                synchronize_session=False)
            s.query(KnowledgeBase).filter(KnowledgeBase.id == ids["kb_id"]).delete(
                synchronize_session=False)
            s.query(Workspace).filter(Workspace.id == ids["ws_id"]).delete(
                synchronize_session=False)
            s.query(User).filter(User.id == ids["user_id"]).delete(
                synchronize_session=False)
            s.query(Blob).filter(Blob.sha256 == ids["blob"]).delete(
                synchronize_session=False)
            s.commit()
