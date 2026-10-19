"""文档状态的**终态判定**：把 `jobs` 的终态搬进 `documents.status`（D127）。

## 为什么要有这个模块

`jobs.status` 与 `documents.status` 是**两份**状态，而 D127 之前
**没有任何代码**把前者的终态搬到后者 —— 于是文档永远停在中间态：

| 任务终态 | 文档实际停在 | 用户看到 |
|---|---|---|
| `ingest` 重试耗尽 | `pending` / `parsing` / … | 「一直在处理中」 |
| `embed_visual` **全部**终态失败 | `pending` | 「一直在处理中」 |

而**下游全都已经准备好了**：

- 契约写着「解析时失败 → `doc.status=failed`」（`DESIGN-API.md` 的上传校验表）
- `POST /documents/{id}/retry` 明确只接受 `status == "failed"` 的文档
- 前端徽章与「重试」按钮都按 `status === 'failed'` 画

也就是说：**唯独那个状态从来不会被写进去** —— 端点在、契约有、测试过、
功能不成立（与 D125 那三条同一形状：文档说 ✅，代码没有）。
实测证据：开发库里 doc 4808（`photo.jpg`）停在 `pending`，
而它唯一的 `embed_visual` 任务 `attempts=3/3` 早已 `failed`。

## 判定要**两件事**都算进去

🔑 只看"任务状态"或只看"点数"都会判错：

- 只看点数（第一版的做法）→ 全失败时点数永远不够，于是**永远 pending**
- 只看任务状态 → 任务 done 只说明"没抛错"，这一页的点到底在不在索引里才是事实

所以终态判定是三个守卫 + 一个比较：

1. 文档还在**中间态**吗？（终态的不许动，包括 `failed`）
2. 还有**未完成**的 `ingest` / `embed_visual` 吗？（有就还没到终态）
3. 该有的页级点**齐了吗**？

⚠️ 第 2 条的 kind 白名单是刻意的：
`thumbnail` / `pdf_preview` **不**挡终态判定 —— 它们不影响"视觉页够不够"，
而把它们算进去会让"缩略图还没生成"把一份页级点已经齐了的文档永远按在 `pending`
（实测踩过一次：第一版把"任何未完成任务"都算进去，纯扫描件的缩略图任务
永远排在那儿，于是刚修好的 `ready` 翻转又被打回去了）。
反过来 `ingest` **必须**算进去：它会重算 `scan_pages` 并派发视觉任务，
它在排队就说明这份文档马上会被重新处理。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select

from ..common.jobs import KIND_EMBED_VISUAL, KIND_INGEST
from ..common.models import Document, Job

logger = logging.getLogger(__name__)

#: `documents.status` 里表示"还在干活"的值。**只有**这些状态会被本模块改写。
#: （`ready` / `failed` 都是终态 —— 尤其 `failed`：它可能是用户已经
#: 看过、并且决定不重试的结果，worker 不该替用户翻案。）
WORKING_STATUSES = ("pending", "parsing", "chunking", "embedding")

#: 哪些 kind 的未完成任务会挡住终态判定。见模块 docstring 末尾。
GATING_KINDS = (KIND_INGEST, KIND_EMBED_VISUAL)

#: `status_msg` 的长度上限。⚠️ 任务错误本身有 4000 字符（`finish_job` 的截断），
#:   而前端徽章只显示前 40 字、完整文本进 `title` 属性 ——
#:   一条 4000 字的 tooltip 比没有 tooltip 更糟。完整原因在 `/jobs` 里。
MAX_STATUS_MSG = 500

#: 消息里最多列几个失败的任务（再多就是"同一行字重复 N 次"）
MAX_LISTED_ERRORS = 3


@dataclass(frozen=True)
class JobView:
    """一条任务的**只读**视图。

    🔑 只取标量列，不用 ORM 实体：这里会在"任务终态"这个时刻被调用，
    而那正好是别人可能正在删库/删文档的时刻 —— 惰性关系在长任务里
    就是时间窗炸弹（PROGRESS §5.101 实测崩过）。
    """

    id: int
    kind: str
    status: str
    error: str | None = None
    page: int | None = None


@dataclass(frozen=True)
class Verdict:
    """终态判定的结果。

    `status is None` = **现在什么都不用改**（还没到终态，或者文档已经是终态）。
    `reason` 永远有一句人话 —— 它会进日志，运维看日志时不该再猜代码
    （§5.109："为让人判断而设计的返回值，如果没人用就等于没设计"）。
    """

    status: str | None
    message: str | None = None
    reason: str = ""


def doc_jobs(session, doc_id: int) -> list[JobView]:  # noqa: ANN001 — ORM Session
    """这份文档的全部任务，按 id 升序（"最后一条"= 最大的 id）。"""
    rows = (
        session.execute(
            select(Job.id, Job.kind, Job.status, Job.error, Job.payload)
            .where(Job.doc_id == doc_id)
            .order_by(Job.id)
        )
        .all()
    )
    return [
        JobView(
            id=r[0], kind=r[1], status=r[2], error=r[3],
            # ⚠️ `payload` 可能是 NULL（老数据），`.get` 会 AttributeError
            page=(r[4] or {}).get("page"),
        )
        for r in rows
    ]


def visual_point_count(doc_id: int) -> int:
    """`kb_visual` 里这份文档已有几个页级点。

    ⚠️ 判据是"点在不在索引里"而不是"任务有没有报错"：
    任务 done 只说明 handler 没抛异常，而点写没写进去是另一件事
    （`upsert_points` 抛错时任务会失败，可点也可能已经在了）。
    """
    from ..common.qdrant import COLL_VISUAL, get_client

    r = get_client().post(
        f"/collections/{COLL_VISUAL}/points/count",
        json={
            "exact": True,
            "filter": {"must": [{"key": "doc_id", "match": {"value": doc_id}}]},
        },
    )
    r.raise_for_status()
    return int(r.json()["result"]["count"])


def _clip(text: str | None, limit: int = MAX_STATUS_MSG) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _error_list(bad: Sequence[JobView]) -> str:
    """把失败/取消的任务列成一句人话（**有上限**，50 页全失败不能塞 50 行）。"""
    listed = [
        f"#{j.id} 第 {j.page} 页 {j.status}：{_clip(j.error, 160) or '无原因'}"
        if j.page else
        f"#{j.id} {j.status}：{_clip(j.error, 160) or '无原因'}"
        for j in bad[:MAX_LISTED_ERRORS]
    ]
    if len(bad) > MAX_LISTED_ERRORS:
        listed.append(f"…另有 {len(bad) - MAX_LISTED_ERRORS} 个失败任务")
    return "终态失败的任务：" + "；".join(listed) + "。" if listed else \
        "没有任何失败任务记录（可能是任务被删了）。"


def decide_doc_state(
    *,
    doc_status: str,
    is_deleted: bool,
    is_current: bool,
    scan_pages: Sequence[int] | None,
    jobs: Sequence[JobView],
    point_count: int | None,
) -> Verdict:
    """**纯函数**：这份文档现在该是什么状态。

    抽成纯函数是为了能不起 worker、不连 Qdrant 就单测每一条分支
    （§5.92："看起来对"的代码必须有判据）。
    """
    # ---- 守卫 1：只动还在干活的那一份 ----
    # ⚠️ `failed` 也在"不动"之列：它是**终态**，用户可能已经决定不重试。
    if is_deleted or not is_current:
        return Verdict(None, reason="文档已删除或已被覆盖，不动")
    if doc_status not in WORKING_STATUSES:
        return Verdict(None, reason=f"文档已是终态（{doc_status}），不动")

    # ---- 守卫 2：还有未完成的活就还没到终态 ----
    gating = [
        j for j in jobs
        if j.kind in GATING_KINDS and j.status in ("pending", "running")
    ]
    if gating:
        kinds = "/".join(sorted({j.kind for j in gating}))
        return Verdict(None, reason=f"还有 {len(gating)} 个未完成的 {kinds} 任务")

    # ---- 摄取任务本身终态失败 → failed（D38/D30/D37 承诺过的那个状态）----
    # 🔑 只看**最后一条** `ingest`：`reparse` / `retry` / 恢复都会新入队一条，
    #   而旧的失败任务不会消失（`enqueue` 的去重只比未完成的）。
    #   看"有没有失败过的 ingest"会把已经重新摄取成功的文档又打回 failed。
    ingests = [j for j in jobs if j.kind == KIND_INGEST]
    last_ingest = ingests[-1] if ingests else None
    if last_ingest is not None and last_ingest.status == "failed":
        return Verdict(
            "failed",
            f"摄取失败（任务 #{last_ingest.id}）："
            f"{_clip(last_ingest.error) or '任务没有给出原因'}",
            f"ingest 任务 #{last_ingest.id} 已终态失败",
        )

    # ---- 视觉腿：点数够了就 ready，不够就 failed（"两件事"都要算）----
    visual = [j for j in jobs if j.kind == KIND_EMBED_VISUAL]
    bad = [j for j in visual if j.status in ("failed", "cancelled")]
    expected = len(scan_pages or ())
    if expected <= 0:
        # ⚠️ `scan_pages` 为空有两种**含义不同**的情况，必须分开：
        #
        # · `[]`（解析过、确认这份没有扫描页）→ 视觉腿不适用 → 不判。
        #   到这里说明 `ingest` 成功了却没把它置 ready —— 那是**另一个** bug，
        #   在这里猜一个状态只会把症状藏起来（D125：端点在 ≠ 功能成立）。
        #
        # · `NULL`（**加这一列之前**摄取的）→ 页数无从得知，对账必须跳过
        #   （CONVENTIONS §2.6.1）。但**任务表仍然知道**哪些页本该被编码，
        #   而它们全部终态失败是一个**确定**的问题：那份 `pending` 承诺
        #   没人能兑现了 → `failed`，好让用户拿到「重解析」这个入口。
        #   （实测：开发库里就有一份 D124 之前摄取的照片停在这个状态。）
        #
        #   ⚠️ 这个分支**只判 failed、不判 ready**：老代码的入队去重不看
        #   payload（PROGRESS §5.89），3 页扫描件只入队过**一个**任务 ——
        #   "任务都 done"证明不了"页都建好了"。宁可停在 pending
        #   （monitor 会报出来），也不把一份不完整的索引说成完整。
        if not (scan_pages is None and bad):
            return Verdict(None, reason="不需要视觉索引，或页码未知且没有失败任务")
        return Verdict(
            "failed",
            "视觉索引未完成：这份文档没有页码清单（摄于页级索引之前），"
            "无法核对应有几个页级点，但视觉任务已全部终态失败。"
            + _error_list(bad) + "请点「重解析」重新摄取。",
            "视觉任务已终态失败，且页码清单缺失",
        )

    have = int(point_count or 0)
    if have >= expected:
        return Verdict("ready", None, f"页级点已齐（{have}/{expected}）")

    return Verdict(
        "failed",
        f"视觉索引未完成：应有 {expected} 个页级点，实际 {have} 个，"
        f"且没有任何未完成的视觉任务。{_error_list(bad)}"
        "这份文档的内容不会被检索到；可点「重试」重新摄取，"
        "或「重解析」重新读一遍源文件。",
        f"视觉任务已全部终态且点数不足（{have}/{expected}）",
    )


def reconcile_doc_state(session, doc_id: int) -> str | None:  # noqa: ANN001
    """对**一份**文档跑一次终态判定。返回新状态（或 `None` = 没改）。

    调用点只有 worker（每个 `ingest` / `embed_visual` 任务到达终态时），
    加上启动时的补扫（`sweep_stuck_docs`）。
    """
    doc = session.get(Document, doc_id)
    if doc is None:
        return None

    jobs = doc_jobs(session, doc_id)
    expected = len(doc.scan_pages or ())
    # ⚠️ 只在真的要判视觉腿时才查 Qdrant：批量导入照片时每个文档都会走到这里，
    #   而 `ingest` 失败那种情况下这个计数是白查的。
    points = visual_point_count(doc_id) if expected else None

    verdict = decide_doc_state(
        doc_status=doc.status,
        is_deleted=doc.deleted_at is not None,
        is_current=bool(doc.is_current),
        scan_pages=doc.scan_pages,
        jobs=jobs,
        point_count=points,
    )
    if verdict.status is None:
        logger.debug("文档 %s 状态不变：%s", doc_id, verdict.reason)
        return None

    was = doc.status
    doc.status = verdict.status
    # 🔑 转 `ready` 时必须清掉 `status_msg`：它是"上一次为什么失败"，
    #   留着会让一份已经好的文档带着失败原因（D25/`reparse` 都不清它）。
    doc.status_msg = verdict.message
    session.flush()
    logger.info(
        "文档 %s %s → %s：%s", doc_id, was, verdict.status, verdict.reason,
    )
    return verdict.status


def stuck_doc_ids(session) -> list[int]:  # noqa: ANN001
    """卡住的文档：**还在中间态**、却**没有任何未完成任务**。

    🔑 这份查询就是 `pending` 那个承诺的反面（PROGRESS §5.91：
    「`pending` 是一种承诺，谁入的队谁负责撤销它」）：
    没有待办任务还停在中间态 = 那个承诺永远不会被撤销。

    为什么要它：终态判定挂在"任务到达终态"这个时刻，而那次调用
    **有可能没跑完**（worker 在 `finish_job` 提交之后、判定提交之前被杀）。
    那个窗口只有几毫秒，但它一旦发生就是**永久**卡住，而症状与
    "根本没人写这个状态"完全一样 —— 从外面分不出来。
    启动时补扫一次就把它变成可自愈的。
    """
    pending_or_running = (Job.status == "pending") | (Job.status == "running")
    rows = (
        session.execute(
            select(Document.id)
            .where(
                Document.status.in_(WORKING_STATUSES),
                Document.deleted_at.is_(None),
                Document.is_current.is_(True),
                ~select(Job.id)
                .where(Job.doc_id == Document.id, pending_or_running)
                .exists(),
            )
            .order_by(Document.id)
        )
        .scalars()
        .all()
    )
    return [int(r) for r in rows]


def sweep_stuck_docs(session) -> list[tuple[int, str]]:  # noqa: ANN001
    """补扫：把卡住的文档结算掉。返回 `(doc_id, 新状态)` 列表。

    幂等：判据是"状态"，不是"有没有扫过"。
    """
    changed: list[tuple[int, str]] = []
    for doc_id in stuck_doc_ids(session):
        new = reconcile_doc_state(session, doc_id)
        if new is not None:
            changed.append((doc_id, new))
    return changed