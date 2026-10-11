"""Worker 主循环（D14：Postgres 任务表 + 轮询）。

**为什么独立进程**（D50）：colSmol 编码实测 **15s/图**、吃满 12 核，
与 API 同进程会让用户在文档处理时提问长时间无响应。
（⚠️ 15s 是**常数**，与图片分辨率无关：colSmol 内部恒定切成 13 个
512×512 图块；跟 checkpoint 默认的 bf16 跑则是 68s，本机 CPU 没有
avx512_bf16。详见 `retrieval/pool.py::get_visual_model`。）

```
┌────────────┐   jobs 表    ┌──────────────┐
│ API 进程    │──入队──────▶│  PG          │
│ (轻量)      │◀─查进度────│              │
└────────────┘             └──────┬───────┘
                                  │ FOR UPDATE SKIP LOCKED
                            ┌─────▼──────┐
                            │ worker 进程 │  空闲时只轮询 PG，几乎不耗 CPU
                            │            │
                            └────────────┘
```

**空闲时不该烧 CPU**：轮询间隔随空闲程度递增（1s → 5s），
有任务时立刻回到 1s。
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import time
import traceback

from ..common.config import get_settings
from ..common.db import dispose_engines, sync_session_scope
from ..common.jobs import (
    KIND_EMBED_VISUAL,
    KIND_INGEST,
    claim_job,
    finish_job,
    heartbeat,
    is_retryable,
    job_stats,
    report_job,
)
from ..common.consistency import report_drift
from .doc_state import reconcile_doc_state, sweep_stuck_docs
from .handlers import HANDLERS, run_handler

logger = logging.getLogger(__name__)

#: 空闲轮询的间隔阶梯（秒）。有任务时用第一个值。
_IDLE_BACKOFF = (1.0, 2.0, 5.0)
#: 领取失败（DB 抖动）时的退避
_ERROR_BACKOFF = 5.0


def worker_id() -> str:
    """实例标识。多机部署时靠它区分是谁在跑（`jobs.locked_by`）。"""
    return f"{socket.gethostname()}:{os.getpid()}"


#: 非终态时 progress 的上限。🔑 `1.0` **只归 `finish_job` 所有**。
#:
#: ⚠️ 为什么（实测踩到，`eval/api_1c_check.py` 偶发红在
#:   「中间态的 progress < 1」）：每个 handler 结尾都调 `progress(1.0)`
#:   （11 处），而它走的是**独立事务**；`finish_job` 在**之后**另一个事务里
#:   才把 status 翻成 done。于是两次提交之间存在一个窗口：
#:   读到的样本是 **`running` + `progress=1.0`**。
#:
#:   那个窗口只有几毫秒，所以 200+ 次运行里偶发一次 —— 而"偶发红"比
#:   "稳定红"更贵：下一个人会以为索引坏了，去查一个**根本不存在**的问题。
#:
#: 🔑 语义上也该这样：**"100%" 是"完成了"的意思**，而"完成"由
#: `finish_job` 判定（含重试判定）。handler 说"我干完了"不等于
#: 任务真的完成 —— 中间还有 worker 的收尾与提交。
#: 封顶 0.999 而不是丢弃这次上报：进度必须**单调不减**
#: （`api_1c` 就在断言这一点），直接丢掉会看到 0.67 → 1.00 的倒退。
PROGRESS_NONTERMINAL_MAX = 0.999


def clamp_progress(p: float) -> float:
    """把 handler 上报的进度压到「非终态」区间。

    抽成纯函数是为了**能不起 worker 就单测**（§5.92）。
    """
    return min(p, PROGRESS_NONTERMINAL_MAX)


#: 这些 kind 的任务到达终态时，要顺手给**文档**也结算一次（D127）。
#:
#: 🔑 为什么只有这两个：`ingest` 决定文档有没有内容、`embed_visual` 决定
#:   纯扫描件的页级点够不够。`thumbnail` / `pdf_preview` 失败**不许**改文档状态
#:   （预览转换失败"不动文档状态"是 D34 的原话），而两个 `import_*` 的成败
#:   说的是"这一批导入"，里面每一份文档都有自己的 `ingest` 任务。
DOC_STATE_KINDS = (KIND_INGEST, KIND_EMBED_VISUAL)


def settle_doc_state(kind: str, doc_id: int | None) -> None:
    """任务到终态之后，给文档也结算一次（D127）。

    ⚠️ **必须在 `finish_job` 的事务之外**：任务终态已经落库了，
    如果把它和文档状态绑在同一个事务里，那么"改文档状态失败"会把
    **已经做完的活**一起回滚 —— 15s 的 colSmol 前向白跑，
    而且任务会被反复重试（每次都再花 15s）。
    代价是"任务成功但文档还是中间态"这个窗口：它由启动补扫
    （`sweep_stuck_docs`）与巡检兜底，不是无人看管。

    失败**不抛**：调用点在 worker 主循环的正中间，抛出去会被当成
    "任务失败"再走一遍 `finish_job(error=...)` —— 那会把一个 `done`
    的任务改回 `pending`/重试，是比原问题严重得多的副作用。
    所以只记 ERROR 日志，让巡检的"卡住的文档"那一项报出来。
    """
    if kind not in DOC_STATE_KINDS or doc_id is None:
        return
    try:
        with sync_session_scope() as s:
            reconcile_doc_state(s, doc_id)
    except Exception:  # noqa: BLE001 — 见上面：抛出去会污染任务状态
        logger.error(
            "结算文档 %s 的终态失败（任务 %s）：\n%s",
            doc_id, kind, traceback.format_exc(),
        )


def run_once() -> bool:
    """处理一个任务。返回 True 表示处理了（队列可能还有活）。"""
    wid = worker_id()
    with sync_session_scope() as s:
        job = claim_job(s, wid)
        if job is None:
            return False
        job_id, kind, doc_id, payload = job.id, job.kind, job.doc_id, job.payload or {}

    handler = HANDLERS.get(kind)
    if handler is None:
        with sync_session_scope() as s:
            # 🔑 D176：未注册的 kind **永远**不会成功，重试是纯浪费 ——
            #   直接终态 failed，不占用 max_attempts 的额度。
            finish_job(s, job_id, error=f"未注册的任务类型: {kind}", retryable=False)
        # 这一处也结算（`settle_doc_state` 自己按 kind 短路）：
        # 规矩是"**每一个** finish_job 之后都结算"，不给自己留例外。
        settle_doc_state(kind, doc_id)
        logger.error("未注册的任务类型: %s", kind)
        return True

    started = time.perf_counter()
    try:
        # ⚠️ 每个任务**独立事务**：失败时只回滚这个任务的操作，
        # 不影响已经提交的其他任务（比如上一个文档的索引）。
        with sync_session_scope() as s:
            def _progress(p: float) -> None:
                # 进度回报走独立事务 —— 它要在主任务仍在跑时就能被读到
                with sync_session_scope() as ps:
                    heartbeat(ps, job_id, clamp_progress(p))

            def _report(p: float | None = None, **counters) -> None:
                """进度 **+ 业务计数器** 一起上报（导入类任务用）。

                🔑 同样走独立事务：`processed_msgs` 写在主事务里的话，
                导入 1740 个文档要几分钟，期间 `/jobs` 上那个数会一直停在 0，
                直到任务结束才突然跳到终值 —— 那等于没有进度
                （契约 §8 就是靠这个字段让人看到进度的）。
                ⚠️ payload 用 `||` 浅合并，用户下发的参数不会丢。
                """
                with sync_session_scope() as ps:
                    report_job(ps, job_id, progress=p, payload=counters or None)

            run_handler(
                kind, s, doc_id=doc_id, payload=payload,
                progress=_progress, job_id=job_id, report=_report,
            )

        with sync_session_scope() as s:
            finish_job(s, job_id)
        settle_doc_state(kind, doc_id)
        logger.info(
            "[%s] 完成 doc=%s 用时 %.1fs", kind, doc_id, time.perf_counter() - started
        )
    except Exception as exc:  # noqa: BLE001 — worker 必须活着接下一个任务
        logger.error("[%s] 失败 doc=%s: %s", kind, doc_id, exc)
        logger.debug(traceback.format_exc())
        with sync_session_scope() as s:
            # 🔑 D176：`retryable=False` 的异常（`IngestError` 声明的永久错误：
            #   blob 缺失、格式不支持、超 D30/D37 上限…）直接终态 failed，
            #   不再重试到 `max_attempts`。
            finish_job(
                s, job_id,
                error=f"{type(exc).__name__}: {exc}",
                retryable=is_retryable(exc),
            )
        # 🔑 D127：**失败**这条路径同样要结算文档状态 ——
        #   全失败时没有任何 `handle_visual` 会跑到收尾，第一版就是只挂在
        #   成功路径上，于是文档永远停在 `pending`（"一直在处理中"）。
        settle_doc_state(kind, doc_id)
    return True


#: D142：定期索引对账的节奏。
#: 🔑 **首次延迟**比间隔长：worker 刚起来时预热模型、Qdrant 还没连上，
#:   那时报出来的"漂移"多半是"还没开始写"。
_DRIFT_FIRST_DELAY = 180.0
#: 间隔别太短 —— 一次对账要 scroll 三条腿的全量点（几万点级），
#: 而且**每次都有真的在写的窗口**（见 `common/consistency.py`）。
_DRIFT_INTERVAL = 900.0

#: D177：清理「非当前文档」向量点的节奏。比 D142 的对账勤一点 ——
#: 顶替 / 软删之后旧点应当**尽快**消失，否则巡检的「索引对账」会一直黄
#: （30 天），而"长期红的项等于没有项"。
_STALE_SWEEP_INTERVAL = 300.0
#: 首次延迟（等模型预热 / Qdrant 连上，别一起来就动点）。
_STALE_SWEEP_FIRST_DELAY = 120.0


def main() -> int:
    """worker 主循环。收到 SIGTERM/SIGINT 后优雅退出。"""
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
    )

    # 视觉模型要在 worker 里才允许加载（D50：API 进程别碰 colSmol 的 2.5GB）。
    # 🔑 `VDR_ENABLED=false` 时连开关都不开 —— `get_visual_model()` 抛的
    #   RuntimeError 会让每个 `embed_visual` 任务失败并重试 3 次，
    #   而正确做法是**根本不入队**（`ingest/queue_visual`，D124）。
    from ..retrieval.pool import enable_visual

    if not settings.vdr_enabled:
        logger.warning(
            "VDR_ENABLED=false：不加载 colSmol，照片/扫描件不建视觉索引"
            "（文本腿照常；/admin/consistency 的 kb_visual 腿会报缺）"
        )
    elif settings.visual_write_via_service:
        # 🔑 D178：写侧走远端常驻视觉服务 ⇒ 本 worker **不加载** colSmol。
        #   这是 SMB 多 worker 的前提：否则每个 worker 各占 2.65GB（4 个 ≈ +10.6GB）。
        logger.info(
            "视觉写侧走远端服务（VISUAL_WRITE_VIA_SERVICE=true，D178）："
            "本 worker 不加载 colSmol；服务 %s", settings.visual_service_url
        )
    else:
        enable_visual(True)

    stopping = {"flag": False}

    def _stop(signum, _frame) -> None:  # noqa: ANN001, ANN202
        logger.info("收到信号 %s，处理完当前任务后退出", signum)
        stopping["flag"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    wid = worker_id()
    logger.info("worker 启动: %s", wid)

    # 🔑 D127：补扫"卡住的文档"（停在中间态却没有任何未完成任务）。
    #   正常情况下这里什么都没有；它存在是为了让**已经卡住**的那些能被结算掉：
    #   ① worker 在 `finish_job` 提交之后、结算提交之前被杀（毫秒级窗口，
    #      但一旦发生就是永久卡住）
    #   ② D127 之前的版本留下的存量（实测开发库里就有一份 `photo.jpg`
    #      停在 `pending`，它的视觉任务 `attempts=3/3` 早已 failed）
    try:
        with sync_session_scope() as s:
            fixed = sweep_stuck_docs(s)
        if fixed:
            logger.warning(
                "补扫：结算了 %d 份卡住的文档 → %s",
                len(fixed), ", ".join(f"{d}={st}" for d, st in fixed[:10]),
            )
    except Exception:  # noqa: BLE001 — 补扫失败不该阻止 worker 启动
        logger.error("补扫卡住的文档失败（不阻止启动）：\n%s", traceback.format_exc())

    # 只预热文本模型；colSmol 等第一个视觉任务时才加载（D29 懒加载）
    try:
        from ..retrieval.pool import preload_core

        preload_core()
        logger.info("核心模型预热完成")
    except Exception as exc:  # noqa: BLE001
        logger.error("核心模型预热失败（不阻止启动）: %s", exc)

    idle_idx = 0
    next_drift_check = time.monotonic() + _DRIFT_FIRST_DELAY
    next_stale_sweep = time.monotonic() + _STALE_SWEEP_FIRST_DELAY
    while not stopping["flag"]:
        try:
            did_work = run_once()
        except Exception:  # noqa: BLE001 — DB 抖动不该让 worker 退出
            logger.error("任务循环异常:\n%s", traceback.format_exc())
            time.sleep(_ERROR_BACKOFF)
            continue

        if did_work:
            idle_idx = 0
            continue

        # 🔴 D142：**定期**索引对账（M5 承诺的"定期对账任务"此前不存在）。
        #   🔑 放在**空闲**分支：忙的时候别去查 —— 那时候漂移多半正在被
        #   正常的摄取/清理修掉，报出来只是噪音；而空闲时任何漂移都是**真的**。
        #   ⚠️ 只报告、**不自动修**（`common/consistency.py` 的 docstring 写了原因：
        #   摄取时行 flush 之后点才 upsert，而 flush 不提交 ⇒ 那个窗口里的
        #   "孤儿点"是正在写的，删了就变成永久空洞）。
        #   ⚠️ 失败不打断循环：对账是旁路监控，不是 worker 的职责。
        now = time.monotonic()
        if now >= next_drift_check:
            next_drift_check = now + _DRIFT_INTERVAL
            try:
                report_drift()
            except Exception:  # noqa: BLE001
                logger.warning("定期索引对账失败（不打断任务循环）：\n%s",
                               traceback.format_exc())

        # 🔴 D177：清「非当前文档」的向量点（顶替 / 软删之后旧点不该留 30 天）。
        #   ⚠️ 与上面那条对账**方向相反**：对账只报告（防删正在写的点造成空洞），
        #      而这里删的是**已提交为非当前**的整份点集 —— 它们永不再被检索，
        #      删了不产生空洞（见 `ingest/purge.py::remove_noncurrent_points`）。
        if now >= next_stale_sweep:
            next_stale_sweep = now + _STALE_SWEEP_INTERVAL
            try:
                from ..ingest.purge import remove_noncurrent_points

                with sync_session_scope() as s:
                    n = remove_noncurrent_points(s)
                if n:
                    logger.info("清理非当前文档的向量点：%s 份（D177）", n)
            except Exception:  # noqa: BLE001
                logger.warning("清理非当前向量点失败（不打断任务循环）：\n%s",
                               traceback.format_exc())

        # 空闲：递增退避，别空转烧 CPU
        time.sleep(_IDLE_BACKOFF[min(idle_idx, len(_IDLE_BACKOFF) - 1)])
        idle_idx = min(idle_idx + 1, len(_IDLE_BACKOFF) - 1)

    logger.info("worker 退出")
    dispose_engines()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
