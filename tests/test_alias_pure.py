"""🔑 D193：别名模式/蓝绿重建的**纯**判据（进 CI 门禁，不需要 Qdrant）。

整合性的"真 Qdrant 跑通"在 `tests/test_alias_mode.py`（需要在线 Qdrant，会 skip）。

对应文章「索引别名切换是生产级灰度和回滚的常用做法」。本轮按用户决定**改动了 D179**
（原为"暂不做"），默认仍是真实集合，别名模式为可选开启。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HANDLERS = ROOT / "services" / "worker" / "handlers" / "__init__.py"
ADMIN = ROOT / "services" / "api" / "routers" / "admin.py"


def test_alias_switch_is_delete_then_create_in_one_payload():
    """🔑 切换必须**一个请求**里 delete + create —— 分两次发中间有"别名不存在"的窗口。"""
    from services.common.qdrant import alias_switch_actions

    acts = alias_switch_actions("kb_dense", "kb_dense__g1")
    assert acts[0]["delete_alias"]["alias_name"] == "kb_dense"
    assert acts[1]["create_alias"] == {
        "collection_name": "kb_dense__g1", "alias_name": "kb_dense",
    }


def test_physical_name_keeps_kind_prefix():
    from services.common.qdrant import COLL_DENSE, physical_name

    assert physical_name(COLL_DENSE, "20261014T1200") == "kb_dense__20261014T1200"


def test_collection_body_infers_kind_from_suffix():
    """`kb_sparse__g1` 要建成稀疏集合、`kb_visual__g1` 要建成多向量 —— 后缀不能骗人。"""
    from services.common.qdrant import (
        SPARSE_VECTOR_NAME,
        VISUAL_VECTOR_NAME,
        collection_body,
    )

    sparse = collection_body("kb_sparse__g1")
    assert "sparse_vectors" in sparse and SPARSE_VECTOR_NAME in sparse["sparse_vectors"]
    visual = collection_body("kb_visual__g1")
    assert VISUAL_VECTOR_NAME in visual["vectors"]
    assert "multivector_config" in visual["vectors"][VISUAL_VECTOR_NAME]
    dense = collection_body("kb_dense__g1")
    assert "vectors" in dense and "sparse_vectors" not in dense


def test_upsert_detects_sparse_by_kind_not_exact_name():
    """🔴 回归：`upsert_points` 判定稀疏要按**腿的种类**（`kb_sparse__<gen>` 也算）。

    按精确名 `== "kb_sparse"` 判的话，蓝绿重建写影子稀疏集合时不会被包上向量名 ·
    ⇒ Qdrant 400（端到端实测踩到过）。这条是**源码级**判据（真跑在 mode 文件里）。
    """
    src = ast.unparse(_fn(ROOT / "services" / "common" / "qdrant.py", "upsert_points"))
    assert "_collection_kind(" in src, "upsert_points 没按种类判定稀疏"


def _fn(path: Path, name: str) -> ast.AST:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 里没有函数 {name}")


def test_handle_reindex_has_swap_branch():
    src = ast.unparse(_fn(HANDLERS, "handle_reindex"))
    assert "swap" in src and "_swap_rebuild" in src, "handle_reindex 没有 swap 分支"


def test_swap_rebuild_verifies_before_switching():
    """顺序铁律：**先验证、后切换**。

    ⚠️ 判据要比**调用行号**，不能比子串位置（函数顶部 import 里两个名字都在）。
    """
    fn = _fn(HANDLERS, "_swap_rebuild")

    def call_lines(name: str) -> list[int]:
        return [
            n.lineno for n in ast.walk(fn)
            if isinstance(n, ast.Call) and ast.unparse(n.func).endswith(name)
        ]

    verify, switch = call_lines("pg_point_ids"), call_lines("switch_alias")
    assert verify and switch, f"没找到验证/切换调用：{verify=} {switch=}"
    assert min(verify) < min(switch), "验证必须在切换别名之前"
    assert "delete_collection" in ast.unparse(fn), "校验不过时要能放弃影子集合"


def test_swap_rebuild_accepts_logical_names_for_testability():
    """逻辑腿名是**参数**（默认常量）—— 隔离测试不必 monkeypatch 常量。"""
    fn = _fn(HANDLERS, "_swap_rebuild")
    args = fn.args
    kwargs = {a.arg for a in args.kwonlyargs}
    assert {"dense_logical", "sparse_logical"} <= kwargs


def test_swap_rebuild_converts_orm_rows_and_keeps_encoding():
    """🔴 回归（实跑踩到）：`chunks_of` 里是 **ORM `Chunk` 行**，必须过
    `chunker.from_rows` 才能喂 `_index_text_legs`（它读 `.text`）。

    漏了就是 `AttributeError: 'Chunk' object has no attribute 'text'` ——
    而且当时还连着留下三套孤儿影子集合（清理没包住建过程）。
    编码口径也必须带上该库的 `chunk_strategy`（`late` 库才与入库时一致）。
    """
    src = ast.unparse(_fn(HANDLERS, "_swap_rebuild"))
    assert "from_rows(" in src, "没把 ORM Chunk 行转成 chunker.Chunk（实跑会 AttributeError）"
    assert "chunk_strategy" in src, "重建没带该库切分策略（late 库向量会与入库时不同）"


def test_swap_rebuild_drops_shadow_on_any_failure():
    """🔴 回归：建过程里任何一步失败都要放弃影子集合（否则重试各留一套孤儿）。"""
    fn = _fn(HANDLERS, "_swap_rebuild")
    has_except_cleanup = any(
        isinstance(n, ast.ExceptHandler) and "_drop_shadow" in ast.unparse(n)
        for n in ast.walk(fn)
    )
    assert has_except_cleanup, "失败时没有放弃影子集合 —— 会留下孤儿集合"


def test_swap_endpoint_requires_superadmin():
    """整实例重建会动所有租户的向量 —— 必须超管。"""
    src = ast.unparse(_fn(ADMIN, "reindex"))
    assert "assert_superadmin" in src
    assert src.index("swap") < src.index("readable_kb_ids"), "swap 分支要先于按库范围短路"
