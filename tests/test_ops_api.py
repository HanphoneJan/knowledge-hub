"""🔑 运维/审计端点的租户口径（M8 + M9）。

这批端点的共同点：**它们比"读数据"更敏感**。

| 端点 | 敏感点 | 错了会怎样 |
|---|---|---|
| `GET /audit` | 里面有 query 摘要（用户的数据） | 跨租户可查 = 数据泄漏面 |
| `POST /admin/reindex` | 重建不是只读（会删 Qdrant 的点） | 越权全库重建 = 别人的库短暂搜不到 |
| `GET /workspaces/{id}/stats` | 磁盘用量能反推别人存了多少 | 轻度信息泄漏 |

测试不验"能不能查到"（那是 eval 的活），只验**范围怎么圈的**：
无权限与不存在同码、范围来自 `ctx.readable_kb_ids`、
可见性判定不重算（§0.6）。
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path

import pytest

from services.api.routers import admin as admin_router
from services.api.routers import audit as audit_router
from services.common.auth import UserContext
from services.common.errors import InvalidRequest, ResourceNotFound

ROOT = Path(__file__).resolve().parents[1]
_LOOP = asyncio.new_event_loop()


def run(coro):
    return _LOOP.run_until_complete(coro)


def teardown_module() -> None:
    _LOOP.close()


def _ctx(*kb_ids: int, user_id: int = 1) -> UserContext:
    return UserContext(
        user_id=user_id, display_name="u", email="u@t.local", api_key_id=None,
        readable_kb_ids=frozenset(kb_ids),
    )


class _Result:
    def __init__(self, rows=None, one=None, scalar=None) -> None:
        self._rows = rows or []
        self._one = one
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._one

    def one(self):
        return self._one

    def scalar_one(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class _Sess:
    """按调用顺序返回预置结果的假 session。"""

    def __init__(self, *results) -> None:
        self.results = list(results)
        self.statements = []
        self.commits = 0

    async def execute(self, stmt):
        self.statements.append(stmt)
        return self.results.pop(0) if self.results else _Result()

    async def commit(self):
        self.commits += 1


# ============================================================
# 1. /admin/reindex：范围来自 readable_kb_ids
# ============================================================


def test_reindex_targets_only_readable_kbs(monkeypatch):
    """⚠️ 重建不是只读操作 —— 越权的全库重建会让别人的库短暂搜不到东西。"""
    seen: list[dict] = []

    async def fake_enqueue(session, kind, **kw):
        seen.append({"kind": kind, **kw})
        return type("J", (), {"id": len(seen)})()

    monkeypatch.setattr(admin_router, "enqueue_async", fake_enqueue)
    out = run(admin_router.reindex(_ctx(5, 9), _Sess()))
    assert out.kb_ids == [5, 9]
    assert [s["kb_id"] for s in seen] == [5, 9]
    assert all(s["kind"] == "reindex" for s in seen), "kind 必须是 reindex"


def test_reindex_commits_before_returning(monkeypatch):
    """不提交 = 入队没落库 = worker 看不到（§5.28 第 3 例同源）。"""

    async def fake_enqueue(session, kind, **kw):
        return type("J", (), {"id": 1})()

    monkeypatch.setattr(admin_router, "enqueue_async", fake_enqueue)
    s = _Sess()
    run(admin_router.reindex(_ctx(5), s))
    assert s.commits == 1


def test_reindex_rejects_empty_scope():
    """一个库都没有 → 明确报错，而不是返回"重建了 0 个库"的 200。

    200 + 空列表会被脚本当成"重建完成"，而实际什么也没做。
    """
    with pytest.raises(InvalidRequest) as ei:
        run(admin_router.reindex(_ctx(), _Sess()))
    assert ei.value.status_code == 400


# ============================================================
# 2. /admin/consistency：两侧必须用同一个 point_id
# ============================================================


def test_consistency_uses_the_same_point_id_formula():
    """🔑 自己另写一份哈希公式 = 对账永远说"一致"。

    PG 侧与 Qdrant 侧必须用**同一个** `point_id(kb, doc, chunk)`。
    所以测试直接断言：admin 模块里没有第二份哈希实现。
    """
    # ⚠️ D143：这份判据搬到了 `common/consistency.py`（端点 / 巡检 / worker
    #   的定期对账现在共用**一份**），所以断言跟着搬 ——
    #   性质没变，变的只是它住在哪个文件。
    #   ⚠️ 别把它改回"在 admin.py 里"：那正是 D143 删掉的那份拷贝。
    for rel in ("services/common/consistency.py", "services/api/routers/admin.py"):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        mods: set[str] = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module:
                mods.add(n.module.split(".")[0])
            elif isinstance(n, ast.Import):
                mods.update(a.name.split(".")[0] for a in n.names)
        assert not (mods & {"hashlib", "blake2", "sha256", "mmh3"}), (
            f"{rel} 自己算哈希了 —— 必须用 common.qdrant.point_id：{mods}"
        )
    # 确实调用了 point_id（在唯一实现里）
    tree = ast.parse(
        (ROOT / "services" / "common" / "consistency.py").read_text(encoding="utf-8")
    )
    assert any(
        isinstance(n, ast.Call) and ast.unparse(n.func).endswith("point_id")
        for n in ast.walk(tree)
    ), "consistency.py 没有用 point_id"


def test_consistency_reconciles_all_three_legs():
    """🔑 三条腿都要对账 —— 少一条就等于那条腿坏了没人知道。

    D123 的教训：`kb_visual` 被排除在对账之外，于是它 8 个任务全 failed
    也没人报警，"0 个点"看起来像"库里没有照片"。所以现在
    `legs` 里有第三项，而它的键必须用**页级**公式算
    （`point_id_for_page`，不是 `point_id`）。
    """
    src = (ROOT / "services" / "api" / "routers" / "admin.py").read_text(
        encoding="utf-8"
    )
    assert "COLL_VISUAL" in src, "kb_visual 没有参与对账 —— 它坏了不会有人知道"
    assert "point_id_for_page" in src, "视觉腿用了 chunk 级公式算键（会报一堆假漂移）"
    # PG 侧的真相来源必须是 scan_pages（页码清单），而不是 is_scanned（布尔）
    assert "scan_pages" in src, "视觉腿的 PG 侧真相来源不是 documents.scan_pages"


def test_consistency_separates_page_level_from_chunk_level():
    """页级点（负 chunk_index）不能混进 chunk 级对账，反之亦然。

    两边的 PG 对应物不同（`chunks` 行 vs `documents.scan_pages`），
    混在一起比会**双向**报错：文本腿多出一堆页级点、视觉腿少一堆 chunk 点。
    """
    # ⚠️ D143：判据搬到 `common/consistency.py`（唯一一份），端点只调用它。
    src = (ROOT / "services" / "common" / "consistency.py").read_text(encoding="utf-8")
    assert 'chunk_index", 0)) < 0' in src or "chunk_index\", 0)) < 0" in src, (
        "对账没有按 chunk_index 的正负区分页级点"
    )
    assert "page_level=True" in (ROOT / "services" / "api" / "routers" / "admin.py"
                                 ).read_text(encoding="utf-8"), \
        "kb_visual 没有要求只收页级点"
    assert "with_payload" in src, (
        "读点时没取 payload —— 就没法按 chunk_index 分辨页级/块级"
    )
    # ⚠️ scan_pages IS NULL（加列之前摄取的文档）必须跳过 ——
    # 当成"应该有 0 个点"会把它已有的页级点全报成残留。
    assert "scan_pages.isnot(None)" in src or "scan_pages.is_not(None)" in src, (
        "scan_pages IS NULL 的文档没被跳过 —— 会报一堆假残留"
    )


def test_consistency_reports_ids_not_just_a_bool():
    """对账的价值全在"修哪几个"—— 只给布尔值等于没做。"""
    from services.common.schemas_ops import ConsistencyReport

    r = ConsistencyReport(
        kb_ids=[1], pg_chunks=10, qdrant_points=8,
        missing_in_qdrant=[111, 222], dangling_in_pg=[333],
        missing_count=2, dangling_count=1, consistent=False,
    )
    assert r.missing_in_qdrant == [111, 222]
    assert r.consistent is False
    # count 字段与列表长度可以不同（列表会截断到 200）
    r2 = ConsistencyReport(
        kb_ids=[1], pg_chunks=1000, qdrant_points=0,
        missing_in_qdrant=[1] * 200, missing_count=1000,
    )
    assert r2.missing_count == 1000 and len(r2.missing_in_qdrant) == 200


# ============================================================
# 3. /audit：工作区级鉴权 + 参数校验
# ============================================================


def test_audit_rejects_unknown_action():
    """拼错值静默返回空列表 = 骗人（用户会以为"这段时间没人用过"）。"""
    with pytest.raises(InvalidRequest) as ei:
        run(
            audit_router.query_audit(
                _ctx(), _Sess(), workspace_id=1, action="serach"
            )
        )
    assert ei.value.status_code == 400
    assert "serach" in ei.value.message


def test_audit_rejects_reversed_date_range():
    with pytest.raises(InvalidRequest):
        run(
            audit_router.query_audit(
                _ctx(),
                _Sess(),
                workspace_id=1,
                date_from=_dt(2026, 10, 3),
                date_to=_dt(2026, 1, 1),
            )
        )


def _dt(*args):
    import datetime as dt

    return dt.datetime(*args, tzinfo=dt.UTC)


def test_audit_missing_workspace_is_fail_closed():
    """不存在与无权限**同码** —— 审计端点尤其不能泄漏"有没有记录"。"""
    with pytest.raises(ResourceNotFound) as ei:
        run(audit_router.query_audit(_ctx(), _Sess(_Result(one=None)),
                                     workspace_id=999))
    assert (ei.value.status_code, ei.value.code) == (403, "KB_NOT_FOUND")


def test_audit_workspace_id_is_required():
    """不填 workspace_id 会退化成"查我能管的全部" —— 那要遍历所有 workspace。"""
    sig = inspect.signature(audit_router.query_audit)
    p = sig.parameters["workspace_id"]
    assert p.default is inspect.Parameter.empty, "workspace_id 必须必填"


def test_audit_scopes_by_kb_ids_array_overlap():
    """`audit_log.kb_ids` 是**数组**，跨库检索一条记录会有多个库 id。

    所以筛选必须是数组重叠（`&&`），不能是 `kb_id = :ws` ——
    后者会把绝大多数记录漏掉，然后用户以为"没人用过这个库"。
    """
    src = (ROOT / "services" / "api" / "routers" / "audit.py").read_text(
        encoding="utf-8"
    )
    assert "kb_ids.overlap" in src, "审计筛选没用数组重叠"


def test_audit_router_has_no_write_methods():
    """审计表只读。有写方法就意味着"审计可以被改" —— 那它就不叫审计了。"""
    from services.api.main import app

    paths = app.openapi()["paths"]
    methods = {m.upper() for p in paths if p == "/api/v1/audit" for m in paths[p]}
    assert methods == {"GET"}, f"/audit 上出现了非 GET：{methods}"


# ============================================================
# 4. /workspaces/{id}/stats：owner 限定 + 两个字节数
# ============================================================


def test_workspace_stats_requires_owner():
    """D4 只有 owner/member 两档 → 统计与审计一样给 owner（成员看不到用量）。"""
    with pytest.raises(ResourceNotFound) as ei:
        run(
            admin_router.workspace_stats(
                9, _ctx(), _Sess(_Result(one=type("W", (), {"id": 9, "owner_id": 2})()))
            )
        )
    assert ei.value.code == "KB_NOT_FOUND"


def test_workspace_stats_exposes_two_byte_numbers():
    """少一个字节数都会误导用户去清磁盘。"""
    from services.common.schemas_ops import WorkspaceStats

    fields = set(WorkspaceStats.model_fields)
    assert {"sum_doc_size_bytes", "blob_disk_bytes"} <= fields, fields


def test_disk_bytes_dedupes_shas(monkeypatch, tmp_path):
    """内容寻址下同一份内容只存一份 —— 逐库累加会重复计算。

    用户按"占用 2 倍"去清磁盘，会发现删了半天没腾出那么多空间。
    """
    (tmp_path / "aa").mkdir()
    (tmp_path / "bb").mkdir()
    (tmp_path / "aa" / "f.bin").write_bytes(b"x" * 100)
    (tmp_path / "bb" / "f.bin").write_bytes(b"x" * 100)
    monkeypatch.setattr(
        admin_router, "blob_dir", lambda sha: tmp_path / sha
    )
    # 同一个 sha 传两次 → 只算一次
    assert admin_router._disk_bytes(["aa", "aa"]) == 100
    assert admin_router._disk_bytes(["aa", "bb"]) == 200