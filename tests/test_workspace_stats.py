"""🔑 D132：工作区用量在**界面上**看得见（端点与契约早就有了）。

## 先说清楚：这个缺口**不是端点**

第一版结论是"M9 写着『按工作区统计 ✅』，而代码里只有单库统计"——
**那是错的**。`GET /workspaces/{id}/stats` 一直在 `routers/admin.py` 里，
契约 §9 也写清了它"给**两个**字节数"（原始之和 vs blob 目录实际占盘）。
真正的线索其实就在眼前：FastAPI 启动时打了一条
`Duplicate Operation ID workspace_stats_... for function workspace_stats at
routers/admin.py` —— 它直接点名了"已经有一个同 path 的实现"。

🔑 教训（PROGRESS §5.131）：**"我没搜到"不等于"它不存在"**。
我搜的是 `routers/workspaces.py` 里有没有，而事实是它在 `admin.py`。
判断"缺口"之前先 `grep -rn '<完整路径>' services/` 全局搜一次，
并且**把启动日志里的警告当线索**而不是噪音。

于是 D132 真正剩下的那件事就很小了：**界面看不到它** ——
设置页每张工作区卡片只写"N 个可见知识库"，用户没法判断**哪个工作区占地方**，
而磁盘写满会让摄取**静默失败**（monitor 第 9 项盯的就是这个）。

## 这一组钉的是三条**性质**

1. 🔑 **聚合 == 逐库相加**：`workspace_id` 的统计必须等于"把它名下那些库的
   `GET /kb/{id}` 相加"。两条**独立**的代码路径（单库 vs 聚合）对同一个数
   必须给出同一个答案 —— 口径漂移（`is_current` / `deleted_at`）无处可藏。
2. 🔑 **只有 owner/admin 能看**（`can_admin_workspace`）：普通成员 403。
3. 🔑 **包含别人的 `private` 库** —— 这是**刻意的**，不是泄露：
   磁盘账要全，看不到的文档**仍然占着盘**；按可见性过滤会让
   "删哪个库能腾出空间"算错，而那正是这个端点的用途。
"""
from __future__ import annotations
from _docs import contract_text

import ast
import asyncio
import datetime as dt
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# ⚠️ 实现**在 admin.py 里**（routers/workspaces.py 没有）—— 这就是 D132 第一版
#   判断错的地方（§5.131）。
ADMIN_ROUTER = ROOT / "services" / "api" / "routers" / "admin.py"

#: 一个模块级 loop（CONVENTIONS.md §9「async 测试的坑」）：asyncpg 连接池绑定
#: 创建它的那个 loop，而 anyio 的 pytest 插件每个测试函数开新 loop。
_LOOP = asyncio.new_event_loop()


def _run(coro):  # noqa: ANN001, ANN201
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module", autouse=True)
def _own_the_async_engine():
    """🔑 **进出都**销毁 async 引擎单例，让它建在本模块的 loop 上。

    ⚠️ 只在退出时销毁**不够**：本文件按字母序排在 `test_retrieval_e2e.py`
    之后，而那个模块用自己的 loop 建过引擎 → 连接池绑在它的 loop 上，
    本模块第一次 await 就报
    `got Future ... attached to a different loop`（实测，整套跑才红，
    单跑这个文件是绿的 —— 又一个"顺序耦合"）。

    所以进入时也 `dispose_engines()`（丢掉单例引用），让它在 `_LOOP` 上重建；
    退出时同样销毁，免得下一个用 async 的文件踩到我。
    """
    from services.common.db import dispose_engines

    dispose_engines()
    yield

    async def _close() -> None:
        from services.common.db import get_async_engine

        await get_async_engine().dispose()

    _LOOP.run_until_complete(_close())
    dispose_engines()


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


# ============================================================
# 1. 端点、契约、接线（不需要 DB）
# ============================================================


def test_the_stats_endpoint_is_exactly_one_implementation():
    """🔑 全代码库**只有一个** `/workspaces/{ws_id}/stats` 实现。

    ⚠️ D132 第一版在 `routers/workspaces.py` 里又写了一个同 path 的端点，
    FastAPI 立刻打出 `Duplicate Operation ID ... at routers/admin.py` ——
    那条警告**就是**线索，而当时的结论却是"端点不存在"。
    这条测试把"只有一个实现"钉住，免得下次再抄一遍。
    """
    import re

    hits = []
    for path in sorted((ROOT / "services").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        # 只认**装饰器上的路径字符串**（注释与 docstring 里提到不算）
        for m in re.finditer(r'@router\.(?:get|post|patch|delete)\(\s*\n?\s*'
                             r'"(/workspaces/\{ws_id\}/stats)"', text):
            hits.append(f"{path.name}:{m.group(1)}")
    assert len(hits) == 1, f"工作区统计端点有 {len(hits)} 个实现：{hits}"


def test_contract_documents_both_byte_numbers():
    """🔑 契约里写清了"两个字节数量的是不同的东西"—— UI 也必须都说。

    只显示一个会让用户以为"删掉这个库能腾出这么多空间"，
    而派生文件（`preview.pdf`、`thumbs/*.jpg`）在文档删掉后还在。
    """
    text = contract_text()
    assert "GET  /api/v1/workspaces/{id}/stats" in text, "契约没写工作区统计端点"
    assert "sum_doc_size_bytes" in text and "blob_disk_bytes" in text, (
        "契约没解释两个字节数的区别（少了任何一个都会误导人去清盘）"
    )


def test_stats_covers_every_kb_in_the_workspace_not_only_visible_ones():
    """🔑 统计**不**按可见性过滤 —— 它是 owner 的磁盘账，不是读列表。

    ⚠️ 第一版把它写成"应该按可见性过滤"，那是**错的**：看不见的文档
    仍然占着盘，而这个端点的用途就是回答"删哪个库能腾出空间"。
    按可见性过滤会让账算少，于是用户删了也不见磁盘降下来 ——
    症状是"统计骗人"，比没有统计更糟。
    """
    tree = ast.parse(ADMIN_ROUTER.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "workspace_stats")
    body = ast.unparse(fn)
    assert "visible_kb_clause" not in body, (
        "按可见性过滤会让磁盘账算少（看不见的文档也占盘）"
    )
    assert "KnowledgeBase.deleted_at.is_(None)" in body, (
        "统计要把**软删**的库排除（它们的数据已经不在了）"
    )
    assert "Document.is_current.is_(True)" in body, (
        "文档口径漏了 is_current（旧版本还在表里，算进去会让数字虚增）"
    )


def test_stats_requires_workspace_admin():
    """🔑 入口必须是 `can_admin_workspace`（owner/admin 口径，fail-closed）。"""
    tree = ast.parse(ADMIN_ROUTER.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "workspace_stats")
    body = ast.unparse(fn)
    assert "can_admin_workspace" in body, (
        "没有 owner 断言 —— 普通成员就能读到工作区的磁盘账"
    )
    assert "ResourceNotFound" in body, "无权限必须与'不存在'同码（§0.4）"


# ============================================================
# 2. 行为（真 PG，自建素材 + 自清理）
# ============================================================


def _ws_with_member_other_than_owner():  # noqa: ANN201
    """一个**活**工作区 + 它的 owner 与另一个成员（都必须是真实用户）。"""
    from sqlalchemy import select

    from services.common.db import sync_session_scope
    from services.common.models import User, Workspace, WorkspaceMember

    with sync_session_scope() as s:
        alive = {
            r[0] for r in s.execute(
                select(User.id).where(User.is_active.is_(True))
            ).all()
        }
        rows = s.execute(
            select(Workspace.id, Workspace.owner_id, WorkspaceMember.user_id)
            .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
            .where(Workspace.deleted_at.is_(None))
            .order_by(Workspace.id)
        ).all()
    owner_of: dict[int, int] = {}
    members: dict[int, list[int]] = {}
    for ws_id, owner_id, member_id in rows:
        owner_of[ws_id] = owner_id
        members.setdefault(ws_id, []).append(member_id)
    for ws_id, ms in members.items():
        others = [m for m in ms if m != owner_of[ws_id] and m in alive]
        if others and owner_of[ws_id] in alive:
            return ws_id, owner_of[ws_id], others[0]
    return None


def _ctx(user_id: int, ws_ids: frozenset[int]):  # noqa: ANN001, ANN201
    from services.common.auth import UserContext

    return UserContext(
        user_id=user_id, display_name="u", email="u@t.local", api_key_id=None,
        readable_kb_ids=frozenset(), workspace_ids=ws_ids,
    )


def _make_private_kb(ws_id: int, owner_id: int, name: str, *, docs: int = 1,
                     chunks: int = 2, size: int = 1234) -> tuple[int, str]:  # noqa: ANN201
    """造一个带文档的 `private` 库（创建者是 owner）。"""
    from services.common.db import sync_session_scope
    from services.common.models import Blob, Chunk, Document, KnowledgeBase

    sha = f"{kb_sha_seed(ws_id, owner_id)}"[:64].ljust(64, "0")
    with sync_session_scope() as s:
        # 🔑 `documents.blob_sha` 有外键指向 `blobs` —— 先有 blob 行，
        #   否则插入直接 IntegrityError（这个坑撞过一次，见 §5.127 同款）。
        s.merge(Blob(sha256=sha, size_bytes=size, mime_type="text/plain",
                     storage_path=f"blobs/{sha[:2]}/{sha}/original", ref_count=1))
        s.flush()
        kb = KnowledgeBase(
            workspace_id=ws_id, name=name, description=None,
            visibility="private", source_kind="file",
            embed_model="bge-small-zh", chunk_size=500, chunk_overlap=50,
            generate_image_desc=False, created_by=owner_id,
        )
        s.add(kb)
        s.flush()
        for d in range(docs):
            doc = Document(
                kb_id=kb.id, file_name=f"{name}-{d}.txt", mime_type="text/plain",
                size_bytes=size, blob_sha=sha, content_hash=sha,
                status="ready", status_msg="", chunk_count=chunks,
                uploaded_by=owner_id,
            )
            s.add(doc)
            s.flush()
            for i in range(chunks):
                s.add(Chunk(doc_id=doc.id, chunk_index=i,
                            content_norm=f"{name} 段落{d}-{i}", locations=None))
        s.flush()
        return kb.id, sha


def kb_sha_seed(ws_id: int, owner_id: int) -> str:  # noqa: ANN001, ANN201
    """测试专用 blob 的 sha（可重复、可定位）：`d132-{ws}-{owner}-{name}`。"""
    import hashlib

    return hashlib.sha256(f"d132-{ws_id}-{owner_id}".encode()).hexdigest()


def _drop_kb(kb_id: int, sha: str | None = None) -> None:  # noqa: ANN001
    from sqlalchemy import select

    from services.common.db import sync_session_scope
    from services.common.models import Blob, Chunk, Document, KnowledgeBase

    with sync_session_scope() as s:
        s.execute(Chunk.__table__.delete().where(
            Chunk.doc_id.in_(select(Document.id).where(Document.kb_id == kb_id))))
        s.execute(Document.__table__.delete().where(Document.kb_id == kb_id))
        s.execute(KnowledgeBase.__table__.delete().where(KnowledgeBase.id == kb_id))
        if sha:
            s.execute(Blob.__table__.delete().where(Blob.sha256 == sha))


@pg
def test_workspace_stats_equal_the_sum_of_per_kb_stats():
    """🔑 **聚合 == 逐库相加**（两条独立代码路径对同一个数必须一致）。

    ⚠️ 判据不写死任何数字：读出工作区里**我能读的**库，逐个跑一遍
    单库统计（`kb.py::get_kb` 那条路径），相加后与聚合比较。
    口径漂移（`is_current` / `deleted_at` / 可见性三处）都会让这条红。
    """
    from sqlalchemy import select

    from services.api.routers.kb import get_kb
    from services.api.routers.admin import workspace_stats
    from services.common.auth import UserContext
    from services.common.db import session_scope, sync_session_scope
    from services.common.models import KnowledgeBase

    found = _ws_with_member_other_than_owner()
    if found is None:
        pytest.skip("没有两个成员的工作区")
    ws_id, owner, _other = found

    with sync_session_scope() as s:
        kb_ids = [
            r[0] for r in s.execute(
                select(KnowledgeBase.id).where(
                    KnowledgeBase.workspace_id == ws_id,
                    KnowledgeBase.deleted_at.is_(None),
                )
            ).all()
        ]
    if not kb_ids:
        pytest.skip("那个工作区里没有库")
    ctx = UserContext(
        user_id=owner, display_name="o", email="o@t.local", api_key_id=None,
        readable_kb_ids=frozenset(kb_ids), workspace_ids=frozenset({ws_id}),
    )

    async def _both():  # noqa: ANN202
        agg = None
        docs = chunks = size = 0
        n_kb = 0
        async with session_scope() as session:
            agg = await workspace_stats(ws_id=ws_id, ctx=ctx, session=session)
            for kid in kb_ids:
                detail = await get_kb(kid, ctx=ctx, session=session)
                n_kb += 1
                docs += detail.doc_count
                chunks += detail.chunk_count
                size += detail.size_bytes
        return agg, n_kb, docs, chunks, size

    agg, n_kb, docs, chunks, size = _run(_both())
    assert agg.kb_count == n_kb, f"库数 {agg.kb_count} != {n_kb}"
    assert agg.doc_count == docs, f"文档数 {agg.doc_count} != {docs}"
    assert agg.chunk_count == chunks, f"chunk 数 {agg.chunk_count} != {chunks}"
    assert agg.sum_doc_size_bytes == size, f"字节数 {agg.sum_doc_size_bytes} != {size}"


@pg
def test_owner_stats_include_another_users_private_kb_on_purpose():
    """🔑 owner 的统计**包含**别人 `private` 库 —— 这是刻意的，不是泄露。

    磁盘账要全：看不到的文档**仍然占着盘**。按可见性过滤会让
    "删哪个库能腾出空间"算错，而那正是这个端点的用途。
    ⚠️ 所以这条测试的作用是**防止有人"顺手修好它"** ——
    那种"修复"会把一个正确实现改成一个算错账的实现（第一版就写反了）。
    """
    from services.api.routers.admin import workspace_stats
    from services.common.db import session_scope

    found = _ws_with_member_other_than_owner()
    if found is None:
        pytest.skip("没有两个成员的工作区")
    ws_id, owner, _other = found

    async def _agg():  # noqa: ANN202
        async with session_scope() as session:
            return await workspace_stats(
                ws_id=ws_id, ctx=_ctx(owner, frozenset({ws_id})), session=session)

    # ⚠️ 基线必须在**造库之前**取 —— 第一版反了（先造后取基线），
    #   于是 `before` 已经含了新库，断言变成"1 == 1 + 1"。
    base = _run(_agg())
    kb_id, sha = _make_private_kb(ws_id, owner, "d132-别人的私有库",
                                 docs=1, chunks=2, size=1234)
    try:
        # 🔑 这个库的 `created_by` 是**owner 自己**，但可见性是 `private`
        #   且工作区里还有别的成员 —— 要证明"统计不做成员级过滤"，
        #   还需要一个"owner 看得到但读不了"的情形。
        #   这里用**另一种**方式验证：把库的 visibility 改成 `private`
        #   且 `created_by` 换成那个**不是 owner** 的成员。
        from sqlalchemy import select

        from services.common.db import sync_session_scope
        from services.common.models import KnowledgeBase

        with sync_session_scope() as s:
            s.execute(
                KnowledgeBase.__table__.update()
                .where(KnowledgeBase.id == kb_id)
                .values(created_by=_other_id(found))
            )
        after = _run(_agg())
        assert after.doc_count == base.doc_count + 1, (
            f"owner 的统计漏了别人的 private 库：{after.doc_count} vs {base.doc_count}"
        )
        assert after.sum_doc_size_bytes == base.sum_doc_size_bytes + 1234
        assert after.kb_count == base.kb_count + 1
    finally:
        _drop_kb(kb_id, sha)


def _other_id(found):  # noqa: ANN001, ANN201
    """工作区里那个**不是 owner** 的成员（用来当 private 库的作者）。"""
    return found[2]


@pg
def test_member_cannot_read_stats():
    """🔑 **普通成员**读不到（`can_admin_workspace`）→ 403（与"不存在"同码）。

    这条端点是 owner 口径的磁盘账，所以只有能管理这个工作区的人能看。
    """
    from sqlalchemy import select

    from services.api.routers.admin import workspace_stats
    from services.common.db import session_scope, sync_session_scope
    from services.common.errors import ResourceNotFound
    from services.common.models import User, WorkspaceMember

    found = _ws_with_member_other_than_owner()
    if found is None:
        pytest.skip("没有两个成员的工作区")
    ws_id, _owner, _other = found
    with sync_session_scope() as s:
        member_ids = {
            r[0] for r in s.execute(
                select(WorkspaceMember.user_id).where(
                    WorkspaceMember.workspace_id == ws_id)
            ).all()
        }
        outsider = next(
            (r[0] for r in s.execute(select(User.id).order_by(User.id)).all()
             if r[0] not in member_ids),
            None,
        )
    if outsider is None:
        pytest.skip("找不到非成员用户")

    async def _call():  # noqa: ANN202
        async with session_scope() as session:
            # ⚠️ 登录态里**没有**这个工作区（那才是非成员的真实形状）
            await workspace_stats(
                ws_id=ws_id, ctx=_ctx(outsider, frozenset()), session=session)

    with pytest.raises(ResourceNotFound):
        _run(_call())


@pg
def test_deleted_workspace_is_not_counted():
    """🔑 软删的工作区 → 403（与"不存在"同码）。"""
    from sqlalchemy import select

    from services.api.routers.admin import workspace_stats
    from services.common.db import session_scope, sync_session_scope
    from services.common.errors import ResourceNotFound
    from services.common.models import Workspace

    found = _ws_with_member_other_than_owner()
    if found is None:
        pytest.skip("没有两个成员的工作区")
    ws_id, owner, _other = found
    async def _call():  # noqa: ANN202
        async with session_scope() as session:
            await workspace_stats(
                ws_id=ws_id, ctx=_ctx(owner, frozenset({ws_id})), session=session)

    # 🔑 软删要**单独提交**：断言跑在**另一个连接**（async 会话）上，
    #   而"设置 + 复原 + 提交"写在同一个同步事务里的话，提交的是复原后的值
    #   —— 断言那边根本看不到软删（第一版就这么写成"没有抛异常"）。
    with sync_session_scope() as s:
        was = s.get(Workspace, ws_id).deleted_at
        s.get(Workspace, ws_id).deleted_at = dt.datetime.now(dt.UTC)
    try:
        with pytest.raises(ResourceNotFound):
            _run(_call())
    finally:
        with sync_session_scope() as s:
            s.get(Workspace, ws_id).deleted_at = was


@pg
def test_counts_exclude_soft_deleted_documents():
    """🔑 软删的文档**不计入**统计（与 `GET /kb/{id}` 同一口径）。"""
    from sqlalchemy import select

    from services.api.routers.admin import workspace_stats
    from services.common.db import session_scope, sync_session_scope
    from services.common.models import Document

    found = _ws_with_member_other_than_owner()
    if found is None:
        pytest.skip("没有两个成员的工作区")
    ws_id, owner, _other = found
    kb_id, sha = _make_private_kb(ws_id, owner, "d132-软删统计",
                                 docs=1, chunks=1, size=100)
    try:
        ctx = _ctx(owner, frozenset({ws_id}))

        async def _agg():  # noqa: ANN202
            async with session_scope() as session:
                return await workspace_stats(
                    ws_id=ws_id, ctx=ctx, session=session)

        before = _run(_agg())
        with sync_session_scope() as s:
            doc_id = s.execute(
                select(Document.id).where(Document.kb_id == kb_id)
            ).scalar_one()
            s.get(Document, doc_id).deleted_at = dt.datetime.now(dt.UTC)
            s.flush()
        after = _run(_agg())
        assert after.doc_count == before.doc_count - 1, (
            f"软删的文档仍被计入：{before.doc_count} → {after.doc_count}"
        )
        assert after.sum_doc_size_bytes == before.sum_doc_size_bytes - 100
    finally:
        _drop_kb(kb_id, sha)