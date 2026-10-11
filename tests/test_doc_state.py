"""🔴 D127：文档状态**永远到不了 `failed`**（以及 `ready` 的另一半）。

## 这组测试锁的是哪两件事

| # | 曾经坏在哪 | 用什么钉住 |
|---|---|---|
| 1 | 视觉任务**全**终态失败 → 文档永远停在 `pending`（"一直在处理中"） | 纯判定函数 + 真实 PG 写入 |
| 2 | `ingest` 重试耗尽 → 文档停在 `parsing`/`pending`，`POST /retry` **永远 400** | 纯判定函数 + 真实 PG 写入 |
| 3 | 判定只挂在 `handle_visual` 的**成功**路径上（失败时那个 handler 走不到收尾） | AST：每个 `finish_job` 之后都要结算 |
| 4 | 结算只认"任务状态"或只认"点数"，两边都会判错 | 纯函数的每一条分支 |
| 5 | 缩略图任务在排队就把一份已经齐了的文档按在 `pending` | `thumbnail` 不许挡判定 |
| 6 | 重新摄取（`reparse`/`retry`）时旧失败任务把它又打回 `failed` | 只看**最后一条** `ingest` |

⚠️ 第 6 条实测踩过：`enqueue` 的去重只比未完成的任务，
所以"历史上失败过的 ingest"会一直躺在表里 —— 看"有没有失败过的任务"
会把重新摄取成功的文档打回 `failed`。

⚠️ 判据一律指向**性质**（"还没到终态就不许改"）而不是某个中间状态，
参见 PROGRESS §5.114（已经三次把对的实现判成失败）。
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from services.worker.doc_state import (
    GATING_KINDS,
    MAX_STATUS_MSG,
    WORKING_STATUSES,
    JobView,
    decide_doc_state,
)

ROOT = Path(__file__).resolve().parents[1]
WORKER_MAIN = ROOT / "services" / "worker" / "main.py"
HANDLERS = ROOT / "services" / "worker" / "handlers" / "__init__.py"


def _job(kind: str, status: str, *, id_: int = 1, error=None, page=None) -> JobView:
    return JobView(id=id_, kind=kind, status=status, error=error, page=page)


def _decide(jobs, *, scan_pages=(1, 2, 3), points=0, doc_status="pending",
            is_deleted=False, is_current=True):
    return decide_doc_state(
        doc_status=doc_status,
        is_deleted=is_deleted,
        is_current=is_current,
        scan_pages=scan_pages,
        jobs=jobs,
        point_count=points,
    )


# ============================================================
# 1. 视觉腿：终态判定要"两件事"都算（任务终态 + 点数）
# ============================================================


def test_all_visual_jobs_failed_marks_doc_failed():
    """🔑 全部视觉任务终态失败 + 点数不够 → `failed`，并说清哪个任务、什么错。

    这是 handoff 点名的那个症状：用户看到的是"一直在处理中"，永远不结束。
    """
    v = _decide(
        [
            _job("ingest", "done", id_=10),
            _job("embed_visual", "failed", id_=11, page=1,
                 error="IngestError: blob 缺失: /data/blobs/ab/abc/original"),
            _job("embed_visual", "failed", id_=12, page=2, error="同上"),
            _job("embed_visual", "failed", id_=13, page=3, error="同上"),
        ],
        points=0,
    )
    assert v.status == "failed", f"视觉任务全失败却判成 {v.status}：{v.reason}"
    # ⚠️ 判的是"消息里有没有那几件事"，不是某一句具体措辞（§5.114）
    assert "11" in v.message and "第 1 页" in v.message, v.message
    assert "blob 缺失" in v.message, f"消息里没有失败原因：{v.message}"


def test_partial_visual_failure_is_not_reported_as_ready():
    """🔑 缺一页也不能报 `ready` —— 那一页的内容永远不会被检索到。

    ⚠️ "任务都终态了"不等于"活干完了"：点数才是事实。
    只看任务状态的话，3 页里坏 1 页会被当成成功。
    """
    v = _decide(
        [
            _job("embed_visual", "done", id_=11, page=1),
            _job("embed_visual", "failed", id_=12, page=2, error="boom"),
            _job("embed_visual", "done", id_=13, page=3),
        ],
        points=2,
    )
    assert v.status == "failed", f"缺 1 个页级点却判成 {v.status}：{v.reason}"
    assert "2/3" in v.message or ("应有 3" in v.message and "实际 2" in v.message), v.message


def test_complete_visual_index_marks_doc_ready():
    """点数齐了 → `ready`，且**不留** `status_msg`。

    ⚠️ `status_msg` 是"上一次为什么失败"；一份已经好的文档带着
    失败原因，用户会在徽章的 tooltip 里读到一条假消息。
    """
    v = _decide([_job("embed_visual", "done", id_=11, page=1)], scan_pages=[1],
                points=1)
    assert v.status == "ready", v.reason
    assert v.message is None, f"ready 不该带失败原因：{v.message}"


def test_unfinished_visual_job_blocks_the_verdict():
    """还有任务在跑 → 什么都不改（既不 ready 也不 failed）。

    ⚠️ 这是"未到终态"那条性质：`failed` 会在**重试还有机会**时
    把一份正在正常处理的文档判成失败（`finish_job` 会把可重试的任务
    放回 `pending`，不是终态）。
    """
    v = _decide(
        [_job("embed_visual", "done", id_=11, page=1),
         _job("embed_visual", "running", id_=12, page=2)],
        points=1,
    )
    assert v.status is None, f"还有未完成任务却判成 {v.status}：{v.reason}"


def test_unfinished_thumbnail_does_not_block_the_verdict():
    """🔑 `thumbnail` / `pdf_preview` **不许**挡住终态判定。

    纯扫描件与缩略图任务是**同一个事务**里入队的，worker 按 created_at
    处理：视觉任务全 done 时缩略图常常还在排队。把它算进"未完成"
    的话，一份页级点已经齐了的文档会被永远按在 `pending` ——
    那正是 D124 刚修好的 `ready` 翻转又被打回去。
    """
    v = _decide(
        [
            _job("embed_visual", "failed", id_=11, page=1, error="boom"),
            _job("thumbnail", "running", id_=12),
            _job("pdf_preview", "pending", id_=13),
        ],
        scan_pages=[1],
        points=0,
    )
    assert v.status == "failed", (
        f"缩略图在排队把文档按在中间态（判成 {v.status}）：{v.reason}"
    )


def test_unfinished_ingest_blocks_the_verdict():
    """🔑 反过来：`ingest` **必须**挡住判定。

    `reparse` / `retry` / 恢复都会把文档置回 `pending` 并新入队一条
    `ingest`；它会重算 `scan_pages` 并派发视觉任务。它在排队就说明
    这份文档马上会被重新处理 —— 此刻按"页级点不够"判失败是错的。
    """
    v = _decide(
        [
            _job("ingest", "pending", id_=20),
            _job("embed_visual", "failed", id_=11, page=1, error="boom"),
        ],
        scan_pages=[1],
        points=0,
    )
    assert v.status is None, f"重摄取在排队却判成 {v.status}：{v.reason}"


# ============================================================
# 2. 摄取失败：`failed` 这个状态以前根本不可达
# ============================================================


def test_ingest_failure_marks_doc_failed_with_the_reason():
    """🔑 `ingest` 重试耗尽 → `documents.status = "failed"` + 原因。

    ⚠️ D127 之前**没有任何代码**写过 `documents.status = "failed"`
    （`grep '"failed"'` 只有 `jobs.py` 那处）。于是：
    - 契约承诺的「解析失败 → `doc.status=failed`」不成立
    - `POST /documents/{id}/retry`（只接受 `failed`）**永远 400**
    - 前端那个 `status === 'failed'` 的「重试」按钮永远不出现
    """
    v = _decide(
        [
            _job("ingest", "failed", id_=30,
                 error="IngestError: 扫描件 60 页，超过 D37 上限 50 页"),
            _job("thumbnail", "pending", id_=31),
        ],
    )
    assert v.status == "failed", f"摄取失败却判成 {v.status}：{v.reason}"
    assert "30" in v.message and "50 页" in v.message, v.message


def test_only_the_latest_ingest_job_counts():
    """🔑 只看**最后一条** `ingest`。

    ⚠️ `enqueue(dedup=True)` 只比未完成的任务（`pending`/`running`），
    所以历史上失败过的那条会一直躺在表里。看"有没有失败过的 ingest"
    会把**已经重新摄取成功**的文档又打回 `failed` ——
    而用户看到的是"我明明点了重试，为什么又坏了"。
    """
    v = _decide(
        [
            _job("ingest", "failed", id_=30, error="Too many pages"),
            _job("ingest", "done", id_=40),
            _job("embed_visual", "done", id_=41, page=1),
        ],
        scan_pages=[1],
        points=1,
    )
    assert v.status == "ready", (
        f"重摄取成功后仍被判成 {v.status}（旧的失败任务被当成了现状）：{v.reason}"
    )


def test_ingest_failure_beats_complete_point_count():
    """摄取失败时**不能**因为"页级点齐了"就报 `ready`。

    那份点属于**上一次**摄取的内容，而这次摄取失败了（可能 chunk 配置
    改过）。报 `ready` 等于宣称"库里是最新版本"，那是撒谎。
    """
    v = _decide(
        [
            _job("ingest", "failed", id_=30, error="boom"),
            _job("embed_visual", "done", id_=41, page=1),
        ],
        scan_pages=[1],
        points=1,
    )
    assert v.status == "failed", v.reason


# ============================================================
# 3. 守卫：只动"还在干活的那一份"
# ============================================================


@pytest.mark.parametrize("status", ["ready", "failed", "deleted"])
def test_terminal_doc_status_is_never_rewritten(status: str):
    """🔑 终态的文档**一律不动** —— 尤其 `failed`。

    `failed` 是用户可能已经看过、并且决定不重试的结果；
    worker 替他翻案（自动 ready / 自动重跑）比停在失败更坏。
    """
    jobs = [_job("embed_visual", "failed", id_=11, page=1, error="boom")]
    v = _decide(jobs, scan_pages=[1], points=0, doc_status=status)
    assert v.status is None, f"{status} 被改成了 {v.status}：{v.reason}"


def test_deleted_or_superseded_doc_is_never_rewritten():
    """回收站里的文档与被覆盖的旧版本都不许动。"""
    jobs = [_job("embed_visual", "failed", id_=11, page=1, error="boom")]
    for kw in ({"is_deleted": True}, {"is_current": False}):
        v = _decide(jobs, scan_pages=[1], points=0, **kw)
        assert v.status is None, f"{kw} 仍被改写：{v.reason}"


def test_docs_that_need_no_visual_index_get_no_verdict():
    """`scan_pages` 为空且**没有视觉任务** → 不判（本模块不许替 `ingest` 猜状态）。

    ⚠️ 到这一步说明"摄取成功却没把文档置 ready"，那是**另一个** bug。
    在这里猜一个状态只会把症状藏起来（D125：端点在 ≠ 功能成立）。
    """
    for pages in (None, [], ()):
        v = _decide([_job("ingest", "done", id_=10)], scan_pages=pages, points=0)
        assert v.status is None, f"scan_pages={pages} 却判成 {v.status}"


def test_unknown_page_list_with_failed_visual_jobs_is_failed():
    """🔑 `scan_pages IS NULL`（D124 之前摄取的）+ 视觉任务全失败 → `failed`。

    页数无从得知，所以**不能**说"应有 N 个点"；但任务表知道
    哪些页本该被编码，而它们全部终态失败是一个**确定**的问题 ——
    那份 `pending` 承诺没人能兑现了，用户需要一个「重解析」的入口。

    ⚠️ 真实数据：开发库里 doc 4808（`photo.jpg`）就是这一类，
    它在 D124 之前摄取、`scan_pages` 为 NULL、唯一的视觉任务
    `attempts=3/3` failed —— 停在"处理中"永远不结束。
    """
    v = _decide(
        [
            _job("ingest", "done", id_=10),
            _job("embed_visual", "failed", id_=11, page=1,
                 error="ValueError: You must specify exactly one of input_ids"),
        ],
        scan_pages=None,
        points=0,
    )
    assert v.status == "failed", f"页码未知但视觉任务全失败却判成 {v.status}"
    assert "页码清单" in v.message, v.message
    assert "11" in v.message and "input_ids" in v.message, v.message


def test_unknown_page_list_is_never_reported_as_ready():
    """🔑 页码未知时**绝不**报 `ready`，哪怕视觉任务全 done。

    ⚠️ 老代码的入队去重不看 payload（PROGRESS §5.89）：3 页扫描件只入队过
    **一个** `embed_visual`，它 done 了而第 2、3 页压根没被编码。
    "任务都 done"证明不了"页都建好了" —— 这个分支只判 failed。
    """
    v = _decide(
        [_job("ingest", "done", id_=10),
         _job("embed_visual", "done", id_=11, page=1)],
        scan_pages=None,
        points=1,
    )
    assert v.status is None, f"页码未知却判成 {v.status}：{v.reason}"


def test_status_msg_is_bounded():
    """🔑 `status_msg` 有长度上限 —— 前端把它整个塞进徽章的 `title`。

    任务错误本身有 4000 字符（`finish_job` 的截断），一条 4000 字的
    tooltip 比没有 tooltip 更糟。完整原因在 `/jobs` 里查。
    """
    v = _decide(
        [_job("embed_visual", "failed", id_=11, page=1, error="E" * 9000)],
        scan_pages=[1],
        points=0,
    )
    assert v.status == "failed"
    assert len(v.message) <= MAX_STATUS_MSG, f"status_msg 有 {len(v.message)} 字符"


def test_status_msg_lists_a_bounded_number_of_tasks():
    """50 页全失败时不要把 50 条错误都塞进 `status_msg`（一句"另有 47 个"就够）。"""
    jobs = [_job("embed_visual", "failed", id_=100 + i, page=i + 1, error="boom")
            for i in range(50)]
    v = _decide(jobs, scan_pages=list(range(1, 51)), points=0)
    assert v.status == "failed"
    assert "另有" in v.message, v.message
    assert len(v.message) <= MAX_STATUS_MSG


def test_gating_kinds_cover_both_index_legs():
    """🔑 挡判定的是 `ingest` 与 `embed_visual` 两个 kind。

    判据指向**集合的意图**（"会改变索引内容的那两类任务"）而不是
    具体常量值；新增 kind 时这条会提醒你回答"它要不要挡判定"。
    """
    assert set(GATING_KINDS) == {"ingest", "embed_visual"}


# ============================================================
# 4. 接线（§5.109 / §5.112：判据要落在**调用点**上）
# ============================================================


def _nested_bodies(stmt: ast.stmt):
    """`stmt` 内部**所有**的语句列表（函数体 / if 分支 / try 各段 / except 体…）。"""
    for field in ("body", "orelse", "finalbody"):
        val = getattr(stmt, field, None)
        if isinstance(val, list) and val and isinstance(val[0], ast.stmt):
            yield val
    for h in getattr(stmt, "handlers", None) or []:
        if isinstance(getattr(h, "body", None), list):
            yield h.body


def _finish_jobs_with_tail(fn_name: str) -> list[tuple[bool, list[ast.stmt]]]:
    """`(finish_job 是否带 error=, 它之后紧跟的语句)` 的清单。

    ⚠️ 必须**带着"之后"的上下文往下递归**：`finish_job` 常常写在
    `with sync_session_scope() as s:` 块里，那个块的最后一条就是它 ——
    只看本块的话会误判成"后面什么都没有"。
    """
    tree = ast.parse(WORKER_MAIN.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == fn_name)
    out: list[tuple[bool, list[ast.stmt]]] = []

    def walk(body: list[ast.stmt], after: list[ast.stmt]) -> None:
        for i, stmt in enumerate(body):
            tail = body[i + 1:] + after
            if (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
                    and getattr(stmt.value.func, "id", None) == "finish_job"):
                out.append(
                    (any(kw.arg == "error" for kw in stmt.value.keywords), tail)
                )
            for nested in _nested_bodies(stmt):
                walk(nested, tail)

    walk(fn.body, [])
    return out


def test_every_finish_job_is_followed_by_a_doc_state_settlement():
    """🔑 **每一个** `finish_job(...)` 之后都要结算一次文档状态。

    ⚠️ 为什么是"每一个"而不是"成功和失败各一次"：D127 之前判定只挂在
    `handle_visual` 的**成功**收尾上，而全失败时那个 handler 根本走不到
    收尾 —— 于是"任务失败了但文档说还在处理"永远不会被发现。
    把例外去掉（未注册 kind 的那一处也结算，`settle_doc_state` 自己会
    按 kind 短路）之后，这条不变量就没有例外可漏了。

    判据用 AST 数"配对"而不是字符串匹配，且看的是"**紧接着执行的那条
    语句**"：顺序反了会读到旧状态（`finish_job` 还没提交）。
    """
    pairs = _finish_jobs_with_tail("run_once")
    assert pairs, "run_once 里一个 finish_job 都没有（判据本身失效了）"
    for has_error, tail in pairs:
        where = "失败路径" if has_error else "成功路径"
        assert tail, f"{where}的 finish_job 之后没有任何语句"
        nxt = ast.unparse(tail[0])
        assert "settle_doc_state(" in nxt, (
            f"{where}的 finish_job 之后没有结算文档状态（下一条是：{nxt[:60]}）"
        )


def test_failure_path_settlement_is_wired_with_an_error_argument():
    """🔑 带 `error=` 的那条（失败路径）也必须结算。

    这是"同一个症状的两半"：成功不转 `ready` 是 D124 修的，
    失败不转 `failed` 是 D127 修的 —— 而后者只能在这里接线。
    """
    assert any(
        has_error and tail and "settle_doc_state(" in ast.unparse(tail[0])
        for has_error, tail in _finish_jobs_with_tail("run_once")
    ), "失败路径（finish_job(error=…)）后面没有结算文档状态"


def test_worker_sweeps_stuck_docs_on_startup():
    """🔑 启动时要补扫"停在中间态却没有任何未完成任务"的文档。

    ⚠️ 没有它的话，这个修复只对**将来**的失败生效：
    结算挂在"任务到达终态"那一刻，而 worker 完全可能在那之后被杀
    （毫秒级窗口，一旦发生就是永久卡住）；D127 之前留下的存量
    （实测开发库 doc 4808）也永远等不到任何一次判定。
    """
    src = WORKER_MAIN.read_text(encoding="utf-8")
    tree = ast.parse(src)
    main_fn = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "main")
    called = [n for n in ast.walk(main_fn)
              if isinstance(n, ast.Call)
              and getattr(n.func, "id", None) == "sweep_stuck_docs"]
    assert called, "main() 没有调用 sweep_stuck_docs（存量卡住的文档不会被结算）"


def test_visual_handler_no_longer_flips_doc_status():
    """🔑 文档状态的翻转只在一处实现（`services/worker/doc_state.py`）。

    ⚠️ 两处判据就会漂移：第一版在 `handle_visual` 收尾时判"点数够不够"，
    而它只看成功路径 —— 语义与真正的终态判定**不一样**。
    """
    tree = ast.parse(HANDLERS.read_text(encoding="utf-8"))
    offenders = [
        n.lineno for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(getattr(t, "attr", None) == "status"
                and getattr(getattr(t, "value", None), "id", None) == "doc"
                for t in n.targets)
    ]
    assert not offenders, (
        f"handlers/__init__.py:{offenders} 还在改 documents.status —— "
        "终态判定必须只有 doc_state.py 一处"
    )


# ============================================================
# 5. 真实 PG：写路径真的落库（不是只在内存里判对）
# ============================================================


def _pg_alive() -> bool:
    from services.common.db import sync_session_scope

    try:
        with sync_session_scope() as s:
            s.execute(select(1))
        return True
    except Exception:  # noqa: BLE001
        return False


pg = pytest.mark.skipif(not _pg_alive(), reason="Postgres 不在线")

#: 测试专用文档的文件名前缀。⚠️ 一来是标识，二来是用来**清掉上一次
#: 崩在中间的残留**（pytest 被 Ctrl-C 掉时 finally 不会跑）。
_MARK = "zz-doc-state-test-"


def _mk_doc(s, kb_id: int, sha: str, *, name: str, **fields):  # noqa: ANN001
    """造一份最小可用的文档行 + 它的任务行。返回 `(doc_id, job_ids)`。

    `_jobs_spec` 是 `[(kind, status, payload, error), …]`，**不进**文档行。
    """
    from services.common.models import Blob, Document, Job

    spec = fields.pop("_jobs_spec", ())
    s.add(Blob(sha256=sha, size_bytes=1, mime_type="application/pdf",
               storage_path=f"test/{sha}", ref_count=1))
    doc = Document(
        kb_id=kb_id, file_name=name, content_hash=sha, blob_sha=sha,
        mime_type="application/pdf", **fields,
    )
    s.add(doc)
    s.flush()
    jobs = []
    for kind, status, payload, error in spec:
        j = Job(kind=kind, doc_id=doc.id, kb_id=kb_id, status=status,
                payload=payload, error=error,
                attempts=3 if status == "failed" else 1)
        s.add(j)
        jobs.append(j)
    s.flush()
    return doc.id, [j.id for j in jobs]


def _cleanup(s) -> None:
    from sqlalchemy import delete

    from services.common.models import Blob, Document, Job

    shas = list(s.execute(
        select(Document.blob_sha).where(Document.file_name.like(f"{_MARK}%"))
    ).scalars().all())
    s.execute(delete(Job).where(Job.doc_id.in_(
        select(Document.id).where(Document.file_name.like(f"{_MARK}%"))
    )))
    s.execute(delete(Document).where(Document.file_name.like(f"{_MARK}%")))
    if shas:
        s.execute(delete(Blob).where(Blob.sha256.in_(shas)))


@pytest.fixture
def live_doc(monkeypatch):  # noqa: ANN001, ANN201
    """真实 PG 里的测试文档（自带清场）。

    ⚠️ 用 `monkeypatch` 换掉 `visual_point_count`：这里要验的是**写路径**
    与"卡住"的查询形状，而点数由纯函数的单测钉住。
    真去查 Qdrant 会让这个测试依赖 collection 的状态。
    """
    from services.common.db import sync_session_scope
    from services.common.models import KnowledgeBase

    holder: dict = {"points": 0}

    def _fake_points(_doc_id: int) -> int:
        return holder["points"]

    with sync_session_scope() as s:
        _cleanup(s)
        kb_id = s.execute(select(KnowledgeBase.id).limit(1)).scalar_one_or_none()
    if kb_id is None:
        pytest.skip("库里没有知识库")

    import services.worker.doc_state as mod

    monkeypatch.setattr(mod, "visual_point_count", _fake_points)

    created: list[str] = []

    def _make(**fields):  # noqa: ANN003, ANN201
        sha = uuid.uuid4().hex
        name = f"{_MARK}{len(created)}.pdf"
        created.append(name)
        with sync_session_scope() as s:
            doc_id, job_ids = _mk_doc(s, kb_id, sha, name=name, **fields)
        return doc_id, job_ids

    try:
        yield _make, holder
    finally:
        with sync_session_scope() as s:
            _cleanup(s)


@pg
def test_reconcile_writes_failed_status_to_postgres(live_doc, monkeypatch):  # noqa: ANN001
    """🔑 真实库里：视觉任务全 failed 的文档会被**写成** `failed` + 原因。

    这是 D127 的核心承诺。判据用**重开一个 session 读回来**，
    否则测的只是 ORM 对象在内存里被改过。
    """
    from services.common.db import sync_session_scope
    from services.common.models import Document
    from services.worker.doc_state import reconcile_doc_state

    make, _points = live_doc
    doc_id, job_ids = make(
        status="pending", scan_pages=[1, 2],
        _jobs_spec=[
            ("ingest", "done", None, None),
            ("embed_visual", "failed", {"page": 1}, "IngestError: blob 缺失"),
            ("embed_visual", "failed", {"page": 2}, "IngestError: blob 缺失"),
        ],
    )
    with sync_session_scope() as s:
        assert reconcile_doc_state(s, doc_id) == "failed"

    with sync_session_scope() as s:
        doc = s.get(Document, doc_id)
        assert doc.status == "failed", f"库里还是 {doc.status}"
        assert str(job_ids[1]) in (doc.status_msg or ""), doc.status_msg
        assert "blob 缺失" in (doc.status_msg or ""), doc.status_msg


@pg
def test_stuck_query_finds_docs_with_no_unfinished_jobs(live_doc):  # noqa: ANN001
    """🔑 补扫的判据是"**没有任何未完成任务**"，不是"有没有失败任务"。

    ⚠️ 漏了后半个条件就会在 worker 启动的瞬间把正在摄取的文档判成失败
    （worker 一启动，队列里排着几百个 `ingest`，它们都是 `pending`）。
    """
    from services.common.db import sync_session_scope
    from services.worker.doc_state import stuck_doc_ids

    make, _ = live_doc
    busy_id, _ = make(
        status="pending", scan_pages=[1],
        _jobs_spec=[("ingest", "pending", None, None)],
    )
    stuck_id, _ = make(
        status="pending", scan_pages=[1],
        _jobs_spec=[
            ("ingest", "done", None, None),
            ("embed_visual", "failed", {"page": 1}, "boom"),
        ],
    )
    done_id, _ = make(
        status="ready", scan_pages=[1],
        _jobs_spec=[("ingest", "done", None, None)],
    )
    with sync_session_scope() as s:
        ids = stuck_doc_ids(s)
    assert busy_id not in ids, "正在排队的 ingest 被当成卡住的文档"
    assert stuck_id in ids, "全部任务终态却没有待办的文档没被认出来"
    assert done_id not in ids, "已经是 ready 的文档不该出现在补扫名单里"


@pg
def test_sweep_is_idempotent(live_doc):  # noqa: ANN001
    """🔑 补扫跑两遍，第二遍什么都不做。

    判据是"状态"而不是"扫过没有"，所以重复执行天然安全 ——
    而 worker 每次重启都会扫一遍。
    """
    from services.common.db import sync_session_scope
    from services.common.models import Document
    from services.worker.doc_state import sweep_stuck_docs

    make, _ = live_doc
    doc_id, _ = make(
        status="pending", scan_pages=[1],
        _jobs_spec=[
            ("ingest", "done", None, None),
            ("embed_visual", "failed", {"page": 1}, "boom"),
        ],
    )
    with sync_session_scope() as s:
        first = sweep_stuck_docs(s)
    assert doc_id in [d for d, _ in first], f"第一次没结算：{first}"
    with sync_session_scope() as s:
        assert doc_id not in [d for d, _ in sweep_stuck_docs(s)]
        assert s.get(Document, doc_id).status == "failed"


@pg
def test_thumbnail_job_left_pending_does_not_hold_the_doc(live_doc):  # noqa: ANN001
    """🔑 真实库里重跑第 1 节那条性质（缩略图在排队不许挡住结算）。

    单测里那份是用 JobView 手搓的；这里证明**真实入队的缩略图任务**
    （`thumbnail` 是另一个 kind）同样不影响判定 ——
    判错的代价是刚修好的 `ready` 翻转被打回去。
    """
    from services.common.db import sync_session_scope
    from services.common.models import Document
    from services.worker.doc_state import reconcile_doc_state

    make, points = live_doc
    doc_id, _ = make(
        status="pending", scan_pages=[1],
        _jobs_spec=[
            ("ingest", "done", None, None),
            ("embed_visual", "done", {"page": 1}, None),
            ("thumbnail", "running", {"page_count": 1}, None),
        ],
    )
    points["points"] = 1
    with sync_session_scope() as s:
        assert reconcile_doc_state(s, doc_id) == "ready"
    with sync_session_scope() as s:
        assert s.get(Document, doc_id).status == "ready"


def test_working_statuses_cover_every_non_terminal_doc_state():
    """🔑 `WORKING_STATUSES` = `ck_doc_status` 里除 `ready`/`failed`/`deleted` 的全部。

    判据指向**集合之间的关系**（模型 CHECK 允许值 − 终态），
    不是写死三个字符串 —— 将来给 `documents.status` 加一个中间态
    （比如 "queued"），这条会立刻红，提醒你把它纳进来。
    """
    from services.common.models import Document

    allowed: set[str] = set()
    for c in Document.__table_args__:
        if getattr(c, "name", None) == "ck_doc_status":
            text = str(c.sqltext)
            for part in text.split("'")[1::2]:
                allowed.add(part)
    assert allowed == {"pending", "parsing", "chunking", "embedding", "ready",
                       "failed", "deleted"}, f"CHECK 约束变了：{allowed}"
    assert set(WORKING_STATUSES) == allowed - {"ready", "failed", "deleted"}