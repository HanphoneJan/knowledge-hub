"""🔑 增量索引（D114）：同名文件内容变了 → 旧版本进回收站。

## 这一轮修的真 bug

`_create_doc`（目录/聊天导入）只判"同名**且**同内容"就跳过，
**同名但内容变了就再建一行 Document**。于是同一个库里出现**两个
`is_current=True` 的同名文档**，后果有三条：

| 后果 | 症状 |
|---|---|
| 搜索返回同一张图的两份内容 | 用户看到"重复结果"，不知道哪个是真的 |
| 旧版本的 chunk 与 Qdrant 点永远不清 | `/admin/consistency` 与 BM25 df 持续漂移 |
| 上传端点 `scalar_one_or_none()` 抛 `MultipleResultsFound` | **那个库再也传不了同名文件**（500） |

第三条最阴：它是**上一次错误导入留下的**，于是"我上周导了一次照片，
今天传同名的文件就 500" —— 症状与原因隔了一周。

## 所以这里不只测 `versions.py` 的纯函数，还要钉住"接上了"

纯函数对但**没人在调用点用它**，症状与没写一样（这正是 D36 的教训：
"handler 写好了，永远不会被调用"）。所以下面有一组**读源码**的断言。
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest

from services.common.models import Chunk, Document
from services.ingest.versions import current_by_name, supersede

ROOT = Path(__file__).resolve().parents[1]

# ============================================================
# 1. 纯函数
# ============================================================


class _FakeDoc:
    """最小替身：只要有那几个字段就行（不碰 DB → 能离线跑）。"""

    def __init__(self, *, is_current=True, deleted_at=None):
        self.is_current = is_current
        self.deleted_at = deleted_at
        self.id = 1


def test_supersede_marks_not_current_and_deleted():
    d = _FakeDoc()
    assert supersede(d) is True
    assert d.is_current is False
    assert d.deleted_at is not None


def test_supersede_is_idempotent():
    """⚠️ 已经是历史版本的文档不该被再动一次。

    否则重复处理同一份数据会不断刷新 `deleted_at` ——
    而 `deleted_at` 是回收站 **30 天到期**的判据（D26），
    刷新它等于**给用户的反悔窗口重新计时**，而用户根本没做新操作。
    """
    d = _FakeDoc(is_current=False, deleted_at=dt.datetime(2020, 1, 1, tzinfo=dt.UTC))
    assert supersede(d) is False
    assert d.deleted_at == dt.datetime(2020, 1, 1, tzinfo=dt.UTC), "到期时间被刷新了"


def test_supersede_none_is_noop():
    assert supersede(None) is False


def test_supersede_uses_utc():
    """⚠️ 必须带时区：PG 列是 `timestamptz`，naive datetime 会被当成本地时间。

    这台机器是 +08:00 → "删除于"差 8 小时，回收站 30 天到期也跟着偏。
    第一版有**两份**实现，其中一份写了 naive —— 分叉的代价。
    """
    d = _FakeDoc()
    supersede(d)
    assert d.deleted_at.tzinfo is not None, "deleted_at 必须是 aware datetime"


def test_supersede_accepts_explicit_now():
    d = _FakeDoc()
    fixed = dt.datetime(2030, 5, 1, tzinfo=dt.UTC)
    supersede(d, now=fixed)
    assert d.deleted_at == fixed


def test_current_by_name_predicate_fields():
    """🔑 判据必须含 `is_current IS TRUE`。

    不含的话，**软删的旧版本会被再次 supersede** ——
    它的 `deleted_at` 被刷新（30 天窗口重新计时），而且它根本不是"当前版本"。
    """
    cols = []
    for c in current_by_name(1, "a.jpg"):
        cols.append(str(c))
    assert any("is_current" in c for c in cols)
    assert any("file_name" in c for c in cols)
    assert any("kb_id" in c for c in cols)


# ============================================================
# 2. 🔑 调用点真的接上了（读源码，纯函数对但没人调用 = 白写）
# ============================================================


def _code_only(text: str) -> str:
    """去注释/docstring —— 断言"代码里不该有什么"时会先撞上注释。"""
    import re

    text = re.sub(r'"""[\s\S]*?"""', "", text)
    out = []
    for line in text.splitlines():
        line = re.sub(r"\s+#.*$", "", line)
        if line.lstrip().startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


def test_create_doc_calls_supersede():
    """🔴 `_create_doc` 必须在"同名不同内容"这条路上调用 `supersede`。

    这是本轮修的核心。第一版**没有**这个调用，症状是库里出现两个
    同名 current 文档 + 上传同名文件 500。
    """
    src = (ROOT / "services" / "worker" / "handlers" / "__init__.py").read_text(
        encoding="utf-8"
    )
    body = src[src.index("def _create_doc("):src.index("def handle_import_chat(")]
    code = _code_only(body)
    assert "supersede(find_current(" in code.replace("\n", "").replace("    ", ""), (
        "_create_doc 里没有调用 supersede —— 同名不同内容会建出第二个 current 文档"
    )
    assert "find_current" in code


def test_upload_endpoint_uses_the_shared_predicate():
    """⚠️ 上传端点也必须用**同一份**判据（`current_by_name`）。

    它原来写死 `scalar_one_or_none()`，而导入路径允许两个同名 current →
    那个库之后每次上传同名文件都 `MultipleResultsFound` → 500。
    """
    src = (ROOT / "services" / "api" / "routers" / "documents.py").read_text(
        encoding="utf-8"
    )
    code = _code_only(src)
    assert "current_by_name(" in code
    # 🔑 不能再有 scalar_one_or_none 的版本判定
    assert "Document.is_current.is_(True),\n            )\n        )\n    ).scalar_one_or_none()" not in src, (
        "上传端点还有自己的一份 current 版本判定（应为 versions.current_by_name）"
    )


def test_soft_delete_paths_do_not_call_supersede():
    """🔑 **软删 ≠ 版本替换**：删文档 / 删 KB 不该调用 `versions.supersede`。

    两者写的字段一样（`is_current=False` + `deleted_at`），语义不同：

    | 动作 | 含义 | 恢复后 |
    |---|---|---|
    | supersede | 这个版本被**新版本**取代了 | 该出现的是新版本 |
    | 软删 | **用户删掉**它 | 该什么都不出现 |

    混用的后果是"删掉的文档在旧版本列表里被恢复出来" —— 而用户明确删过它。

    ⚠️ 第一版的断言写成"除 `versions.py` 外任何文件都不许出现
    `is_current = False`"，结果软删路径（documents 的删除端点、kb 的删除端点）
    全被误报 —— **用字符串搜字段守不住语义**，只会被误报训练成"这条测试不用看"。
    """
    docs = _code_only(
        (ROOT / "services" / "api" / "routers" / "documents.py").read_text(
            encoding="utf-8"
        )
    )
    kb = _code_only(
        (ROOT / "services" / "api" / "routers" / "kb.py").read_text(encoding="utf-8")
    )
    # 软删端点里只有 `load_document(...)` → 赋 deleted_at → 赋 is_current 这条路
    assert "supersede(" not in _soft_delete_region(docs), (
        "删除文档端点调用了 versions.supersede —— 那是版本替换，不是删除"
    )
    assert "supersede(" not in kb, (
        "删除 KB 的路径调用了 versions.supersede —— 那是版本替换，不是删除"
    )


def _soft_delete_region(documents_py_code: str) -> str:
    """取「删除文档」那个端点的函数体（它也含上传端点的 supersede 调用，故要定位准）。"""
    start = documents_py_code.index("doc.deleted_at = dt.datetime.now(dt.UTC)")
    return documents_py_code[start: start + 400]


def test_both_paths_import_the_shared_implementation():
    """🔑 两条需要「版本替换」的路径都从 `versions` 导入，不是各写一遍。"""
    for rel in ("services/api/routers/documents.py",
                "services/worker/handlers/__init__.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "from ...ingest.versions import" in src, (
            f"{rel} 没有从 versions 导入（supersede 逻辑可能又分叉了）"
        )


def test_create_doc_returns_outcome_tag():
    """返回值必须带"为什么"（`created` / `superseded` / `same_content`）。

    只有一个 Document 时，调用方**没法区分**"新建"与"替换"，
    于是用户看到的增量报告会是"建了 N 个"而实际其中 N 个是替换 ——
    数字对了但含义错了。
    """
    src = (ROOT / "services" / "worker" / "handlers" / "__init__.py").read_text(
        encoding="utf-8"
    )
    body = src[src.index("def _create_doc("):src.index("def handle_import_chat(")]
    code = _code_only(body)
    assert '"same_content"' in code
    assert '"superseded" if superseded else "created"' in code.replace("\n", " ").replace("    ", " ") or (
        "superseded" in code and "created" in code
    )


def test_dir_handler_counts_superseded_separately():
    """🔑 `superseded` 必须**单独计数**。

    混进 `created` 或 `skipped` 的话，用户无法判断增量导入对不对
    （"跳过了多少 = 没变"、"替换了多少 = 变了"是两种不同的信息）。
    """
    src = (ROOT / "services" / "worker" / "handlers" / "__init__.py").read_text(
        encoding="utf-8"
    )
    # ⚠️ 按"下一个顶层 def"切，不要按分隔符切 —— 我第一版用 `\n# ====`
    #   做锚点，结果 `handle_import_dir` 后面根本没有那个分隔符，
    #   报错是 `ValueError: substring not found`，完全指不到"函数名写错"。
    body = src[src.index("def handle_import_dir("):]
    m = re.search(r"\ndef (?!handle_import_dir)", body[10:])
    body = body[: m.start() + 10] if m else body
    assert "superseded" in body
    assert body.count("superseded=superseded") >= 1, "report 里没有 superseded 计数器"


# ============================================================
# 3. 端到端（有 Postgres 时验真实行为）
# ============================================================


def _pg_alive() -> bool:
    """⚠️ `sync_session_scope()` 给的是 **Session**，不是 `Connection`。

    第一版写 `s.exec_driver_sql(...)` —— 那是 `Connection` 的方法，
    于是**每次调用都 AttributeError**、永远返回 False，
    下面两条真跑数据库的测试**全部被 skip**。

    而 **skip 掉的测试比没有测试更坏**：CI 全绿，却从没验过核心行为。
    """
    try:
        from sqlalchemy import text

        from services.common.db import sync_session_scope

        with sync_session_scope() as s:
            s.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


def _mk_kb(session) -> int:
    """建一套 1 用户 + 1 工作区 + 1 KB（同步 Session 版）。

    ⚠️ `documents.blob_sha` 有外键指向 `blobs(sha256)`，而 `_create_doc`
    自己会 `register_blob` —— 所以这里**不要**预建 blob 行。
    """
    import uuid

    from services.common.models import KnowledgeBase, User, Workspace

    tag = uuid.uuid4().hex[:8]
    user = User(email=f"incr-{tag}@t.local", password_hash="x", display_name="增量测试")
    session.add(user)
    session.flush()
    ws = Workspace(name=f"incr-ws-{tag}", owner_id=user.id)
    session.add(ws)
    session.flush()
    kb = KnowledgeBase(
        workspace_id=ws.id, name=f"incr-{tag}", visibility="private",
        embed_model="BAAI/bge-small-zh-v1.5", chunk_size=500, chunk_overlap=50,
        created_by=user.id,
    )
    session.add(kb)
    session.flush()
    return kb.id


def _drop_kb_workspace_user(s, kb_id: int) -> bool:
    """删干净一个测试 KB 及其**工作区与用户**（**用调用方的 session**，按外键顺序）。

    🔑 第一版只删 Document + KB，于是**一次 pytest 攒 8 个 `incr-ws-*`
    工作区**留在库里：它们出现在 `GET /workspaces` 里、出现在巡检的
    可见范围里，久了之后真实用户的列表被测试垃圾淹没。

    🔑 **必须用调用方的 `s`**，不要在函数里自己 `sync_session_scope()`：
    第二版那样写会与外层事务交叉 —— 外层刚 `_mk_kb` 的行**还没提交**，
    内层 session 看不到，于是 `if kb is None: return` **静默返回、什么也没删**；
    出了 `with` 外层再提交，垃圾就留下了。症状是"清理函数明明跑了，
    每跑一次却多留一行"—— 连查 5 次才定位。

    🔑 `kb is None` 时**不静默**：返回 False，让调用方断言"真的删了"。
    静默早退会把"没删"伪装成"删了"。

    「测试自己清理干净」和「不留垃圾」是同一件事 —— 后者做不到时，
    前者也做不到（异常路径必漏）。

    @return 是否真的找到并删掉了那个 KB
    """
    from services.common.models import KnowledgeBase, Job, User, Workspace
    from services.retrieval.terms import unregister_chunks

    kb = s.get(KnowledgeBase, kb_id)
    if kb is None:
        return False
    ws_id = kb.workspace_id
    owner_id = kb.created_by

    doc_ids = [d.id for d in s.query(Document.id).filter(
        Document.kb_id == kb_id).all()]
    if doc_ids:
        # 🔑 **先撤 df，再删行**：顺序反了就无从对照（chunk 行没了，
        #   `unregister_chunks` 拿不到正文 → df 永久多出来）。
        #   ⚠️ 这不是为了"让测试好看"——它暴露了一个真实缺口：
        #   `_create_doc` 会 enqueue `KIND_INGEST`，worker 稍后建 chunk 并
        #   登记 df；而测试已经把文档行删了，于是 df 里留下 10 个幽灵 chunk
        #   （实测 `n_docs=562` vs `chunk 行=552`）。
        rows = s.query(Chunk.doc_id, Chunk.content_norm).filter(
            Chunk.doc_id.in_(doc_ids)).all()
        if rows:
            unregister_chunks(s, [[r.content_norm] for r in rows])
        # 已入队的 ingest 任务也别留着跑（它会给已删文档建 chunk）
        s.query(Job).filter(Job.doc_id.in_(doc_ids)).delete(
            synchronize_session=False)
        s.flush()

    s.query(Document).filter(Document.kb_id == kb_id).delete(
        synchronize_session=False)
    s.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).delete(
        synchronize_session=False)
    s.flush()
    # 工作区里可能还有别的 KB（不会：这是测试专用工作区），
    # 但仍要检查后再删 —— 直接删会撞外键，报错完全指不到"顺序错了"。
    left = s.query(KnowledgeBase).filter(
        KnowledgeBase.workspace_id == ws_id).count()
    if left == 0:
        s.query(Workspace).filter(Workspace.id == ws_id).delete(
            synchronize_session=False)
        s.flush()
    s.query(User).filter(User.id == owner_id).delete(synchronize_session=False)
    return True


@pytest.mark.skipif(not _pg_alive(), reason="Postgres 不在线")
def test_cleanup_leaves_no_workspace_behind():
    """🔑 反向断言：跑完一轮之后**不该**多出工作区与用户。

    第一版只删 Document+KB，一次 pytest 就攒 8 个 `incr-ws-*` 工作区。
    这条测试把"不留垃圾"变成**可执行的判据**，而不是靠人记得。
    """
    from services.common.db import sync_session_scope
    from services.common.models import User, Workspace

    with sync_session_scope() as s:
        before_ws = s.query(Workspace).filter(
            Workspace.name.like("incr-ws-%")).count()
        before_u = s.query(User).filter(User.email.like("incr-%")).count()

    with sync_session_scope() as s:
        kb_id = _mk_kb(s)
        # 🔑 断言它**真的删了**（而不是静默早退，见函数 docstring）
        assert _drop_kb_workspace_user(s, kb_id) is True, "清理函数没找到那个 KB"
        s.commit()

    with sync_session_scope() as s:
        after_ws = s.query(Workspace).filter(
            Workspace.name.like("incr-ws-%")).count()
        after_u = s.query(User).filter(User.email.like("incr-%")).count()
    assert (after_ws, after_u) == (before_ws, before_u), (
        f"测试留下了垃圾：工作区 {before_ws}→{after_ws}、用户 {before_u}→{after_u}"
    )


@pytest.mark.skipif(not _pg_alive(), reason="Postgres 不在线")
def test_incremental_via_real_db():
    """🔴 真实跑一遍「同名不同内容」，逐项断言（不是恒真断言）。

    | 步骤 | 断言 |
    |---|---|
    | 第一次 `_create_doc` | `created`，库里有 1 个 current |
    | 同样的字节再来一次 | `same_content`，current 仍是 1（幂等） |
    | **同名、内容变了** | `superseded`，current 仍 1，旧的那行被软删 |
    | 旧行检查 | `is_current=False` 且 `deleted_at` 非空 |
    | 不同名同内容 | `created`（另一个文件名就是另一份文档，不该被当成重复） |
    """
    from sqlalchemy import func, select

    from services.common.db import sync_session_scope
    from services.worker.handlers import _create_doc

    NAME = "同名不同内容.txt"
    from services.common.models import KnowledgeBase

    with sync_session_scope() as s:
        kb_id = _mk_kb(s)
        kb = s.get(KnowledgeBase, kb_id)

    def cur_count(s) -> int:
        return int(s.execute(
            select(func.count()).select_from(Document).where(
                Document.kb_id == kb_id, Document.file_name == NAME,
                Document.is_current.is_(True))
        ).scalar_one())

    # ① 第一次：新建
    with sync_session_scope() as s:
        doc, outcome = _create_doc(
            s, kb=kb, file_name=NAME, data=b"v1", mime_type="text/plain",
            doc_source="photo_dir", source_path=None, uploaded_by=None)
        assert doc is not None and outcome == "created", outcome
        assert cur_count(s) == 1

    # ② 同名同内容：幂等跳过
    with sync_session_scope() as s:
        doc2, outcome2 = _create_doc(
            s, kb=kb, file_name=NAME, data=b"v1", mime_type="text/plain",
            doc_source="photo_dir", source_path=None, uploaded_by=None)
        assert doc2 is None and outcome2 == "same_content", outcome2
        assert cur_count(s) == 1, "重复导入不该再建一行"

    # ③ 同名不同内容：旧版本进回收站
    with sync_session_scope() as s:
        old_id = doc.id
        doc3, outcome3 = _create_doc(
            s, kb=kb, file_name=NAME, data=b"v2-longer-content", mime_type="text/plain",
            doc_source="photo_dir", source_path=None, uploaded_by=None)
        assert doc3 is not None and outcome3 == "superseded", outcome3
        assert doc3.id != old_id
        assert cur_count(s) == 1, f"current 必须仍是 1（实际 {cur_count(s)}）"

    # ④ 旧行确实被软删（不是只改了 is_current）
    with sync_session_scope() as s:
        old = s.get(Document, old_id)
        assert old.is_current is False
        assert old.deleted_at is not None, "旧版本必须带 deleted_at（进回收站）"

    # ⑤ 不同名同内容 → 另一份文档（不该被当成重复跳过）
    with sync_session_scope() as s:
        doc4, outcome4 = _create_doc(
            s, kb=kb, file_name="另一个名字.txt", data=b"v1", mime_type="text/plain",
            doc_source="photo_dir", source_path=None, uploaded_by=None)
        assert doc4 is not None and outcome4 == "created", outcome4

    with sync_session_scope() as s:
        assert _drop_kb_workspace_user(s, kb_id) is True
        s.commit()


@pytest.mark.skipif(not _pg_alive(), reason="Postgres 不在线")
def test_supersede_does_not_refresh_expiry_of_older_version():
    """🔑 连续三次同名不同内容 → **只有最新那个是 current**，前两个都带 deleted_at。

    且前一个的 `deleted_at` **不应被第三次导入刷新** ——
    刷新等于给用户的 30 天反悔窗口重新计时，而用户没做任何新操作（D26）。
    """
    import datetime as dt

    from services.common.db import sync_session_scope
    from services.worker.handlers import _create_doc

    NAME = "三连替换.txt"
    from services.common.models import KnowledgeBase, User, Workspace

    with sync_session_scope() as s:
        kb_id = _mk_kb(s)
        kb = s.get(KnowledgeBase, kb_id)
        ws_id, owner_id = kb.workspace_id, kb.created_by

    ids = []
    for i in range(3):
        with sync_session_scope() as s:
            d, oc = _create_doc(
                s, kb=kb, file_name=NAME, data=f"v{i}".encode(),
                mime_type="text/plain", doc_source="photo_dir",
                source_path=None, uploaded_by=None)
            assert d is not None and oc == ("created" if i == 0 else "superseded"), oc
            ids.append(d.id)
        # ⚠️ 睡 1ms：PG 的 `timestamptz` 精度是微秒，
        #   而 `deleted_at` 在两次导入之间**必须能分辨出不同值**，
        #   否则"有没有被刷新"这个断言测不出任何东西。
        import time

        time.sleep(0.01)

    with sync_session_scope() as s:
        rows = [s.get(Document, i) for i in ids]
        assert rows[2].is_current is True
        assert rows[2].deleted_at is None
        for r in rows[:2]:
            assert r.is_current is False and r.deleted_at is not None
        # 🔑 前一个的删除时间没被第三次刷新
        assert rows[0].deleted_at < rows[1].deleted_at, (
            "更早的版本 deleted_at 被刷新了 —— 30 天窗口会重新计时"
        )
        # ⚠️ 在**外层** session 里删，而不是调 `_drop_kb_workspace_user`：
        #   那个函数自己开一个 session，于是这里出现**两个事务交叉**，
        #   外层的 `s.commit()` 又会把已删对象当待提交改动 ——
        #   症状是"清理跑了但库里还剩一行"，极难定位。
        for m in (Document,):
            s.query(m).filter(m.kb_id == kb_id).delete(synchronize_session=False)
        s.query(KnowledgeBase).filter(
            KnowledgeBase.id == kb_id).delete(synchronize_session=False)
        s.flush()
        ws = s.query(Workspace).filter(Workspace.id == ws_id).one()
        s.delete(ws)
        s.flush()
        s.delete(s.get(User, owner_id))
        s.commit()


def _pg_alive() -> bool:
    try:
        from services.common.db import sync_session_scope

        with sync_session_scope() as s:
            s.exec_driver_sql("SELECT 1")
        return True
    except Exception:  # noqa: BLE001
        return False
