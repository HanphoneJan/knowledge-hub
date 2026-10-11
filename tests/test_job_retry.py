"""🔑 D176：永久性错误**不重试**（`IngestError.retryable` 之前只写不读）。

## 缺口

`IngestError.retryable` 在 `services/ingest/pipeline.py` 里被 20+ 处设置，
并在 docstring 写明了理由（「文件超过 50MB 重试一万次也是 50MB」）——
但 **`grep '\\.retryable'` 只有赋值、从无读取**：worker 的 except 分支
（`worker/main.py`）一律 `finish_job(error=…)`，后者只按 `attempts < max_attempts`
重试 ⇒ blob 缺失、格式不支持、超 D30/D37 上限这类**永久错误**也被重试到 3 次。

## 这一组钉三件事

1. **语义**（`finish_job`）：`retryable=False` 时即使 `attempts < max_attempts`
   也直接终态 `failed`；`retryable=True` 时保持旧行为（按次数重试）。
2. **判据来源**（`is_retryable`）：默认 `True`（保守），只有异常显式声明
   `retryable=False` 才不重试 —— 用 `getattr` 而非 `isinstance`，避免
   `common → ingest` 的导入环。
3. **接线**（worker）：`run_once` 的失败路径必须用 `is_retryable(exc)` 算
   `retryable=`，而不是硬编码。这条用 AST 钉住（**变异验证过**：
   去掉 `retryable=` 或换成 `retryable=True` 都会红）。

⚠️ 行为断言在真 PG 上跑（自己造一行 `jobs` 再删），与 `test_health_queue.py` 同风格。
"""
from __future__ import annotations

import ast
import datetime as dt
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


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
# 1. 判据来源：is_retryable
# ============================================================


def test_is_retryable_defaults_true_for_unknown_errors():
    """任何普通异常都默认可重试（保守：宁可多试，不要漏掉瞬时的）。"""
    from services.common.jobs import is_retryable

    assert is_retryable(RuntimeError("网络抖动")) is True
    assert is_retryable(ValueError("随便")) is True


def test_is_retryable_honors_the_flag():
    """显式 `retryable=False` 的异常不重试；`True` 才重试。"""
    from services.common.jobs import is_retryable

    class Permanent(Exception):
        retryable = False

    class Transient(Exception):
        retryable = True

    assert is_retryable(Permanent()) is False
    assert is_retryable(Transient()) is True


def test_is_retryable_reads_the_real_ingest_error():
    """真 `IngestError` 的两种取值都被认出来（接线到真实类型）。"""
    from services.common.jobs import is_retryable
    from services.ingest.pipeline import IngestError

    assert is_retryable(IngestError("blob 缺失", retryable=False)) is False
    assert is_retryable(IngestError("限流", retryable=True)) is True
    # ⚠️ `IngestError.__init__` 的默认是 `retryable=False`（见 pipeline.py 签名）
    #    —— 即"没明说可重试，就默认不重试"。这与普通异常（默认 True）**不同**，
    #    是刻意的：摄取错误的常态是"数据/格式问题"，重试多半白费。
    assert is_retryable(IngestError("默认")) is False


# ============================================================
# 2. 语义：finish_job
# ============================================================


def _make_job(*, attempts: int, max_attempts: int = 3) -> int:
    from services.common.db import sync_session_scope
    from services.common.models import Job

    now = dt.datetime.now(dt.UTC)
    with sync_session_scope() as s:
        job = Job(
            kind="thumbnail", status="running", attempts=attempts,
            max_attempts=max_attempts, progress=0.4,
            locked_at=now, locked_by="pytest-d176",
            created_at=now, updated_at=now,
        )
        s.add(job)
        s.flush()
        return int(job.id)


def _status(job_id: int) -> tuple[str, int]:
    from services.common.db import sync_session_scope
    from services.common.models import Job

    with sync_session_scope() as s:
        j = s.get(Job, job_id)
        return j.status, j.attempts


def _delete(job_id: int) -> None:
    from services.common.db import sync_session_scope
    from services.common.models import Job

    with sync_session_scope() as s:
        s.execute(Job.__table__.delete().where(Job.id == job_id))


@pg
def test_permanent_error_goes_terminal_on_first_attempt():
    """🔑 `retryable=False` + 还有额度 ⇒ 仍然终态 failed（**不重试**）。"""
    from services.common.db import sync_session_scope
    from services.common.jobs import finish_job

    job_id = _make_job(attempts=1, max_attempts=3)
    try:
        with sync_session_scope() as s:
            finish_job(s, job_id, error="blob 缺失", retryable=False)
        status, attempts = _status(job_id)
        assert status == "failed", (
            f"永久错误被放回了队列：status={status}（应 failed，D176）"
        )
        assert attempts == 1, f"attempts 变了（{attempts}）—— 不该再占用重试额度"
    finally:
        _delete(job_id)


@pg
def test_transient_error_keeps_retrying_until_max_attempts():
    """`retryable=True` 保持旧行为：先回 pending，额度用尽才 failed。"""
    from services.common.db import sync_session_scope
    from services.common.jobs import finish_job
    from services.common.models import Job

    job_id = _make_job(attempts=1, max_attempts=3)
    try:
        with sync_session_scope() as s:
            finish_job(s, job_id, error="瞬时", retryable=True)
        assert _status(job_id)[0] == "pending", "瞬时错误应放回队列重试"

        # 把 attempts 推到上限 → 再失败就终态
        with sync_session_scope() as s:
            s.get(Job, job_id).attempts = 3
        with sync_session_scope() as s:
            finish_job(s, job_id, error="瞬时", retryable=True)
        assert _status(job_id)[0] == "failed", "额度用尽应终态 failed"
    finally:
        _delete(job_id)


# ============================================================
# 3. 接线：worker 真的用了 is_retryable
# ============================================================


def test_worker_passes_is_retryable_to_finish_job():
    """🔑 `run_once` 的失败路径必须传 `retryable=is_retryable(exc)`。

    **变异验证**：去掉 `retryable=` 关键字、或改成 `retryable=True`，
    这条都会红（后者的判据是"至少有一个 finish_job 的 retryable 值是
    `is_retryable(...)` 调用"）。
    """
    src = (ROOT / "services" / "worker" / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "finish_job"
    ]
    assert calls, "main.py 里找不到 finish_job 调用 —— 判据失效"
    retryable_kws = [kw for c in calls for kw in c.keywords if kw.arg == "retryable"]
    assert retryable_kws, (
        "run_once 的 finish_job 没有传 `retryable=` —— D176 的接线丢了"
    )
    assert any(
        isinstance(kw.value, ast.Call)
        and getattr(kw.value.func, "id", None) == "is_retryable"
        for kw in retryable_kws
    ), (
        "失败路径的 retryable 不是由 is_retryable(exc) 算出来的 —— "
        "永久错误又会被重试"
    )
