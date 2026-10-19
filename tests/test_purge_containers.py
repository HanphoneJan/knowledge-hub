"""D125：库/工作区的**物理清理**（30 天到期）+ 顺序纪律。

## 缺口

D26/D42 承诺「软删除，保留 30 天后物理清理」。文档那一半实现了
（`purge_documents`），**库与工作区那一半从来没实现** ——
`purgeable_stmt` 只看 `documents`。后果：软删的库永远占着行与 blob，
而界面上又没有它们的回收站（看得见的垃圾被清了，看不见的没有）。

## 三个必须钉住的点

| 点 | 不钉的后果 |
|---|---|
| **先文档后容器** | 容器先删 → 底下文档的 df 永远减不回去 → idf 虚高 → **静默**的检索质量下降（§5.29） |
| **不重复走文档链** | 同一批文档清两次 → df 减两次 → idf 变负 → 检索被噪声淹没，也不报错 |
| **不新增 API 端点** | D79 定了 `DELETE /trash` 是**唯一**不可撤销端点；再加"连库带文档清空"就是第二个 |
"""

from __future__ import annotations
from _docs import contract_text

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PURGE = ROOT / "services" / "ingest" / "purge.py"
HANDLERS = ROOT / "services" / "worker" / "handlers" / "__init__.py"


def _code_of(path: Path, fn_name: str) -> str:
    """函数体去掉 docstring 的源码（判据不许被 docstring 里的反例告发）。"""
    import copy

    tree = ast.parse(path.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == fn_name)
    node = copy.deepcopy(fn)
    body = node.body
    if (
        body and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        node.body = body[1:] or [ast.Pass()]
    return ast.unparse(node)


# ============================================================
# 1. 存在且被调用
# ============================================================


def test_purge_containers_exists_and_is_exported():
    from services.ingest import purge

    assert hasattr(purge, "purge_containers")
    assert hasattr(purge, "purgeable_containers_stmt")
    stats = purge.ContainerPurgeStats()
    assert (stats.kbs, stats.workspaces) == (0, 0)


def test_cleanup_handler_purges_documents_before_containers():
    """🔑 顺序：**先 `purge_documents`，后 `purge_containers`**。

    ⚠️ 这是本文件最要紧的一条。反过来的后果**不报错**：
    容器行删掉后底下文档的 df 永远减不回去，idf 虚高，
    检索质量悄悄变差（§5.29 那个 6 倍的实测）。
    """
    body = _code_of(HANDLERS, "handle_cleanup")
    assert "purge_documents" in body and "purge_containers" in body, (
        "handle_cleanup 没有同时清理文档与容器"
    )
    i_docs = body.index("purge_documents(session")
    i_ctrs = body.index("purge_containers(session")
    assert i_docs < i_ctrs, (
        "顺序反了：先删容器再删文档 —— df 就永远减不回去（静默的质量下降）"
    )


def test_purge_containers_does_not_touch_df_or_qdrant():
    """🔑 容器清理**只删容器行**，不重复走文档链。

    同一批文档被清两次 → df 减两次（`_df_delta` 只加不减）→ idf 变负。
    """
    body = _code_of(PURGE, "purge_containers")
    for forbidden in ("unregister_chunks", "remove_from_indexes",
                      "release_blob", "purge_documents"):
        assert forbidden not in body, (
            f"purge_containers 不该碰 {forbidden} —— 那是 purge_documents 的职责"
        )


# ============================================================
# 2. 选择条件（纯 SQL，不碰 session）
# ============================================================


def test_containers_stmt_has_no_is_current_filter():
    """🔑 容器**没有** `is_current` 概念（那是文档的版本机制，D28）。

    给容器加 `is_current=false` 会让条件恒假 —— 因为那列不存在，
    写出来是 SQL 错误；而"看起来对但恒空"的过滤器最会骗人。
    """
    body = _code_of(PURGE, "purgeable_containers_stmt")
    assert "is_current" not in body, "容器没有版本概念，不该按 is_current 过滤"
    assert "deleted_at.isnot(None)" in body.replace(" ", ""), (
        "必须要求真的软删过（否则活库会被删）"
    )


def test_containers_stmt_is_pure_sql_construction():
    """与 `purgeable_stmt` 同形状：只构造 SQL，不碰 session。

    这样同步/异步两条驱动共用**同一个**选择条件，不会漂。
    """
    import inspect

    from services.ingest.purge import purgeable_containers_stmt

    sig = inspect.signature(purgeable_containers_stmt)
    assert "session" not in sig.parameters, "选择条件构造器不该收 session"
    body = _code_of(PURGE, "purgeable_containers_stmt")
    assert "select(" in body and "execute(" not in body, "不该在这里执行查询"


def test_workspaces_are_deleted_before_kbs():
    """🔑 **先删工作区**：它的 KB 靠 `ON DELETE CASCADE` 跟着走。

    反过来虽然也对，但那样就分不清哪些库是"自己到期该删"、
    哪些是"跟着工作区走的"，日志里的库数会少报。
    """
    body = _code_of(PURGE, "purge_containers")
    assert body.index("Workspace") < body.index("KnowledgeBase"), (
        "先删 KnowledgeBase 再删 Workspace —— 级联后分不清哪些库是自己到期该删的"
    )


def test_cascade_deleted_kbs_are_not_double_counted():
    """跟着工作区级联删掉的库**不再重复报数**。"""
    body = _code_of(PURGE, "purge_containers")
    assert "ws_ids" in body, "没有记录被删的工作区 id —— 会重复处理/重复报数"
    # ⚠️ 归一化后再比：`ast.unparse` 不保证保留空格，而
    #   "kb.workspace_id in ws_ids" 里有空格（第一版没归一化就匹配，恒红）
    flat = body.replace(" ", "")
    assert "kb.workspace_idinws_ids" in flat, (
        "没有排除跟着工作区级联删掉的库"
    )


# ============================================================
# 3. 不新增不可撤销端点（D79）
# ============================================================


def test_no_new_irreversible_api_endpoint_for_containers():
    """🔑 容器清理**只能**由 30 天到期路径触发，不能加 API 端点。

    D79 定了 `DELETE /trash` 是**唯一**不可撤销端点。
    「连库带文档一起清空」比清回收站重得多（那是删掉一整个库），
    给它一个端点等于开第二个不可撤销入口。
    """
    api = contract_text()
    for bad in ("DELETE /api/v1/kb/{id}/purge",
                "DELETE /api/v1/workspaces/{id}/purge",
                "POST   /api/v1/kb/purge"):
        assert bad not in api, f"契约里出现了容器清理端点 {bad}（违反 D79）"

    # 而 DELETE /trash 仍然是唯一的不可撤销端点
    from eval import contract_check  # noqa: F401 — 只为确认模块可导入

    src = (ROOT / "services" / "api" / "routers" / "documents.py").read_text(
        encoding="utf-8"
    )
    assert "/trash" in src


@pytest.mark.parametrize(
    "path",
    [
        "services/api/routers/kb.py",
        "services/api/routers/workspaces.py",
    ],
)
def test_container_routers_have_no_delete_purge(path: str):
    """两个容器路由里都不许出现 `purge` 字样的处理函数。"""
    src = (ROOT / path).read_text(encoding="utf-8")
    assert "purge" not in src.replace("purgeable_stmt", ""), (
        f"{path} 里出现了 purge —— 容器清理只走 worker 的到期路径（D79）"
    )


# ============================================================
# 4. 缺口已经补上（原 §4 那条"钉住在案"的测试要反过来）
# ============================================================


def test_purge_module_now_covers_containers():
    """✅ 本轮之前这里断言"缺口存在"，现在断言"缺口已补"。

    留着一个"断言缺口存在"的测试是危险的：实现补上之后它会红，
    而人很容易因此把实现改回去（"让测试绿"优先于"让需求满足"）。
    所以补完必须**同时**把这条测试翻过来。
    """
    src = PURGE.read_text(encoding="utf-8")
    assert "KnowledgeBase" in src and "Workspace" in src, (
        "purge.py 又不处理容器了 —— D26 的『30 天后物理清理』只做了一半"
    )
