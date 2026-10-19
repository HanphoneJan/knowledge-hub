"""任务队列（D14）：Postgres 任务表 + 轮询 worker。

选 Postgres 而不是 Redis/BullMQ 的理由：
1. 本机已有 PG，少一个要运维的组件
2. 任务与业务数据同库，`ON DELETE CASCADE` 能自动清掉文档的待处理任务
3. 需求规模是 5~20 人团队（D23），任务量峰值也就几百条，PG 完全够

**并发安全的关键**：`FOR UPDATE SKIP LOCKED`。没有它，多 worker 会抢到同一行。
见 `claim_job()`。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy import cast
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from .models import Job

# 任务类型
#: 文本腿摄取主流程。**刻意把 parse/chunk/embed 合并成一个任务** ——
#: 实测三步加起来不到 1 秒（解析 ~40ms/页、编码 88ms/chunk），
#: 拆开只是多三次事务，而且进度回传要跨三个任务拼装。
KIND_INGEST = "ingest"
#: 视觉腿：colSmol 单页 **15s**、2.5GB 内存，**必须**独立任务，
#: 否则会把整个 worker 进程占死。
#: 🔑 **一页一个任务**（`payload={"page": n}`）—— 所以入队去重必须看
#:   payload，见 `enqueue` 的说明（D124 实测踩到）。
KIND_EMBED_VISUAL = "embed_visual"
KIND_THUMBNAIL = "thumbnail"
KIND_PDF_PREVIEW = "pdf_preview"
KIND_CLEANUP = "cleanup"
KIND_IMPORT_CHAT = "import_chat"
#: 目录导入（D48）：与 chat 导入**分开**而不是合成一个 kind ——
#: 两者产出的 `doc_source` 不同（`chat_import` vs `photo_dir`），
#: 失败原因也不同（jsonl 坏了 vs 目录里没有图片），
#: 合在一起会让 `/jobs?kind=import` 的失败无法定位是哪一类。
KIND_IMPORT_DIR = "import_dir"
KIND_REINDEX = "reindex"

ALL_KINDS = (
    KIND_INGEST, KIND_EMBED_VISUAL, KIND_THUMBNAIL, KIND_PDF_PREVIEW,
    KIND_CLEANUP, KIND_IMPORT_CHAT, KIND_IMPORT_DIR, KIND_REINDEX,
)

#: 租约超时。超过这个时间没更新 locked_at 就认为持有者已死，可被重新领取。
#: 要 > 单个任务的最长耗时（colSmol 编码一页 15s，2000 页理论上要 8 小时 ——
#: 但 D37 把扫描件限到 50 页，所以视觉任务最长约 12 分钟）。
#: 实际上 worker 会周期性续租（heartbeat），这个值只是兜底。
LEASE_TIMEOUT = dt.timedelta(minutes=15)


def enqueue(
    session: Session,
    kind: str,
    *,
    doc_id: int | None = None,
    kb_id: int | None = None,
    payload: dict[str, Any] | None = None,
    dedup: bool = True,
) -> Job:
    """入队一个任务。**同步 session**（worker 用）。

    @param dedup True 时，若已有**未完成**的 `(doc_id, kind, payload)` 相同
        的任务则不重复入队。上传时同一文档可能被多个流程触发，
        不去重会重复跑解析。

    🔑 **去重必须看 `payload`**（D124 实测踩到）：视觉腿是**一页一个任务**
    （`payload={"page": n}`），第一版的去重只比 `(doc_id, kind)`，
    于是 3 页扫描件只入队了**一个** `embed_visual`（第 1 页）——
    第 2、3 页永远不会被编码，文档也就永远补不齐。
    ⚠️ 这个 bug 极隐蔽：任务状态全是 `done`、日志干净，
    只有"页数对不上"才看得出来，而页级索引当时根本没人在意。

    ⚠️ 同理不能用 `scalar_one_or_none()`：一页一任务之后，
    同一文档会同时有多条未完成的同类任务（那是**正常状态**），
    那个调用会抛 `MultipleResultsFound`。
    """
    if dedup and doc_id is not None:
        existing = _find_unfinished(session, doc_id, kind, payload)
        if existing is not None:
            return existing

    job = Job(
        kind=kind,
        doc_id=doc_id,
        kb_id=kb_id,
        payload=payload,
        status="pending",
        progress=0.0,
    )
    session.add(job)
    session.flush()
    return job


def _find_unfinished(
    session: Session, doc_id: int, kind: str, payload: dict[str, Any] | None
) -> Job | None:
    """找一条 `(doc_id, kind, payload)` 相同且未完成的任务。

    payload 在 Python 里比，不交给 SQL：JSONB 的相等比较在 NULL 上
    语义别扭（`payload = NULL` 恒为 NULL 而不是真），而这里 `None`
    与 `{}` 必须视为同一个（老数据 `payload` 可能是 `{}`）。
    """
    rows = (
        session.execute(
            select(Job).where(
                Job.doc_id == doc_id,
                Job.kind == kind,
                Job.status.in_(("pending", "running")),
            )
        )
        .scalars()
        .all()
    )
    want = payload or {}
    for row in rows:
        if (row.payload or {}) == want:
            return row
    return None



async def enqueue_async(
    session: AsyncSession,
    kind: str,
    *,
    doc_id: int | None = None,
    kb_id: int | None = None,
    payload: dict[str, Any] | None = None,
    dedup: bool = True,
) -> Job:
    """入队。**异步 session**（API 用）。

    ⚠️ 必须有独立的 async 版本 —— 在 `AsyncSession` 上调同步的 `enqueue()`
    会在 `session.execute(select(Job)...)` 处返回 coroutine，
    紧接着的 `.scalar_one_or_none()` 抛
    `AttributeError: 'coroutine' object has no attribute 'scalar_one_or_none'`。
    这个报错完全指不到真正原因（"session 类型用错了"），所以宁可多写一份。

    ⚠️ 去重语义必须与同步版**逐字一致**（`enqueue` 的说明）：
    同样按 `(doc_id, kind, payload)` 判重，且同样不能用
    `scalar_one_or_none()`（一页一个任务之后同一文档会同时有多条未完成任务）。
    """
    if dedup and doc_id is not None:
        # 🔑 `await` 不能少：漏了的话这里拿到的是 **coroutine 对象**，
        #    而 `if existing is not None` 对 coroutine 恒为真 ——
        #    于是每次上传都"去重成功"、**一个任务都不入队**，
        #    文档永远停在 pending 且日志干净（CONVENTIONS §0.9 那个坑的变种：
        #    不报错，只是静默什么都不做）。
        existing = await _find_unfinished_async(session, doc_id, kind, payload)
        if existing is not None:
            return existing

    job = Job(
        kind=kind,
        doc_id=doc_id,
        kb_id=kb_id,
        payload=payload,
        status="pending",
        progress=0.0,
    )
    session.add(job)
    await session.flush()
    return job


async def _find_unfinished_async(
    session: AsyncSession, doc_id: int, kind: str, payload: dict[str, Any] | None
) -> Job | None:
    """`_find_unfinished` 的异步版（同源，见那里的说明）。"""
    rows = (
        (
            await session.execute(
                select(Job).where(
                    Job.doc_id == doc_id,
                    Job.kind == kind,
                    Job.status.in_(("pending", "running")),
                )
            )
        )
        .scalars()
        .all()
    )
    want = payload or {}
    for row in rows:
        if (row.payload or {}) == want:
            return row
    return None



def claim_job(
    session: Session, worker_id: str, kinds: tuple[str, ...] | None = None
) -> Job | None:
    """领取一个待处理任务。无任务返回 None。

    **这是并发安全的关键**。`FOR UPDATE SKIP LOCKED` 让别的 worker 直接跳过
    已被锁定的行，不会阻塞等待，也不会拿到同一行。

    同时处理租约过期：worker 崩了以后任务不会永久卡在 running。
    """
    now = dt.datetime.now(dt.UTC)
    lease_cutoff = now - LEASE_TIMEOUT

    stmt = (
        select(Job)
        .where(
            Job.status == "pending",
            or_(
                Job.locked_at.is_(None),
                Job.locked_at < lease_cutoff,
            ),
        )
        .order_by(Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if kinds:
        stmt = stmt.where(Job.kind.in_(kinds))

    job = session.execute(stmt).scalar_one_or_none()
    if job is None:
        # pending 里没有，试试接管租约过期的 running
        job = session.execute(
            select(Job)
            .where(Job.status == "running", Job.locked_at < lease_cutoff)
            .order_by(Job.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).scalar_one_or_none()

    if job is None:
        return None

    job.status = "running"
    job.locked_at = now
    job.locked_by = worker_id
    job.attempts += 1
    job.updated_at = now
    session.flush()
    return job


def heartbeat(session: Session, job_id: int, progress: float | None = None) -> None:
    """长任务续租 + 上报进度。

    colSmol 编码单页要 15s，一个文档可能几百页 —— 不续租会被别的 worker
    抢走（然后两个 worker 同时编码同一页，浪费 2.5GB 内存）。
    """
    session.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(
            locked_at=dt.datetime.now(dt.UTC),
            progress=progress if progress is not None else Job.progress,
            updated_at=dt.datetime.now(dt.UTC),
        )
    )


def report_job(
    session: Session,
    job_id: int,
    *,
    progress: float | None = None,
    payload: dict | None = None,
) -> None:
    """进度 **+ 业务计数器** 一起上报（导入类任务用）。

    🔑 为什么 payload 的更新也要走**独立事务**：导入任务要跑很久
    （87k 条消息 / 几千张图），如果 payload 只在任务结束的主事务里提交，
    用户在 `/jobs` 上看到的 `processed_msgs` 会**一直停在 0**，
    直到任务结束才突然跳到最终值 —— 那等于没有进度。
    `heartbeat` 已经为进度回传开了这条路，计数器搭同一班车。

    ⚠️ payload 用 `||` **浅合并**而不是覆盖：worker 的入口 payload
    里带着用户下发的参数（`source_path` / `recursive` 等），
    覆盖掉它们会让"这个任务到底在导什么"在 `/jobs` 上查不到。
    """
    values: dict = {"updated_at": dt.datetime.now(dt.UTC)}
    if progress is not None:
        values["progress"] = progress
    if payload:
        # ⚠️ JSONB 的 `||` 需要右侧也是 JSONB —— 用 `cast(:p, JSONB)` 显式转换，
        #   否则 psycopg 传 dict 会被当成 text 拼接，SQLite/PG 上行为还不一样
        values["payload"] = func.coalesce(
            Job.payload, cast("{}", JSONB)
        ).op("||")(cast(payload, JSONB))
    session.execute(
        update(Job).where(Job.id == job_id).values(**values)
    )


def is_retryable(exc: BaseException) -> bool:
    """这个异常值不值得重试（D176）。

    `IngestError.retryable` 说了算（`ingest/pipeline.py`）—— 它区分
    「瞬时错误」（网络抖动、向量库暂时不可用）与「永久错误」
    （blob 缺失、格式不支持、超过 D30/D37 上限）：后者重试一万次也不会成功。

    🔑 **默认 `True` 是保守方向**：把"其实永久"的错误多试两次，代价有界
    （≤ `max_attempts`）；反过来把"其实瞬时"的错误当永久，则要用户手动重试
    —— 那更糟。所以只有**明确声明** `retryable=False` 的异常才不重试。

    ⚠️ 用 `getattr` 而不是 `isinstance(IngestError)`：`common` **不能** import
    `ingest`（依赖方向：`ingest → common`，反过来就是环）。这样将来任何
    带 `retryable` 属性的异常都自动生效，不必再改这里。
    """
    return bool(getattr(exc, "retryable", True))


def finish_job(
    session: Session,
    job_id: int,
    *,
    error: str | None = None,
    retryable: bool = True,
) -> None:
    """标记完成或失败。失败会重置为 pending（还有重试次数）或终态 failed。

    @param retryable False = **永久错误**：不再占用重试次数，直接置终态
        `failed`（D176）。调用方用 `is_retryable(exc)` 算它；默认 True
        保持"按次数重试"的旧行为。
    """
    job = session.get(Job, job_id)
    if job is None:
        return
    if error is None:
        job.status = "done"
        job.progress = 1.0
        job.error = None
    else:
        job.error = error[:4000]
        if retryable and job.attempts < job.max_attempts:
            # 还有重试机会 → 放回队列，延迟重试由 worker 的 backoff 处理
            job.status = "pending"
            job.locked_at = None
            job.locked_by = None
        else:
            job.status = "failed"
    job.updated_at = dt.datetime.now(dt.UTC)
    session.flush()


def cancel_jobs_for_doc(session: Session, doc_id: int) -> int:
    """文档被删除/覆盖时，取消它未完成的任务。返回取消数量。"""
    res = session.execute(
        update(Job)
        .where(Job.doc_id == doc_id, Job.status.in_(("pending", "running")))
        .values(status="cancelled", updated_at=dt.datetime.now(dt.UTC))
    )
    return res.rowcount or 0


def job_stats(session: Session) -> dict[str, int]:
    """按状态统计任务数，给 `/health` 和运维页用。"""
    rows = session.execute(
        select(Job.status, func.count()).group_by(Job.status)
    ).all()
    return {status: count for status, count in rows}
