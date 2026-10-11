"""🔑 软删除的**反悔窗口**必须真的有（D26 文档 / D42 工作区 · D125）。

## 缺口是怎么发现的

按 REQUIREMENTS 逐条对代码时发现两行字与代码不符：

| REQUIREMENTS 写的 | 代码实际 |
|---|---|
| M1「工作区删除：软删除，**可恢复**，保留 30 天（D42）」 | 只有 `DELETE`，**没有任何地方能恢复** |
| M2「KB 创建/重命名/删除」（D26「保留 30 天后物理清理 / 给误删一个反悔窗口」） | `DELETE /kb/{id}` 把文档一并软删，**没有任何地方能放回去** |

也就是说：**删库在 API 上是不可撤销的**，而 D26 承诺了 30 天窗口。
承诺了却没有的删除，比明说不可撤销更糟 —— 用户以为还有 29 天。

## 还有一个更隐蔽的一半

`delete_kb` 只软删 KB 与文档，而 `purge_documents` **只清文档** ——
所以软删的 KB / 工作区**永远不会被物理清理**，它们的 blob 与行一直占着。
本文件也钉住"清理路径覆盖什么"这条（见 §3）。
"""

from __future__ import annotations
from _docs import contract_text

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _routers_src(name: str) -> str:
    return (
        ROOT / "services" / "api" / "routers" / name
    ).read_text(encoding="utf-8")


def _code_of(fn: ast.AST) -> str:
    """函数体**去掉 docstring** 的源码。

    🔑 判据必须剥掉 docstring：这些函数的说明里**故意**写着
    "`assert_kb_write` 会永远 403"这类反面例子，
    直接对 `ast.unparse(fn)` 做字符串匹配会把自己告发
    （`tests/test_retrieval_filters.py` 早就因为这件事改成 AST，
    这里栽的是同一个坑的另一面：docstring 参与了匹配）。
    """
    import copy

    node = copy.deepcopy(fn)
    body = getattr(node, "body", [])
    if (
        body and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        node.body = body[1:] or [ast.Pass()]
    return ast.unparse(node)


def _endpoints(src: str) -> dict[str, str]:
    """`{路径: handler 名}`。"""
    tree = ast.parse(src)
    out: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            fn = ast.unparse(dec.func)
            if not fn.startswith("router."):
                continue
            for a in dec.args:
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    out.setdefault(a.value, node.name)
    return out


# ============================================================
# 1. 端点存在（这是本文件的核心）
# ============================================================


def test_kb_restore_endpoint_exists():
    """🔑 `DELETE /kb/{id}` 必须有配对的恢复入口。"""
    eps = _endpoints(_routers_src("kb.py"))
    assert "/{kb_id}/restore" in eps, (
        f"没有 KB 恢复端点（现有：{sorted(eps)}）—— 删库不可撤销，"
        "与 D26 的『30 天反悔窗口』不符"
    )


def test_workspace_restore_endpoint_exists():
    """🔑 D42 承诺工作区软删除「可恢复」。"""
    eps = _endpoints(_routers_src("workspaces.py"))
    assert "/{ws_id}/restore" in eps, (
        f"没有工作区恢复端点（现有：{sorted(eps)}）—— D42 承诺了却没实现"
    )


def test_contract_declares_both_restore_endpoints():
    """契约是唯一端点来源（`contract_check.py` 查"实现了但没写"）。"""
    text = contract_text()
    assert "POST   /api/v1/kb/{id}/restore" in text, "契约没写 KB 恢复"
    assert "POST   /api/v1/workspaces/{id}/restore" in text, "契约没写工作区恢复"


# ============================================================
# 2. 恢复的鉴权：软删的行**不在**授权集合里
# ============================================================


def test_restore_does_not_rely_on_assert_helpers_that_filter_soft_deleted():
    """🔑 恢复端点**不能**直接调 `assert_kb_write` / `assert_workspace_admin`。

    原因（实测会静默失效）：`load_user_context` 过滤 `deleted_at IS NULL`，
    所以软删的行不在 `readable_kb_ids` / `workspace_ids` 里 ——
    `assert_kb_write` 会**永远 403**，恢复功能等于不存在（还不报错）。

    ✅ 正确形状：先按 `deleted_at IS NOT NULL` 取行，再用 `can_write_kb`
    / `owner_id` 判权限。判据是**真的用了那两种写法**。
    """
    kb_src = _routers_src("kb.py")
    tree = ast.parse(kb_src)
    fn = next(n for n in tree.body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "restore_kb")
    body = _code_of(fn)
    assert "assert_kb_write" not in body, (
        "restore_kb 用了 assert_kb_write —— 它过滤软删行，恢复永远 403"
    )
    assert "can_write_kb" in body, "restore_kb 没有显式判写权限"
    assert "deleted_at.isnot(None)" in body.replace(" ", ""), (
        "restore_kb 没有按『软删的行』取 —— 取不到就无从恢复"
    )

    ws_src = _routers_src("workspaces.py")
    wtree = ast.parse(ws_src)
    wfn = next(n for n in wtree.body
               if isinstance(n, ast.AsyncFunctionDef)
               and n.name == "restore_workspace")
    wbody = _code_of(wfn)
    assert "assert_workspace_admin" not in wbody, (
        "restore_workspace 用了 assert_workspace_admin —— 同样会永远 403"
    )
    assert "owner_id" in wbody, "restore_workspace 没要求 owner"


def test_restore_reuses_the_same_error_code_as_not_found():
    """🔑 恢复失败与"不存在"**同码**（`ResourceNotFound` → `KB_NOT_FOUND`）。

    否则攻击者能靠 403/404 的差异探测"这个 id 有个软删的库"（§0.4）。
    """
    for name, fn_name in (("kb.py", "restore_kb"),
                          ("workspaces.py", "restore_workspace")):
        tree = ast.parse(_routers_src(name))
        fn = next(n for n in tree.body
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == fn_name)
        body = _code_of(fn)
        assert "ResourceNotFound" in body, f"{fn_name} 没有抛 ResourceNotFound"
        assert "InvalidRequest" not in body and "Forbidden" not in body, (
            f"{fn_name} 用了别的错误类型 —— 会与『不存在』可区分"
        )


# ============================================================
# 3. 只恢复"这次删除带走的"（不许顺手复活别人删的）
# ============================================================


def test_kb_restore_restores_by_timestamp_equality():
    """🔑 恢复的文档判据是 `deleted_at == kb.deleted_at`（**相等**，不是 `<=`）。

    `delete_kb` 把**同一个** `kb.deleted_at` 盖到它带走的每份文档上，
    所以那批文档共享这个时间戳。

    ⚠️ 用 `<=` 会把回收站里**更早单独删掉**的、以及**被同名新版本顶替**的
    文档一起复活 —— 那是用户另外删的东西。
    第一版的验收脚本就是在这里写错了断言
    （把"仍在回收站"当成"被复活了"），差点把对的实现改成错的。
    """
    tree = ast.parse(_routers_src("kb.py"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "restore_kb")
    body = _code_of(fn).replace(" ", "")
    assert "Document.deleted_at==stamp" in body, (
        "恢复文档没有用『时间戳相等』判据（用 <= 会复活别人删的文档）"
    )
    assert "Document.deleted_at<=stamp" not in body, (
        "用了 <= —— 会把更早单独删掉的文档一起复活"
    )


def test_kb_restore_reingests_documents():
    """🔑 恢复的文档必须**重新入队摄取**。

    D44：旧版本向量只留 3 天；即使还在，期间改过 `chunk_size` 的话
    旧切法也已经过时。不重跑 = 恢复出来一份搜不到的文件。
    """
    tree = ast.parse(_routers_src("kb.py"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "restore_kb")
    body = _code_of(fn)
    assert "enqueue_async" in body and "KIND_INGEST" in body, (
        "KB 恢复没有重新入队摄取 —— 恢复出来的文档搜不到"
    )


def test_workspace_restore_does_not_cascade():
    """🔑 工作区恢复**只翻自己那一行**。

    删除时**故意没有**级联软删其下的 KB/文档（`delete_workspace` 的说明），
    所以恢复也不该级联 —— 否则会把别人单独删掉的库/文档一起复活。
    """
    tree = ast.parse(_routers_src("workspaces.py"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.AsyncFunctionDef)
              and n.name == "restore_workspace")
    body = _code_of(fn)
    assert "KnowledgeBase" not in body and "Document" not in body, (
        "restore_workspace 级联改了 KB/文档 —— 删除时并没有级联，语义不对称"
    )


# ============================================================
# 4. "保留 30 天后物理清理"那一半
# ============================================================


def test_purge_covers_containers_too():
    """🔑 D26/D42 的「30 天后物理清理」现在**三样都覆盖**：文档 / 库 / 工作区。

    ⚠️ 这条测试**原来是反的**：它断言"purge 不处理容器"，用来把缺口钉在案。
    缺口补上之后（D125 的 `purge_containers`）它会红 ——
    而人很容易为了让测试绿而把实现改回去
    （"让测试绿"优先于"让需求满足"，是这类测试最大的风险）。
    所以补完必须**同时**把它翻过来，让它盯住"覆盖面不许缩回去"。

    覆盖面本身的细则（顺序、不重复走文档链、不新增端点）
    在 `tests/test_purge_containers.py`。
    """
    src = (ROOT / "services" / "ingest" / "purge.py").read_text(encoding="utf-8")
    for name in ("Document", "KnowledgeBase", "Workspace"):
        assert name in src, f"清理路径不再覆盖 {name} —— D26 的 30 天窗口有了缺口"


@pytest.mark.parametrize(
    "path",
    [
        "services/api/routers/kb.py",
        "services/api/routers/workspaces.py",
    ],
)
def test_soft_delete_still_sets_the_timestamp(path: str):
    """软删两处都必须写 `deleted_at`（恢复的判据就是它非空）。"""
    src = (ROOT / path).read_text(encoding="utf-8")
    assert "deleted_at = dt.datetime.now(dt.UTC)" in src, f"{path} 没有写 deleted_at"
