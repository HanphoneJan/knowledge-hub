"""🔑 `/api/v1/jobs` 的租户口径（阶段 2）。

这个端点本身只有十几行，**它唯一可能出错的地方**是租户范围：
把两个条件写成 `or` 而不是 `and`，或者干脆漏掉 —— 而这两种错误
在单用户下**测不出来**（只有一个用户时隔离逻辑恒真，D54）。

所以这组测试不测"能不能查到任务"（那是 `eval/api_1c_check.py` 的活），
只测**生成的 SQL 到底长什么样**：用 stub session 捕获语句，
不需要 PG 在线 —— 快，且不受库里残留数据影响。

## 为什么用 stub session 而不是真连库

真库 + `run_paginated` 会把分页 count 一起跑掉，测出来的是"结果集"，
而我们要断言的是"**条件怎么拼的**"。`literal_binds` 编译出来的 SQL 字符串
就是这件事的直接证据（`test_permissions.py` 也是这个路子）。

真链路（上传 → worker 写 progress → 轮询到 1.0）在
`eval/api_1c_check.py` 里，那边有真实 worker 和真实 PDF。

## 为什么是同步函数 + 自己管 loop

同 `tests/test_retrieval_e2e.py`：anyio 的 pytest 插件**每个测试函数开新 loop**，
而 `get_async_engine()` 是模块级单例、连接池绑定创建它的 loop（PROGRESS §5.17）。
这里不用引擎，但保持同一个写法，别给后人引入第二种模式。
"""

from __future__ import annotations

import ast
import asyncio
import datetime as dt
import re
from pathlib import Path
from typing import Any

import pytest

from services.api.routers import jobs as jobs_router
from services.common.auth import UserContext
from services.common.errors import InvalidRequest, ResourceNotFound
from services.common.jobs import ALL_KINDS
from services.common.models import Job
from services.common.pagination import Page
from services.worker.handlers import HANDLERS

ROOT = Path(__file__).resolve().parents[1]

#: 唯一一个 event loop（§5.17）：模块级 = 全部用例共用 = 与 FastAPI 一致
_LOOP = asyncio.new_event_loop()


def run(coro):
    return _LOOP.run_until_complete(coro)


def teardown_module() -> None:
    _LOOP.close()


def _ctx(*kb_ids: int) -> UserContext:
    return UserContext(
        user_id=1,
        display_name="u",
        email="u@t.local",
        api_key_id=None,
        readable_kb_ids=frozenset(kb_ids),
    )


class _Result:
    """只提供 `scalar_one_or_none` 的假结果。"""

    def __init__(self, value: Any = None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _StubSession:
    """记录它收到的语句，并返回预置的假结果。

    `boom=True` 时任何查询都让测试失败 —— 用来证明"某些路径根本不该查库"
    （空授权集合返回空页；这条要是真去查库就会触发它）。
    """

    def __init__(self, value: Any = None, *, boom: bool = False) -> None:
        self.statements: list[Any] = []
        self._value = value
        self._boom = boom

    async def execute(self, stmt: Any) -> _Result:
        self.statements.append(stmt)
        if self._boom:
            raise AssertionError("这条路径不该查库（空授权集合 = 恒空）")
        return _Result(self._value)

    @property
    def sql(self) -> str:
        """最后一条语句的 SQL（去引号，便于断言）。"""
        from sqlalchemy.dialects import postgresql

        assert self.statements, "没有执行任何查询 —— 测试没测到东西"
        return str(
            self.statements[-1].compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        ).replace('"', "")


def _capture_pagination(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """把 `run_paginated` 换成记录版（返回空结果），返回录到的 stmt 列表。"""
    stmts: list[Any] = []

    async def fake_run_paginated(session, stmt, page, page_size, *, scalars=True):
        stmts.append(stmt)
        return [], 0

    monkeypatch.setattr(jobs_router, "run_paginated", fake_run_paginated)
    return stmts


def _pass_kb_access(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """把 `assert_kb_access` 换成放行版（记录被校验的 kb_id）。

    校验本身有专门的用例（`test_kb_id_param_propagates_fail_closed`）；
    这里要断言的是**端点自己拼的 SQL**，不该让真实校验去查 stub。
    """
    seen: list[int] = []

    async def ok(session, ctx, kb_id):
        seen.append(kb_id)

    monkeypatch.setattr(jobs_router, "assert_kb_access", ok)
    return seen


def _sql(stmt: Any) -> str:
    from sqlalchemy.dialects import postgresql

    return str(
        stmt.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    ).replace('"', "")


# ============================================================
# 1. 租户范围：只能来自 ctx.readable_kb_ids
# ============================================================


def test_list_scopes_to_readable_kb_ids(monkeypatch):
    """/jobs 只看 `ctx.readable_kb_ids` 里的库，且只用它。"""
    stmts = _capture_pagination(monkeypatch)
    page = run(jobs_router.list_jobs(_ctx(9, 5), _StubSession(), page=2, page_size=5))
    assert isinstance(page, Page)
    assert (page.page, page.page_size) == (2, 5)
    assert page.items == [] and page.total == 0
    assert "jobs.kb_id IN (5, 9)" in _sql(stmts[0])


def test_list_sql_has_no_or_at_top_level(monkeypatch):
    """🔑 条件是 `AND` 串起来的，不存在顶层 `OR`。

    复刻 CONVENTIONS §0.6 的**二次错误**：把带限定的分支"追加"成另一条
    `or`，限定完全无效。那次是 `visibility`，这次是 `kb_id` —— 同一类错误。
    """
    stmts = _capture_pagination(monkeypatch)
    _pass_kb_access(monkeypatch)
    run(jobs_router.list_jobs(_ctx(5, 9), _StubSession(), kb_id=5))
    sql = _sql(stmts[0])
    assert " OR " not in sql, f"顶层出现了 OR，限定可能被绕过：{sql}"
    assert "kb_id = 5" in sql
    assert "kb_id IN (5, 9)" in sql


def test_empty_authorized_set_returns_empty_without_querying(monkeypatch):
    """没有任何可见库 → 恒空，且**不查库**。

    不查库不只是省一次往返：SQLAlchemy 对 `IN ()` 的处理在各方言里
    不一致（PG 报语法错、MySQL 恒假），行为差异迟早会咬人。
    """
    stmts = _capture_pagination(monkeypatch)
    session = _StubSession(boom=True)
    page = run(jobs_router.list_jobs(_ctx(), session))
    assert (page.items, page.total) == ([], 0)
    assert session.statements == [] and stmts == []


def test_optional_filters_are_anded_in(monkeypatch):
    """四个可选条件同时给，SQL 里要全部出现（谁都不能把谁挤掉）。"""
    stmts = _capture_pagination(monkeypatch)
    _pass_kb_access(monkeypatch)
    run(
        jobs_router.list_jobs(
            _ctx(5, 9), _StubSession(), kb_id=5, doc_id=3, kind="ingest",
            job_status="running",
        )
    )
    sql = _sql(stmts[0])
    for frag in ("kb_id = 5", "kb_id IN (5, 9)", "doc_id = 3",
                 "kind = 'ingest'", "status = 'running'"):
        assert frag in sql, f"少了 {frag}：{sql}"
    assert " OR " not in sql


def test_kb_id_param_goes_through_assert_kb_access(monkeypatch):
    """`?kb_id=` 传了别人的库，要走 `assert_kb_access`（403 KB_NOT_FOUND）。

    判据是"补丁是否被调用"而不是真去建库 —— 真库路径由
    `eval/api_1b_check.py` 覆盖，这里只保证**端点没有绕过它**。
    """
    seen: list[int] = []

    async def fake_assert(session, ctx, kb_id):
        seen.append(kb_id)

    monkeypatch.setattr(jobs_router, "assert_kb_access", fake_assert)
    _capture_pagination(monkeypatch)
    run(jobs_router.list_jobs(_ctx(5), _StubSession(), kb_id=5))
    assert seen == [5]


def test_kb_id_param_propagates_fail_closed(monkeypatch):
    """`assert_kb_access` 抛的 `ResourceNotFound` 必须原样冒出去。"""

    async def deny(session, ctx, kb_id):
        raise ResourceNotFound(detail={"kb_id": kb_id})

    monkeypatch.setattr(jobs_router, "assert_kb_access", deny)
    _capture_pagination(monkeypatch)
    with pytest.raises(ResourceNotFound) as ei:
        run(jobs_router.list_jobs(_ctx(5), _StubSession(), kb_id=6))
    assert (ei.value.status_code, ei.value.code) == (403, "KB_NOT_FOUND")


# ============================================================
# 2. 参数校验（错值要 400，不是静默空结果）
# ============================================================


@pytest.mark.parametrize("kwargs", [{"kind": "不存在的kind"}, {"job_status": "半途而废"}])
def test_unknown_enum_values_are_rejected(monkeypatch, kwargs):
    """⚠️ 错值返回 400 而不是"空结果"。

    空结果的表现是"这个库没有任务"，用户会以为任务丢了，
    实际原因只是他打错了一个字。**失败要指向原因**。
    """
    _capture_pagination(monkeypatch)
    with pytest.raises(InvalidRequest) as ei:
        run(jobs_router.list_jobs(_ctx(5), _StubSession(), **kwargs))
    assert (ei.value.status_code, ei.value.code) == (400, "INVALID_REQUEST")


def test_every_declared_kind_and_status_passes_validation(monkeypatch):
    """合法值不能被自己拒掉（校验写反了的话，上面那条测试仍然全绿）。"""
    stmts = _capture_pagination(monkeypatch)
    for kind in ALL_KINDS:
        run(jobs_router.list_jobs(_ctx(5), _StubSession(), kind=kind))
    for status in jobs_router.JOB_STATUSES:
        run(jobs_router.list_jobs(_ctx(5), _StubSession(), job_status=status))
    assert len(stmts) == len(ALL_KINDS) + len(jobs_router.JOB_STATUSES)


# ============================================================
# 3. 单任务：无权限 ≡ 不存在
# ============================================================


def test_get_job_scopes_by_readable_kb_ids():
    session = _StubSession(None)
    with pytest.raises(ResourceNotFound):
        run(jobs_router.get_job(1, _ctx(5, 9), session))
    assert "jobs.kb_id IN (5, 9)" in session.sql


def test_missing_and_forbidden_share_one_error_code():
    """🔑 传不存在的 id 与传别人的 id，返回**完全相同**的 code/status。

    两者走的是同一条 SQL（`id = … AND kb_id IN (可见集)`），所以天然同码 ——
    这里是把性质钉住，防止将来有人为了"报错更友好"在前面加一个
    "先查存在性"的分支（那正是 §0.4 要堵的存在性探测）。
    """
    errs = []
    for _ in range(2):
        with pytest.raises(ResourceNotFound) as ei:
            run(jobs_router.get_job(999, _ctx(5), _StubSession(None)))
        errs.append((ei.value.code, ei.value.status_code))
    assert errs[0] == errs[1] == ("KB_NOT_FOUND", 403)


def test_visible_job_is_returned():
    now = dt.datetime(2026, 10, 3, 12, 0, tzinfo=dt.UTC)
    row = Job(
        id=7, kind="ingest", doc_id=3, kb_id=5, status="running",
        progress=0.42, attempts=1, payload={"page": 2},
        created_at=now, updated_at=now,
    )
    out = run(jobs_router.get_job(7, _ctx(5), _StubSession(row)))
    assert out.id == 7
    assert out.progress == pytest.approx(0.42)
    assert out.payload == {"page": 2}
    assert out.status == "running"


# ============================================================
# 4. 本模块只读（没有第三个入口能改任务状态）
# ============================================================


def test_router_is_read_only():
    """`/api/v1/jobs` 上只有 GET。

    加写操作前先想清楚：任务的产生与终结在 worker 侧，
    路由层多一条写路径就多一处"状态怎么算对"的问题。
    """
    from services.api.main import app

    paths = app.openapi()["paths"]
    methods = {m.upper() for p in paths if p.startswith("/api/v1/jobs") for m in paths[p]}
    assert methods == {"GET"}, f"/api/v1/jobs 上出现了非 GET 方法：{methods}"


# ============================================================
# 5. 三处登记（CONVENTIONS §3.3）—— 防 kind 漂移
# ============================================================


def _check_constraint(name: str) -> str:
    """从 `models.py` 的 AST 里取出某个 CheckConstraint 的 SQL 文本。

    用 AST 而不是读文件找字符串 —— 约束文本在源码里是**两段字符串拼接**
    （为了行宽），正则很容易只抓到一半。
    """
    src = (ROOT / "services" / "common" / "models.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == (
            "CheckConstraint"
        )):
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords}
        if not (isinstance(kwargs.get("name"), ast.Constant)
                and kwargs["name"].value == name):
            continue
        parts = [
            n.value for n in ast.walk(node.args[0])
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        ]
        return "".join(parts)
    pytest.fail(f"models.py 里找不到 CheckConstraint {name}")


def test_job_statuses_match_the_check_constraint():
    """`JOB_STATUSES` 必须与 `ck_job_status` 一致。

    不一致的后果：`/jobs?status=x` 对一个永远不可能出现的状态返回空列表，
    用户以为"没有这样的任务"，其实是端点在骗人。
    """
    assert tuple(re.findall(r"'([^']+)'", _check_constraint("ck_job_status"))) == (
        jobs_router.JOB_STATUSES
    )


def test_all_kinds_match_the_check_constraint():
    """`ALL_KINDS` 必须与 `ck_job_kind` 一致。

    不一致的后果分两种，**都很难查**：
    - 少登记：worker 报"未注册的任务类型"，任务永远失败
    - 多登记：INSERT 被 `CheckViolationError` 挡下，
      而 `alembic check` 照样说 "No new upgrade operations"（PROGRESS §5.26）
    """
    assert tuple(re.findall(r"'([^']+)'", _check_constraint("ck_job_kind"))) == ALL_KINDS


def test_every_kind_has_a_handler():
    """🔑 每个 kind 都必须有 handler。

    没有 handler 时 worker 遇到它只标失败**不崩**（见 `worker/main.py`），
    于是端点照常返回 202、任务永远跑不了 —— 用户完全不知道出了什么事。
    第 1 版就是那样：`import_chat`（D47）与 `pdf_preview`（D34 v1.1）
    两个 kind 缺 handler，症状是"功能写好了但完全没反应"
    （PROGRESS §5.49 / §5.57）。**现在所有 kind 都有 handler。**
    """
    missing = set(ALL_KINDS) - set(HANDLERS)
    assert missing == set(), (
        f"这些 kind 没有 handler：{sorted(missing)} —— "
        "端点还能入队（202），任务却永远跑不了；"
        "如果是你新写的 kind，记得三处登记（CONVENTIONS §3.3）"
    )