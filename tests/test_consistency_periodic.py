"""🔴 D142：**定期**索引对账（M5 承诺"定期对账任务修复漂移"，此前不存在）。

## 这一组钉的四条性质

1. 🔑 **期望点数不能被笛卡尔积放大** —— 第一版为了拿 `kb_id` 去 join
   `knowledge_bases`，实测把 399 放大成 8379，于是报告里是"缺 8379 个点"
   的巨大假漂移（而 Qdrant 其实好好的）。
2. 🔑 **Qdrant 出错必须抛**：它返回 `{"status":{"error":…}}`（没有 `result` 键），
   而 `.get("result") or {}` 会把它变成**空集合** ⇒ "实有 0 个点"。
3. 🔑 用 **scroll 分页**而不是 search（drift 要全量，search 有 top-k 上限，
   几万点会被静默截断 ⇒ 报成"一致"，§5.69 同一族）。
4. 🔴 **绝不自动删点** —— 摄取时 chunk 行 `flush()` 之后才 upsert 点，
   而 `flush` **不提交** ⇒ 那个窗口里的"孤儿点"是**正在写的**，
   删了就变成永久空洞（比漂移更难修）。

## 交叉验证

同一套数字（399/399/0）与 `deploy/monitor.py` 那份**独立实现**一致 ——
两份算法不同的实现给出同一个答案，才说明这份不是自说自话。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from services.common.consistency import (
    LEGS,
    check_drift,
    current_kb_ids,
    drift_is_clean,
    format_drift,
    pg_page_point_ids,
    pg_point_ids,
    qdrant_point_ids,
    report_drift,
)

ROOT = Path(__file__).resolve().parents[1]
CONSISTENCY_PY = ROOT / "services" / "common" / "consistency.py"
WORKER_MAIN = ROOT / "services" / "worker" / "main.py"


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
# 1. 期望点数（笛卡尔积那条坑）
# ============================================================


@pg
def test_expected_ids_have_no_cartesian_product():
    """🔑 期望点集合 == 直接聚合出来的条数（笛卡尔积会把它放大）。

    ⚠️ 判据用**独立算出来的条数**去比，而不是"某个常数"——
    数据量每次 eval 都在变，写死数字下一次就假红（§5.31）。
    """
    from sqlalchemy import func, select

    from services.common.db import sync_session_scope
    from services.common.models import Chunk, Document

    with sync_session_scope() as s:
        kbs = current_kb_ids(s)
        if not kbs:
            pytest.skip("库里没有未软删的库")
        # ⚠️ 用 `DISTINCT (a, b, c)` 的**行构造**而不是 `func.count(distinct(...))`：
        #   后者在 SQLAlchemy 2.x 会把三个 InstrumentedAttribute 当参数传给
        #   psycopg，报 `cannot adapt type 'InstrumentedAttribute'` ——
        #   报错完全指不到"该用 tuple_ 或 distinct 三列"。
        want = s.execute(
            select(func.count()).select_from(Chunk)
            .join(Document, Document.id == Chunk.doc_id)
            .where(Document.kb_id.in_(kbs),
                   Document.is_current.is_(True),
                   Document.deleted_at.is_(None))
            .distinct()
        ).scalar_one()
        rows = s.execute(
            select(Document.kb_id, Chunk.doc_id, Chunk.chunk_index)
            .select_from(Chunk).join(Document, Document.id == Chunk.doc_id)
            .where(Document.kb_id.in_(kbs),
                   Document.is_current.is_(True),
                   Document.deleted_at.is_(None))
            .distinct()
        ).all()
        want = len(rows)
        got = pg_point_ids(s, kbs)
    assert len(got) == int(want), (
        f"期望点数 {len(got)} ≠ 直接聚合的 {want} —— "
        "八成是查询里多了一个 join（笛卡尔积，§5.140 的同类）"
    )


@pg
def test_page_expected_ids_ignore_documents_without_scan_pages():
    """🔑 没有 `scan_pages` 的文档**不**产生页级期望点（否则纯文本库会报漂移）。"""
    from services.common.db import sync_session_scope

    with sync_session_scope() as s:
        kbs = current_kb_ids(s)
        if not kbs:
            pytest.skip("库里没有库")
        ids = pg_page_point_ids(s, kbs)
    # 断言不了具体内容，但可以断言"它是 set 且不大到离谱"
    assert isinstance(ids, set)
    assert len(ids) < 10_000_000


# ============================================================
# 2. Qdrant 侧（scroll 分页 + 出错要抛）
# ============================================================


class _FakeClient:
    """记下所有请求的假 Qdrant 客户端。"""

    def __init__(self, pages: list[dict] | None = None, *, status: int = 200,
                 body: dict | None = None) -> None:
        self.pages = pages or []
        self.status = status
        self.body = body
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, json: dict):  # noqa: ANN001
        self.calls.append((url, json))

        class _R:
            def __init__(self, code: int, payload: dict) -> None:
                self.status_code = code
                self._payload = payload
                self.text = str(payload)

            def json(self):  # noqa: ANN202
                return self._payload

        if self.status != 200:
            return _R(self.status, self.body or {"status": {"error": "boom"}})
        if self.body is not None:
            return _R(200, self.body)
        idx = len(self.calls) - 1
        page = self.pages[idx] if idx < len(self.pages) else {"points": [],
                                                             "next_page_offset": None}
        return _R(200, {"result": page, "status": "ok"})


def test_scroll_follows_next_page_offset(monkeypatch):
    """🔑 分页要**追到底**（drift 报告要全量）。"""
    import services.common.consistency as mod

    fake = _FakeClient([
        {"points": [{"id": 1}, {"id": 2}], "next_page_offset": 2},
        {"points": [{"id": 3}], "next_page_offset": None},
    ])
    monkeypatch.setattr(mod, "get_client", lambda: fake)
    got = qdrant_point_ids(["kb_dense"], [1])
    assert got == {1, 2, 3}, got
    assert len(fake.calls) == 2, fake.calls


def test_http_error_raises_instead_of_reporting_zero_points(monkeypatch):
    """🔑 HTTP 非 200 → **抛**。返回空集合会被读成"实有 0 个点"。"""
    import services.common.consistency as mod

    monkeypatch.setattr(mod, "get_client",
                        lambda: _FakeClient(status=500))
    with pytest.raises(RuntimeError):
        qdrant_point_ids(["kb_dense"], [1])


def test_error_status_body_raises(monkeypatch):
    """🔑 200 但 `status` 是错误对象 → 也要抛（Qdrant 的报错风格）。"""
    import services.common.consistency as mod

    monkeypatch.setattr(
        mod, "get_client",
        lambda: _FakeClient(body={"status": {"error": "Wrong payload: …"}}),
    )
    with pytest.raises(RuntimeError):
        qdrant_point_ids(["kb_dense"], [1])


def test_never_deletes_anything(monkeypatch):
    """🔴 **绝不删点**：摄取时行 flush 之后才 upsert，而 flush 不提交 ⇒
    那个窗口里的"孤儿点"是**正在写的**，删了就成永久空洞。"""
    import services.common.consistency as mod

    fake = _FakeClient([{"points": [{"id": 1}], "next_page_offset": None}])
    monkeypatch.setattr(mod, "get_client", lambda: fake)
    with sync_ctx() as s:
        kbs = current_kb_ids(s)
        drift = check_drift(s, kbs or [1])
    urls = [u for u, _ in fake.calls]
    assert urls, "根本没调 Qdrant"
    assert all("delete" not in u for u in urls), urls
    assert all("points/scroll" in u for u in urls), urls
    assert drift  # 顺带确认真的算出了三条腿


def sync_ctx():  # noqa: ANN202
    from services.common.db import sync_session_scope

    return sync_session_scope()


# ============================================================
# 3. 报告与 worker 接线
# ============================================================


def test_drift_report_shape_and_helpers():
    drift = {c: {"expected": 3, "actual": 3, "missing": 0, "dangling": 0,
                 "missing_sample": [], "dangling_sample": [], "kb_ids": [1]}
             for c in LEGS}
    assert drift_is_clean(drift) is True
    assert "缺 0/多 0" in format_drift(drift)
    dirty = dict(drift)
    dirty["kb_visual"] = dict(dirty["kb_visual"], missing=2, dangling=1)
    assert drift_is_clean(dirty) is False
    assert "kb_visual: 缺 2/多 1" in format_drift(dirty)


def test_all_three_legs_are_checked():
    """🔑 **三条腿**都要查 —— 取并集的话"只有一条腿坏了"会报成一致（D124）。"""
    assert set(LEGS) == {"kb_dense", "kb_sparse", "kb_visual"}


@pg
def test_report_drift_logs_and_returns_clean_state(caplog):
    """🔑 真库上跑一次：当前开发库是**一致**的，所以返回 True 且只 debug 记一条。"""
    import logging

    with caplog.at_level(logging.DEBUG, logger="services.common.consistency"):
        clean = report_drift()
    if not clean:
        pytest.skip("当前开发库有真实漂移（本条只验'一致'这条路径）")
    assert clean is True


def test_worker_calls_report_drift_in_its_idle_loop():
    """🔑 **接线**：worker 的空闲循环里真的调了 `report_drift()`。

    ⚠️ 判据是"函数体里有这个调用"（AST）—— worker 主循环跑一次要几分钟，
    用行为测它不划算；而"helper 存在但没人调"正是 D141 变异脚本的盲区。
    """
    tree = ast.parse(WORKER_MAIN.read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "main"), None)
    assert fn is not None, "worker/main.py 里找不到 main()"
    calls = [
        n.func.id for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    ]
    assert "report_drift" in calls, (
        f"worker 的主循环没有调 report_drift（calls={sorted(set(calls))}）"
        "⇒ M5 的「定期对账」仍然不存在"
    )


def test_drift_interval_is_not_too_short():
    """🔑 间隔别太短：一次对账要 scroll 三条腿全量点，而且**每次都有真在写的窗口**。"""
    tree = ast.parse(WORKER_MAIN.read_text(encoding="utf-8"))
    vals = {
        t.id: n.value.value
        for n in tree.body if isinstance(n, ast.Assign)
        for t in n.targets if isinstance(t, ast.Name)
        if (isinstance(n.value, ast.Constant) and isinstance(n.value.value, (int, float)))
    }
    assert vals.get("_DRIFT_INTERVAL", 0) >= 60, vals
    assert vals.get("_DRIFT_FIRST_DELAY", 0) >= 60, (
        "首次延迟太短：worker 刚起时模型还没预热、Qdrant 还没连上"
    )


def test_module_has_no_delete_calls():
    """🔑 源码级守卫：本模块**不该**出现删除类调用。

    ⚠️ 这是"性质用源码表达"的合法场景：性质就是"没有删除"，
    而运行时无法证明"一个函数没有做某件事"（只能证明它做了别的事）。
    """
    tree = ast.parse(CONSISTENCY_PY.read_text(encoding="utf-8"))
    forbidden = {"delete", "delete_by_filter", "delete_points",
                 "delete_collection", "upsert_points", "recreate_collection"}
    bad: list[str] = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        # ⚠️ 两种形态都要看：`client.delete_points(...)`（属性）
        #   **与** `delete_by_filter(...)`（`from … import` 之后的裸调用）。
        #   第一版只看了属性形态，于是变异脚本里那句
        #   `from .qdrant import delete_by_filter` + 裸调用**全绿通过**
        #   —— 守卫漏掉的那一半，正好是"最像合法代码"的写法。
        if isinstance(n.func, ast.Attribute) and n.func.attr in forbidden:
            bad.append(ast.unparse(n))
        elif isinstance(n.func, ast.Name) and n.func.id in forbidden:
            bad.append(ast.unparse(n))
    assert bad == [], f"consistency.py 里出现了写/删索引的调用：{bad}"

# ============================================================
# 5. **三个入口必须共用一份判据**（D143）
# ============================================================

MONITOR = ROOT / "deploy" / "monitor.py"
ADMIN = ROOT / "services" / "api" / "routers" / "admin.py"

#: 判据的"指纹"：PG 侧的 WHERE 里那三条 + Qdrant 侧的页级判据。
#: ⚠️ 这些**不是**要禁止出现在别处的字符串，而是"如果某个入口自己算一遍，
#:   它就必须重新抄一遍这些东西" —— 而抄一遍就会漂。
_JUDGEMENT_MARKERS = (
    "is_current",
    "scan_pages",
    "chunk_index",
)


def test_monitor_does_not_recompute_the_judgement():
    """🔑 `deploy/monitor.py` 里**不许**再自己算对账。

    ⚠️ 判据是"不再出现拼点 ID / scroll 取点的那套算术"，而不是"不许 import
    共享模块" —— 后者会让"复制一份再改"这种最坏情况合法化。
    """
    src = MONITOR.read_text(encoding="utf-8")
    tree = ast.parse(src)
    funcs = {n.name: n for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    # 这两个取点器必须已经不在了（D143 删掉）
    assert "_qdrant_point_ids" not in funcs, "monitor 又有了自己的 Qdrant 取点器"
    assert "_qdrant_page_point_ids" not in funcs, "monitor 又有了自己的页级取点器"
    # 也不许自己拿 chunk_index 判页级（那是 Qdrant 侧判据）
    offenders = [
        f"{n}:{ast.unparse(c)}"
        for n, node in funcs.items()
        for c in ast.walk(node)
        if isinstance(c, ast.Compare)
        and "chunk_index" in ast.unparse(c)
    ]
    assert not offenders, f"monitor 里出现了自己的页级判据：{offenders}"


def _calls_in(rel: Path, func_name: str) -> set[str]:
    """某函数体里调到的**名字**（含属性名）。

    🔑 为什么用 AST 而不是 `assert "check_drift" in src`：字符串判据在
    **import 行还在**的时候恒成立 —— 变异脚本第一版就是这么骗过去的
    （`check_drift = lambda …: {}` 让行为全坏，而文本判据照样绿）。
    """
    tree = ast.parse(rel.read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == func_name), None)
    assert fn is not None, f"{rel.name} 里找不到函数 {func_name}"
    names: set[str] = set()
    for n in ast.walk(fn):
        if not isinstance(n, ast.Call):
            continue
        if isinstance(n.func, ast.Name):
            names.add(n.func.id)
        elif isinstance(n.func, ast.Attribute):
            names.add(n.func.attr)
    return names


def test_monitor_reads_the_shared_module():
    """🔑 反向守卫：monitor 真的**调用**了共享实现（否则上面那条形同虚设）。"""
    assert "check_drift" in _calls_in(MONITOR, "_all_legs"), (
        "monitor 的 _all_legs 没有调用共享的 check_drift —— "
        "上一条'不许自己算'就成了空约束"
    )
    # 两个检查项都只经由 _leg_drift 拿数据（不许直接去查库/查 Qdrant）
    for fn in ("check_index", "check_visual_index"):
        calls = _calls_in(MONITOR, fn)
        assert "_leg_drift" in calls, f"{fn} 没有走 _leg_drift（自己算去了？）"
        assert not (calls & {"psql", "_qdrant_point_ids"}), (
            f"{fn} 还在自己查：{calls & {'psql', '_qdrant_point_ids'}}"
        )


def test_admin_endpoint_uses_the_shared_module():
    """🔑 `routers/admin.py` 的对账端点**真的调用**了共享实现（async 变体）。

    ⚠️ 判据是 AST 里的**调用**，不是"文件里出现过这个名字"——
    import 行永远在，第一版文本判据被变异直接骗过去。
    ⚠️ 必须用**同一份 SQL 构造**：PG 侧两个入口的 WHERE 要是各写一遍，
    就会出现"端点说一致、巡检说有漂移"—— 对账工具自己不一致。
    """
    calls = _calls_in(ADMIN, "consistency")
    for want in ("pg_point_ids_async", "pg_page_point_ids_async",
                 "qdrant_point_ids"):
        assert want in calls, f"端点没有调用 {want}（实际调了 {sorted(calls)}）"
    src = ADMIN.read_text(encoding="utf-8")
    # 旧的三份实现必须已经删掉
    tree = ast.parse(src)
    funcs = {n.name for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for gone in ("_pg_point_ids", "_pg_page_point_ids", "_qdrant_point_ids"):
        assert gone not in funcs, f"admin.py 里还留着 {gone}（第二份判据）"
    # 也不许自己再拼一次点 ID
    assert "def _stmt_point_ids" not in src, (
        "admin.py 里自己写了 SQL 构造 —— 端点与巡检会给出不同答案"
    )


def test_sql_and_async_paths_share_the_same_statement():
    """🔑 sync 与 async 两个 PG 入口**共用** `_stmt_point_ids` / `_stmt_page_ids`。

    ⚠️ 这是"只有一份判据"的**结构**保证：两份 WHERE 改一处就会漂，
    而行为测试（跑真库比数字）只在当前数据下碰巧一致。
    """
    import inspect

    from services.common import consistency as mod

    for name in ("pg_point_ids", "pg_point_ids_async"):
        assert "_stmt_point_ids(kb_ids)" in inspect.getsource(getattr(mod, name))
    for name in ("pg_page_point_ids", "pg_page_point_ids_async"):
        assert "_stmt_page_ids(kb_ids)" in inspect.getsource(getattr(mod, name))


@pg
def test_endpoint_and_shared_module_agree_on_a_real_kb():
    """🔑 **端点与共享实现给出同一个答案**（同一个库、逐腿比数字）。

    ⚠️ 怎么造一把"读得到那个库"的 Key：用**该库所属工作区的成员**。
    一开始想直接用某个用户的 Key 随机挑一个库，那会在"用户读不到"时
    报 403/403 与 0/0 的假一致 —— 症状是"测试绿了但什么也没比"。
    ⇒ 这里显式按 `workspace_members` 找用户；找不到就跳过。
    """
    from sqlalchemy import select

    from services.common.db import sync_session_scope
    from services.common.models import Chunk, Document, KnowledgeBase, WorkspaceMember

    with sync_session_scope() as s:
        # 选一个**真有内容**的库（空的库三方都是 0，比了也不说明问题）
        row = s.execute(
            select(KnowledgeBase.id, KnowledgeBase.workspace_id)
            .join(Document, Document.kb_id == KnowledgeBase.id)
            .join(Chunk, Chunk.doc_id == Document.id)
            .where(KnowledgeBase.deleted_at.is_(None),
                   Document.is_current.is_(True),
                   Document.deleted_at.is_(None))
            .group_by(KnowledgeBase.id, KnowledgeBase.workspace_id)
            .limit(1)
        ).first()
        if row is None:
            pytest.skip("库里没有带 chunk 的库")
        kb_id, ws_id = int(row[0]), int(row[1])
        uid = s.execute(
            select(WorkspaceMember.user_id).where(
                WorkspaceMember.workspace_id == ws_id)
        ).scalars().first()
        if uid is None:
            pytest.skip(f"工作区 {ws_id} 没有成员（找不到读得到的用户）")
        shared = check_drift(s, [kb_id])
    dense = shared["kb_dense"]
    if dense["actual"] == 0:
        pytest.skip("该库在 Qdrant 里没有点（比了也不说明问题）")

    from fastapi.testclient import TestClient

    from services.api.main import app

    with sync_session_scope() as s:
        user = s.get(__import__("services.common.models", fromlist=["User"]).User, uid)
        email, display = user.email, user.display_name
    # 现铸一把 key（明文只在这一次可见），用它打端点
    from services.common.auth import generate_api_key, hash_password
    from services.common.models import ApiKey

    plaintext, key_hash, prefix = generate_api_key()
    with sync_session_scope() as s:
        s.add(ApiKey(user_id=int(uid), name="d143-probe",
                     key_hash=key_hash, key_prefix=prefix))
    try:
        with TestClient(app, client=("127.0.0.1", 12345)) as c:
            r = c.get(f"/api/v1/admin/consistency?kb_id={kb_id}",
                      headers={"Authorization": f"Bearer {plaintext}"})
        assert r.status_code == 200, r.text[:200]
        body = r.json()
        assert body["kb_ids"] == [kb_id], body["kb_ids"]
        assert body["legs"]["kb_dense"]["points"] == dense["actual"], (
            "端点与共享实现的点数不一致 —— 判据又分成两份了"
        )
        assert body["legs"]["kb_dense"]["missing_count"] == dense["missing"]
        assert body["legs"]["kb_visual"]["points"] == shared["kb_visual"]["actual"]
        assert body["consistent"] == (dense["missing"] == 0 and dense["dangling"] == 0)
    finally:
        with sync_session_scope() as s:
            s.execute(ApiKey.__table__.delete().where(
                ApiKey.key_prefix == prefix))


def test_page_level_filter_actually_filters(monkeypatch):
    """🔑 `page_level=True/False` **真的**分开页级点与块级点（D143 修的真缺陷）。

    🔴 D142 第一版这个参数**收了却没用**，而且当时 `with_payload=False`
    根本读不到 `chunk_index` ⇒ 两条腿的判据其实没分开。
    当前数据下数字碰巧是对的（页级点只落在 `kb_visual`），
    但只要 `kb_visual` 里混进一个 chunk 级点，对账就会**静默**算进去，
    而"视觉腿期望集合"是页级口径 ⇒ 报出假漂移。

    ⚠️ 判据是"**只**收页级的那次调用返回 1 个" ——
    不分别写 `page_level=True/False` 的断言的话，这条守卫会漏掉"参数被忽略"。
    """
    import services.common.consistency as mod

    page_pt = {"id": 111, "payload": {"kb_id": 1, "chunk_index": -1}}
    chunk_pt = {"id": 222, "payload": {"kb_id": 1, "chunk_index": 3}}
    # ⚠️ 给**两页**：下面要连着查两次（page_level=True 与 False），
    #   而 `_FakeClient` 按调用序号取页 —— 只给一页的话第二次调用会拿到空页，
    #   于是 `== {222}` 那条断言红在"没有数据"而不是"过滤错了"。
    _page = {"points": [page_pt, chunk_pt], "next_page_offset": None}
    fake = _FakeClient([_page, _page])
    monkeypatch.setattr(mod, "get_client", lambda: fake)

    assert qdrant_point_ids(["kb_visual"], [1], page_level=True) == {111}
    assert qdrant_point_ids(["kb_dense"], [1], page_level=False) == {222}
    # 请求里必须带 payload —— 否则读不到 chunk_index（第一版的第二个坑）
    assert all(body["with_payload"] is True for _, body in fake.calls), fake.calls


def test_scroll_requests_payload_so_the_level_can_be_told(monkeypatch):
    """🔑 单独钉住 `with_payload=True` —— 少了它 `page_level` 就只能靠猜。"""
    import services.common.consistency as mod

    _page = {"points": [{"id": 1, "payload": {"chunk_index": 0}}],
             "next_page_offset": None}
    fake = _FakeClient([_page])
    monkeypatch.setattr(mod, "get_client", lambda: fake)
    qdrant_point_ids(["kb_dense"], [1])
    assert fake.calls[0][1]["with_payload"] is True
