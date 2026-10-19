"""异步/同步 session 混用的防回归测试。

## 这组测试为什么存在

2026-10-03 实现上传时踩了一个**极其隐蔽**的坑：

```python
def register_blob(session, sha, size):
    session.execute(pg_insert(Blob).values(...))   # ← 写成了同步版

# API（异步）调用：
await register_blob(session, sha, size)   # 不报错，但 INSERT 从未执行
```

在 `AsyncSession` 上调**同步**的 `session.execute()`：
- **不抛异常**（`AsyncSession.execute` 是 coroutine 函数，返回 coroutine 对象，
  不 await 它就没有任何效果）
- 只有一条 `RuntimeWarning: coroutine ... was never awaited`
- 后果是后面插 `documents` 行时外键报错 `documents_blob_sha_fkey`，
  而日志里**看不到任何指向真正原因的线索**

同一个函数在同步 session（worker）上完全正常，所以只在 API 路径挂。

**教训**：凡是接受 `session` 参数的工具函数，都要明确它是同步还是异步。
同名不同行为是最容易出这种错的形态 —— 所以这里直接
**分成两个名字**（`xxx` / `xxx_async`），不靠调用方自觉。
"""

from __future__ import annotations

import ast
import inspect

import pytest
import sqlalchemy.exc
from sqlalchemy import select

from services.common.db import get_async_sessionmaker, sync_session_scope
from services.common.models import Blob, Document, KnowledgeBase
from services.common.storage import (
    register_blob,
    register_blob_async,
    release_blob,
    release_blob_async,
    store_bytes,
)


# ============================================================
# 辅助
# ============================================================


def _drop_blob(sha: str) -> None:
    """测试间隔离。

    内容寻址让**同内容永远是同一个 sha**，重复跑测试会累积 ref_count
    使断言漂移（第一版就踩了这个：`ref_count=3` 而期望 2）。
    """
    with sync_session_scope() as s:
        s.execute(Blob.__table__.delete().where(Blob.sha256 == sha))


def _blob_row(sha: str) -> Blob | None:
    with sync_session_scope() as s:
        return s.execute(select(Blob).where(Blob.sha256 == sha)).scalar_one_or_none()


# ============================================================
# 1. 命名约定：异步版本必须用 `_async` 后缀
# ============================================================


@pytest.mark.parametrize("name", ["register_blob", "release_blob"])
def test_async_variants_exist_with_explicit_names(name: str):
    """有同步版的 blob 操作，必须有对应的 `_async` 版本。

    分成两个名字而不是靠调用方自觉 —— 这类 bug 靠自觉一定会再犯。
    """
    import services.common.storage as st

    assert hasattr(st, f"{name}_async"), f"缺 {name}_async"


def test_no_unawaited_execute_in_async_helpers():
    """🔑 `_async` 函数里不能出现未 await 的 `session.execute(`。

    用 AST 的**父子关系**判断，而不是文本匹配：
    - `await session.execute(...)` 里，Call 节点的父节点是 Await
    - 漏掉 await 时，Call 变成独立表达式语句，父节点不是 Await

    文本匹配会误报 —— 第一版是因为 docstring 里提到 `session.execute(` 就被判不合格；
    改成比对 unparse 文本也还是不匹配，因为 awaited 那个 unparse 出来**带参数**。
    """
    import ast

    import services.common.storage as st

    for name in ("register_blob_async", "release_blob_async"):
        fn = getattr(st, name)
        assert inspect.iscoroutinefunction(fn), f"{name} 应该是 async def"

        tree = ast.parse(inspect.getsource(fn))
        for parent in ast.walk(tree):
            for node in ast.iter_child_nodes(parent):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "execute"):
                    continue
                called = ast.unparse(node.func.value) + ".execute"
                if not isinstance(parent, ast.Await):
                    pytest.fail(
                        f"{name} 里 {called}() 没有被 await —— "
                        f"在 AsyncSession 上它返回 coroutine 却不报错，"
                        f"语句根本不会发出（2026-10-03 踩过）"
                    )


# ============================================================
# 2. 同步路径（worker 用）
# ============================================================


def test_register_blob_sync_inserts_row():
    sha, size = store_bytes(b"blob-sync-test")
    _drop_blob(sha)

    with sync_session_scope() as s:
        register_blob(s, sha, size)

    row = _blob_row(sha)
    assert row is not None, "❌ 同步路径没插上行"
    assert row.size_bytes == size
    # 🔑 首次登记就是**一次引用**，所以是 1 不是 0。
    # 写成 0 的后果：单引用的 blob 被 release 一次就物理删除，
    # 而那个文档还在引用它 → 后续读取 FileNotFoundError（实测发现）。
    assert row.ref_count == 1, f"ref_count={row.ref_count}（期望 1）"
    _drop_blob(sha)


def test_register_blob_sync_increments_refcount():
    sha, _ = store_bytes(b"blob-refcount-test")
    _drop_blob(sha)

    with sync_session_scope() as s:
        register_blob(s, sha, size := 10)
    with sync_session_scope() as s:
        register_blob(s, sha, size)

    row = _blob_row(sha)
    assert row is not None and row.ref_count == 2, f"ref_count={row.ref_count if row else None}"
    _drop_blob(sha)


def test_release_blob_sync_drops_at_zero():
    sha, size = store_bytes(b"blob-release-test")
    _drop_blob(sha)

    with sync_session_scope() as s:
        register_blob(s, sha, size)
    with sync_session_scope() as s:
        assert release_blob(s, sha) is True, "唯一一次引用被释放后应物理删除"
    assert _blob_row(sha) is None


def test_release_blob_sync_keeps_when_referenced():
    sha, size = store_bytes(b"blob-keep-test")
    _drop_blob(sha)

    with sync_session_scope() as s:
        register_blob(s, sha, size)
    with sync_session_scope() as s:
        register_blob(s, sha, size)
    assert _blob_row(sha).ref_count == 2, "两次登记 = 2 次引用"

    with sync_session_scope() as s:
        assert release_blob(s, sha) is False, "还有 1 次引用时不该物理删除"
    assert _blob_row(sha) is not None
    assert _blob_row(sha).ref_count == 1
    _drop_blob(sha)


# ============================================================
# 3. 异步路径（API 用）—— 原 bug 的回归测试
# ============================================================


def test_register_blob_async_actually_inserts(run_async):
    """🔑 回归：异步 session 上必须真的插上行。

    原 bug：`session.execute(...)` 没 await → coroutine 从未执行 →
    行不存在 → 后续 `documents` 插入触发外键错误。
    """
    sha, size = store_bytes(b"blob-async-test")
    _drop_blob(sha)

    async def go():
        factory = get_async_sessionmaker()
        async with factory() as session:
            await register_blob_async(session, sha, size)
            await session.commit()

    run_async(go())
    assert _blob_row(sha) is not None, (
        "❌ 异步路径没插上行 —— 就是那个 never-awaited 的坑"
    )
    _drop_blob(sha)


def test_register_blob_async_increments_refcount(run_async):
    sha, size = store_bytes(b"blob-async-refcount")
    _drop_blob(sha)

    async def go():
        factory = get_async_sessionmaker()
        for _ in range(2):
            async with factory() as session:
                await register_blob_async(session, sha, size)
                await session.commit()

    run_async(go())
    row = _blob_row(sha)
    assert row is not None and row.ref_count == 2, (
        f"ref_count={row.ref_count if row else None}"
    )
    _drop_blob(sha)


def test_release_blob_async_drops_at_zero(run_async):
    sha, size = store_bytes(b"blob-async-release")
    _drop_blob(sha)

    async def reg():
        factory = get_async_sessionmaker()
        async with factory() as session:
            await register_blob_async(session, sha, size)
            await session.commit()

    async def rel():
        factory = get_async_sessionmaker()
        async with factory() as session:
            dropped = await release_blob_async(session, sha)
            await session.commit()
            return dropped

    run_async(reg())
    assert run_async(rel()) is True
    assert _blob_row(sha) is None


# ============================================================
# 4. 外键完整性
# ============================================================


def test_documents_blob_sha_has_foreign_key():
    """🔑 `documents.blob_sha` 有 FK 到 `blobs.sha256`。

    原 bug 正是踩在这条约束上。断言这条约束**存在**，
    这样"忘了登记 blob"会以明确的 IntegrityError 暴露，
    而不是变成静默的数据不一致。
    """
    sha, _ = store_bytes(b"blob-fk-test")
    _drop_blob(sha)

    with sync_session_scope() as s:
        kb = s.execute(select(KnowledgeBase.id).limit(1)).scalar_one_or_none()
    if kb is None:
        pytest.skip("没有可用知识库，先跑 seed_dev")

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        with sync_session_scope() as s:
            s.add(
                Document(
                    kb_id=kb,
                    file_name="fk-test.pdf",
                    content_hash=sha,
                    blob_sha=sha,  # 故意指向未登记的 blob
                    uploaded_by=1,
                )
            )
            s.flush()

    _drop_blob(sha)


# ============================================================
# 5. 其它仓储层现状
# ============================================================


def test_term_registry_is_sync_only():
    """BM25 词表是同步实现（只有 worker 用）。

    API 侧**不应**碰 terms 表 —— 检索时的 idf 计算是只读路径，
    且检索端点在后续阶段。这个测试记录"当前的事实"，
    避免有人以为它有 async 版而直接调。
    """
    import services.retrieval.terms as terms

    assert hasattr(terms, "register_chunks")
    assert not hasattr(terms, "register_chunks_async"), (
        "terms 模块目前是同步专用；要用在 API 侧请先加 async 版"
    )
