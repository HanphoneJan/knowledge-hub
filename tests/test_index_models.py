"""🔑 D191：Embedding 模型一致性守卫（文章「为什么 Embedding 模型必须保持一致」）。

索引与查询必须用同一个模型：不一致时向量空间不通用，排序基础整个失效，
而且**不报错**。这一组钉住三件事：

1. 判据（`model_mismatch`）的三态：一致 → None · 不一致 → 原因 ·
   **没有记录 → None**（未知不等于不一致，在未知上拦查询会拦死正常部署）。
2. 查询路径真的会因此降级：引擎把 session 传给 dense 腿、dense 腿真的校验。
3. 「谁建的索引」真的被写下来：入库 / 重建 / 视觉三条路径都记。

前两条有纯函数 + 源码级断言，不需要 DB / 模型。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DENSE = ROOT / "services" / "retrieval" / "dense.py"
ENGINE = ROOT / "services" / "retrieval" / "engine.py"
PIPE = ROOT / "services" / "ingest" / "pipeline.py"
HANDLERS = ROOT / "services" / "worker" / "handlers" / "__init__.py"
CHUNKER = ROOT / "services" / "ingest" / "chunker.py"


# ============================================================
# 1. 判据（纯函数）
# ============================================================


def test_no_record_means_unknown_and_passes():
    """空表 / 加这一列之前的索引 ⇒ 未知 ⇒ **放行**。

    把它当"不一致"会把每个还没重新摄取的部署拦死 —— 而"没记录"与
    "记录得不一样"是两件事。
    """
    from services.common.index_models import model_mismatch

    assert model_mismatch(None, "BAAI/bge-small-zh-v1.5", 512) is None


def test_matching_model_and_dim_passes():
    from services.common.index_models import IndexModel, model_mismatch

    rec = IndexModel(collection="kb_dense", model="m", dim=512)
    assert model_mismatch(rec, "m", 512) is None


def test_different_model_fails_with_actionable_reason():
    """换个模型名要报出来，且原因里要带**怎么修**（指向 reindex）。"""
    from services.common.index_models import IndexModel, model_mismatch

    rec = IndexModel(collection="kb_dense", model="old-model", dim=512)
    reason = model_mismatch(rec, "new-model", 512)
    assert reason and "old-model" in reason and "new-model" in reason
    assert "reindex" in reason, "原因要给出可操作的修法"


def test_same_model_name_but_different_dim_fails():
    """同名不同维度也要拦：维度不同时向量库根本无法比对。"""
    from services.common.index_models import IndexModel, model_mismatch

    rec = IndexModel(collection="kb_dense", model="m", dim=512)
    assert model_mismatch(rec, "m", 768) is not None


# ============================================================
# 2. 查询路径真的会降级
# ============================================================


def _fn(path: Path, name: str) -> ast.AST:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 里没有函数 {name}")


def test_engine_passes_session_to_dense_leg():
    """引擎必须把 session 传给 dense 腿 —— 否则那条腿拿不到表，校验不生效。

    ⚠️ D207 起三条 DB 腿**各拿一个自己的会话**（真并发），所以判据从
    `session=session` 改成"有 `session=` 关键字 + 会话来自工厂"，
    仍然钉住"dense 拿到会话"这个性质。
    """
    src = ast.unparse(_fn(ENGINE, "_gather_legs"))
    assert "search_dense" in src
    assert "session=" in src, "dense 腿没拿到 session，D191 校验不会跑"
    assert "get_async_sessionmaker" in src, (
        "dense 腿应使用独立的 per-leg 会话（D207 真并发）"
    )


def test_dense_leg_checks_embedding_consistency():
    src = ast.unparse(_fn(DENSE, "search_dense"))
    assert "text_model_mismatch_async" in src, "dense 腿没有做 Embedding 一致性校验"
    assert "skipped=mismatch" in src, "不一致时要降级成 skipped，而不是照常查"


# ============================================================
# 3. 「谁建的索引」被写下来
# ============================================================


def test_ingest_records_the_text_model():
    src = ast.unparse(_fn(PIPE, "_index_text_legs"))
    assert "record_current_text_model" in src


def test_visual_handler_records_the_visual_model():
    src = ast.unparse(_fn(HANDLERS, "handle_visual"))
    assert "record_current_visual_model" in src


def test_payload_carries_embed_model():
    """文章要求把模型信息写进元数据 —— payload 里要有 `embed_model` 自证。"""
    from services.ingest.chunker import Chunk, build_payload

    pl = build_payload(
        Chunk(text="x"), chunk_index=0, kb_id=1, doc_id=1, workspace_id=1,
        visibility="private", source_kind="file", owner_id=1,
        file_name="f.md", mime_type=None,
    )
    assert pl.get("embed_model"), "payload 没带上建索引的模型名"
