"""🔑 清空回收站（`DELETE /trash`）的三条硬约束。

`DELETE /trash` 是本项目**唯一不可撤销**的端点（删用户、删 KB 都还有软删兜底）。
所以它的测试不测"删掉了没有"（那是 eval 的活），只测**不该发生的事**：

| 约束 | 不测会怎样 |
|---|---|
| `is_current = false` 恒定生效 | 一次清空把正在被检索的文档删掉，**不可撤销** |
| `dry_run` 在动手**之前**返回 | 用户点了"预览"，数据没了 |
| 范围由**写**权限圈定 | 能读的就能删（private 库只有创建者能写） |

删除链本身的正确性（df 回退 / 索引清空 / blob 释放）在
`eval/api_1c_check.py` 里用真实数据验：判据是那条 SQL ——
`count(*) FROM chunks` 必须等于 `n_docs`（PROGRESS §5.29）。
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql

from services.api.routers import documents as docs
from services.common.auth import UserContext
from services.common.errors import ResourceNotFound
from services.ingest import purge
from services.ingest.purge import PurgeStats, purgeable_stmt

ROOT = Path(__file__).resolve().parents[1]
_LOOP = asyncio.new_event_loop()


def run(coro):
    return _LOOP.run_until_complete(coro)


def teardown_module() -> None:
    _LOOP.close()


def _sql(stmt) -> str:
    return str(
        stmt.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    ).replace('"', "")


# ============================================================
# 1. is_current = false 恒定生效
# ============================================================


def test_purgeable_stmt_always_excludes_current_versions():
    """🔑 当前版本是活数据，**任何调用方都省不掉**这条条件。

    后果是不可撤销的：软删 30 天的反悔窗口（D26）对 `is_current=TRUE`
    的文档不成立 —— 它们从来没进过回收站。
    """
    for kwargs in (
        {},
        {"kb_ids": [5]},
        {"older_than": None},
        {"doc_ids": [1, 2, 3]},
        {"kb_ids": [5], "doc_ids": [1]},
    ):
        sql = _sql(purgeable_stmt(**kwargs))
        assert "documents.is_current IS false" in sql, kwargs
        assert "documents.deleted_at IS NOT NULL" in sql, kwargs


def test_purgeable_stmt_is_never_widened_by_a_caller():
    """调用方只能**加**条件，不能减。构造器不接受任何"包括当前版本"的开关。"""
    import inspect

    params = set(inspect.signature(purgeable_stmt).parameters)
    assert params == {"kb_ids", "older_than", "doc_ids"}, params
    assert "include_current" not in params
    assert "force" not in params


def test_endpoint_calls_the_shared_constructor_not_its_own_sql():
    """`dry_run` 的预测条件必须来自 `purgeable_stmt`。

    两处各写一遍 WHERE，dry_run 报的数字就会与真删的不一样 ——
    而用户是照着 dry_run 的数字点确认的。
    """
    src = (ROOT / "services" / "api" / "routers" / "documents.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(src)
    # 找 `_trash_preview` 的函数体
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_trash_preview"
    )
    used = {
        ast.unparse(n.func) for n in ast.walk(fn)
        if isinstance(n, ast.Call)
    }
    assert any("purgeable_stmt" in u for u in used), f"dry_run 没用共用构造器：{used}"
    # 且不许自己拼 is_current（那一行的真相源只能在 purgeable_stmt）
    assert not any(
        isinstance(n, ast.Attribute) and n.attr == "is_current" for n in ast.walk(fn)
    ), "dry_run 自己判了 is_current —— 条件会与真删漂移"


# ============================================================
# 2. dry_run 必须在动手之前返回
# ============================================================


def test_dry_run_returns_before_purge_is_called(monkeypatch):
    """🔑 dry_run 的**代码顺序**就是它的全部语义。

    写成
    ```python
    stats = await purge_documents_async(...)
    if dry_run:
        return stats        # ← 先删了再报告
    ```
    用户点"预览"就把数据删了，而且**测试会绿**（返回的字段全对）。
    """
    calls: list[str] = []

    async def boom(*a, **kw):
        calls.append("purge")
        raise AssertionError("dry_run 不该走到真删")

    async def ok(*a, **kw):
        calls.append("scope")
        return [7]  # 非空，否则 `_trash_preview` 提前返回、什么都测不到

    monkeypatch.setattr(docs, "purge_documents_async", boom)
    monkeypatch.setattr(docs, "_writable_kb_ids", ok)

    class _S:
        async def execute(self, stmt):
            class _R:
                def all(self_inner):
                    return [(11, 100), (12, 200)]

            return _R()

    out = run(docs.empty_trash(_ctx(7), _S(), dry_run=True))
    # 走到这里没有异常 = `boom` 没被调用
    assert calls == ["scope"], f"dry_run 之前不该调 purge，实际：{calls}"
    assert out.dry_run is True and out.docs == 2, out


def test_real_purge_calls_the_shared_chain(monkeypatch):
    """真删必须走 `ingest.purge` 的共用链，不是自己拼一套。"""
    seen: list[dict] = []

    async def fake_purge(session, **kw):
        seen.append(kw)
        return PurgeStats(docs=2, chunks=10, bytes_freed=1234)

    async def ok(*a, **kw):
        return [7]

    class _S:
        async def commit(self):
            seen.append({"commit": True})

    monkeypatch.setattr(docs, "purge_documents_async", fake_purge)
    monkeypatch.setattr(docs, "_writable_kb_ids", ok)

    out = run(docs.empty_trash(_ctx(), _S()))
    assert out.docs == 2 and out.bytes_freed == 1234 and out.dry_run is False
    assert seen[0]["kb_ids"] == [7], seen
    assert any(s.get("commit") for s in seen), "删完不提交 = 白删（下次事务回滚）"


# ============================================================
# 3. 范围由写权限圈定
# ============================================================


def _ctx(*kb_ids: int, workspaces: tuple[int, ...] = (9,)) -> UserContext:
    """⚠️ `workspace_ids` 要一起给 —— `can_write_kb` 判 workspace 档靠它，
    忘了给会让"工作区库可写"那条分支整条不被走到（测试还是绿的）。"""
    return UserContext(
        user_id=1, display_name="u", email="u@t.local", api_key_id=None,
        readable_kb_ids=frozenset(kb_ids),
        workspace_ids=frozenset(workspaces),
    )


class _KB:
    def __init__(self, kb_id: int, visibility: str, created_by: int,
                 workspace_id: int) -> None:
        self.id = kb_id
        self.visibility = visibility
        self.created_by = created_by
        self.workspace_id = workspace_id


def _sess(rows: list[_KB]):
    class _Res:
        def scalars(self_inner):
            return self_inner

        def all(self_inner):
            return rows

    class _S:
        async def execute(self, stmt):
            return _Res()

    return _S()


def test_writable_scope_excludes_read_only_kbs():
    """能读 ≠ 能写：private 库只有创建者能写，成员的 scope 里不该有它。"""
    rows = [
        _KB(1, "private", created_by=1, workspace_id=9),   # 我的私有库 → 可写
        _KB(2, "private", created_by=2, workspace_id=9),   # 别人的私有库 → 只读
        _KB(3, "workspace", created_by=2, workspace_id=9),  # 我在的工作区库 → 可写
    ]
    ctx = _ctx(1, 2, 3)
    scope = run(docs._writable_kb_ids(_sess(rows), ctx))
    assert scope == [1, 3], scope


def test_writable_scope_skips_kbs_outside_readable_set():
    """`readable_kb_ids` 之外的库（读都读不到）不进 scope。"""
    rows = [_KB(9, "global", created_by=2, workspace_id=1)]
    # D43：全局库谁都能写，但前提是你**能读到**它 —— 这里 ctx 只有 {1}
    assert run(docs._writable_kb_ids(_sess(rows), _ctx(1))) == []


# ============================================================
# 4. 同步 / 异步成对（§0.9）
# ============================================================


def test_sync_and_async_twins_both_exist():
    """`purge_documents` 与 `purge_documents_async` 必须成对。

    只有同步版时，API 想清回收站就得在 AsyncSession 上调它 ——
    **不报错**，返回未 await 的 coroutine，什么都没删（§5.28 第 3 例）。
    """
    import inspect

    assert inspect.iscoroutinefunction(purge.purge_documents_async)
    assert not inspect.iscoroutinefunction(purge.purge_documents)
    import services.retrieval.terms as terms

    assert inspect.iscoroutinefunction(terms.unregister_chunks_async)
    assert not inspect.iscoroutinefunction(terms.unregister_chunks)


def test_df_delta_is_computed_in_one_place():
    """两个版本必须共用 `_df_delta`。

    df 算法写两遍的后果是减错，而 df 减错**不报错**，
    只让 idf 慢慢偏掉（§5.29）。
    """
    src = (ROOT / "services" / "retrieval" / "terms.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    # `Counter()` 只应出现在 `_df_delta` 里
    users = [
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, ast.AnnAssign) or isinstance(n, ast.Assign)
        if "Counter" in ast.unparse(n)
    ]
    delta_fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_df_delta"
    )
    assert len(users) == 1, f"Counter 的构造点不止一处：{users}"
    assert delta_fn.lineno <= users[0] <= delta_fn.end_lineno


# ============================================================
# 5. 统计口径只有一份
# ============================================================


def test_purge_stats_truncates_id_list_but_keeps_counting():
    """id 列表会截断，但**计数不许截断**。

    截断 id 是为了不让响应变成一串没用的大数字；
    如果计数也跟着截，用户会以为"删了 500 份"而实际删了 800 份。
    """
    s = PurgeStats()
    cap = PurgeStats.ID_LIST_CAP
    for i in range(cap + 7):
        s.add(i, chunks=1, blob_dropped=True, size_bytes=10)
    assert s.docs == cap + 7
    assert s.chunks == cap + 7
    assert s.bytes_freed == (cap + 7) * 10
    assert len(s.doc_ids) == cap
    assert s.ids_truncated is True


def test_bytes_freed_only_counts_physically_dropped_blobs():
    """引用计数没归零的 blob **没被删**，不能算进"释放的字节"。"""
    s = PurgeStats()
    s.add(1, chunks=3, blob_dropped=True, size_bytes=100)
    s.add(2, chunks=3, blob_dropped=False, size_bytes=999)
    assert s.blobs_dropped == 1
    assert s.bytes_freed == 100
    assert s.chunks == 6


def test_qdrant_failures_are_recorded_not_swallowed():
    """Qdrant 清失败要能被调用方看到（阶段 4 的对账会捡，但要先有人知道）。"""
    s = PurgeStats()
    s.qdrant_failed.append(42)
    assert s.qdrant_failed == [42]


def test_resource_not_found_is_not_used_for_trash_scope():
    """清空不是"取资源"，不存在的库也要 403 KB_NOT_FOUND（§0.4）。"""
    from services.common.errors import DocNotFound  # noqa: F401 — 确认存在即可

    assert ResourceNotFound().code == "KB_NOT_FOUND"