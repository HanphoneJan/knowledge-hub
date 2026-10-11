"""🔴 D142：PG ↔ Qdrant 的**定期**对账（M5 承诺了"定期对账任务修复漂移"）。

## 缺口

M5 那一行写的是「删除一致性 | 先删 Postgres → 异步清 Qdrant → **定期对账任务
修复漂移**」。实测：

| 环节 | 实际 |
|---|---|
| 先删 PG → 异步清 Qdrant | ✅ `pipeline.py` / `purge.py` |
| 对账**查询** | ✅ `GET /admin/consistency`（分腿）+ `deploy/monitor.py` |
| 对账**修复** | ✅ `POST /admin/reindex`（按需，手动触发）|
| 🔴 **定期**对账 | ❌ **不存在** —— worker 启动时只补扫"卡住的文档"（D127），之后只轮询任务 |

⇒ 漂移只能靠"人恰好跑了巡检"发现。而漂移**真实发生过**：
§5.70 实测过一次"每条腿 19733 个孤儿点"，那还是靠手动排查发现的。

## 为什么这里只**报告**、不自动删

⚠️ 自动删除"孤儿点"（Qdrant 有、PG 无）在**这个项目里不安全**，我查过写入顺序：

```python
session.add_all(chunk rows); session.flush()   # ← flush 不 commit！
...
upsert_points(COLL_DENSE, points)              # ← 点先于"行可见"
```

`flush()` 把 INSERT 发出去但**不提交**，所以在事务提交之前，**另一个连接**
（对账就是另一个连接）会看到"点存在、行不存在" ⇒ 那一刻的孤儿点是**正在写的**。
自动删 ⇒ 把刚写进去的点删掉，而行随后提交了 ⇒ **永久的索引空洞**，
比漂移更难修（漂移能重建，空洞要重新摄取）。

⇒ 本模块只**检测并告警**，修复仍然走 `POST /admin/reindex`（按库重建）。
"定期"这一条由 worker 的空闲循环满足（见 `worker/main.py`）。

## 三份实现的收口（⚠️ 已知的历史包袱）

对账逻辑在本项目里有**三份**拷贝：`routers/admin.py`（async，HTTP 契约面）、
`deploy/monitor.py`（直连 psql + Qdrant 客户端，**故意不走 API** —— 巡检账号
加不进别人的工作区）、以及本模块。三份都会漂，所以本模块是**新的唯一来源**，
worker 与巡检都用它；`admin.py` 的 async 版**暂时保留**（它要在请求作用域里
查，且是契约面），收敛它是一次独立改动（见 `PLAN.md` 的待办）。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from .models import Chunk, Document, KnowledgeBase
from .qdrant import get_client, point_id, point_id_for_page

logger = logging.getLogger(__name__)

COLL_DENSE = "kb_dense"
COLL_SPARSE = "kb_sparse"
COLL_VISUAL = "kb_visual"

#: 三条腿。🔑 **必须分腿**（D124）：取并集的话"只有一条腿坏了"会报成"一致"，
#: 而那正是真实故障模式（§5.44）。
LEGS = (COLL_DENSE, COLL_SPARSE, COLL_VISUAL)


def current_kb_ids(session) -> list[int]:  # noqa: ANN001
    """未软删的 KB id 列表。"""
    return list(session.execute(
        select(KnowledgeBase.id).where(KnowledgeBase.deleted_at.is_(None))
    ).scalars().all())


def _stmt_point_ids(kb_ids: list[int]):  # noqa: ANN202
    """PG 侧**块级**点 ID 的查询（sync / async **共用这一份判据**）。

    ⚠️ 只算 `is_current=TRUE AND deleted_at IS NULL` 的文档 —— 软删的文档
    **本来就不该**在索引里（D7：先删 PG，异步清 Qdrant）。把它们算进来会把
    "正在清理中"报成"漏索引"，然后有人去重建，把清理又推翻一遍（§5.43）。

    🔑 **kb_id 取自 `documents`，不要为了拿 kb_id 去 join `knowledge_bases`** ——
    那是一次笛卡尔积：实测期望点数被放大到 8379（真实值 399），
    于是"缺 8379 个点"这种巨大漂移会被当成真漂移报出来。症状极具欺骗性：
    SQLAlchemy 只发一条 `SAWarning`，而数字大到没人去怀疑它。

    ⚠️ 这份判据**只此一处**：HTTP 端点（`routers/admin.py`）也用它 ——
    两边各写一份 `WHERE` 的话，对账端点与巡检会给出不同答案，
    而"对账工具自己不一致"是最难查的一类问题（D140 刚为「两份清单」付过代价）。
    """
    return (
        select(Document.kb_id, Chunk.doc_id, Chunk.chunk_index)
        .join(Document, Document.id == Chunk.doc_id)
        .where(Document.kb_id.in_(kb_ids),
               Document.is_current.is_(True),
               Document.deleted_at.is_(None))
    )


def _stmt_page_ids(kb_ids: list[int]):  # noqa: ANN202
    """PG 侧**页级**（视觉腿）点 ID 的查询（sync / async 共用）。

    🔑 真相来源是 `documents.scan_pages`（D37）：`is_scanned: bool` 说不出页数，
    只有它能算出「该有几个点」。
    ⚠️ `scan_pages IS NULL` 的文档必须**跳过**（不能当成「应该有 0 个点」）：
    NULL 是「加这一列之前摄取的，页码无从得知」，当成空集会把它的页级点
    全报成残留 —— 那正是假漂移（与 `admin.py` 原注释语义一致）。
    """
    return (
        select(Document.kb_id, Document.id, Document.scan_pages)
        .where(Document.kb_id.in_(kb_ids),
               Document.is_current.is_(True),
               Document.deleted_at.is_(None),
               Document.scan_pages.isnot(None))
    )


def _page_ids(rows) -> set[int]:  # noqa: ANN001
    out: set[int] = set()
    for kb, doc, pages in rows:
        for page in pages or []:
            out.add(point_id_for_page(int(kb), int(doc), int(page)))
    return out


def pg_point_ids(session, kb_ids: list[int]) -> set[int]:  # noqa: ANN001
    """PG 侧期望有的**块级**点 ID（同步 session：worker / 巡检）。"""
    if not kb_ids:
        return set()
    return {point_id(int(k), int(d), int(i))
            for k, d, i in session.execute(_stmt_point_ids(kb_ids)).all()}


async def pg_point_ids_async(session, kb_ids: list[int]) -> set[int]:  # noqa: ANN001
    """同上，异步 session（`routers/admin.py` 用）。

    🔑 两个函数**共用 `_stmt_point_ids`**：SQLAlchemy 的 statement 与执行模式
    无关，差的只是 `execute` 要不要 `await` ⇒ **判据只写一遍**。
    """
    if not kb_ids:
        return set()
    rows = (await session.execute(_stmt_point_ids(kb_ids))).all()
    return {point_id(int(k), int(d), int(i)) for k, d, i in rows}


def pg_page_point_ids(session, kb_ids: list[int]) -> set[int]:  # noqa: ANN001
    """PG 侧期望有的**页级**点 ID（视觉腿，同步 session）。"""
    if not kb_ids:
        return set()
    return _page_ids(session.execute(_stmt_page_ids(kb_ids)).all())


async def pg_page_point_ids_async(session, kb_ids: list[int]) -> set[int]:  # noqa: ANN001
    """同上，异步 session（`routers/admin.py` 用）。"""
    if not kb_ids:
        return set()
    return _page_ids((await session.execute(_stmt_page_ids(kb_ids))).all())



def qdrant_point_ids(
    collections: list[str], kb_ids: list[int], *, page_level: bool = False
) -> set[int]:
    """Qdrant 侧实际有的点 ID（按 `kb_id ∈ kb_ids` 过滤）。

    ⚠️ 用**分页 scroll** 而不是 `search`：drift 报告要的是"全部点"，
    而 search 有 top-k 上限（几万点会被静默截断 —— 症状是"孤儿点是 0"，
    与 §5.69「对账什么都没查却报一致」同一族）。

    @param page_level `True` = **只**收页级点（`payload.chunk_index < 0`，
        视觉腿）；`False` = **只**收 chunk 级点。
        🔴 **D143 修**：这个参数第一版**收了却没用**（而且当时
        `with_payload=False`，根本读不到 `chunk_index`）⇒ 两条腿的判据其实
        没分开。当前数据下数字碰巧是对的（页级点只落在 `kb_visual`），
        但只要 `kb_visual` 里混进一个 chunk 级点，对账就会**静默**把它算进去，
        而"视觉腿期望集合"是页级口径 ⇒ 报出假漂移。
        判据沿用 `routers/admin.py` 原实现（`chunk_index < 0` = 页级）。
    """
    if not kb_ids:
        return set()
    client = get_client()
    out: set[int] = set()
    for coll in collections:
        offset = None
        while True:
            body: dict[str, Any] = {
                "filter": {"must": [{"key": "kb_id", "match": {"any": list(kb_ids)}}]},
                "limit": 4096,
                # 🔑 必须取 payload：要靠 `chunk_index < 0` 区分页级/块级
                "with_payload": True,
                "with_vector": False,
            }
            if offset is not None:
                body["offset"] = offset
            resp = client.post(f"/collections/{coll}/points/scroll", json=body)
            # 🔑 **必须查 status**：Qdrant 出错时返回的是
            #   `{"status":{"error":…}}`（没有 `result` 键），
            #   而下面那行 `.get("result") or {}` 会把它变成**空集合**
            #   ⇒ "实有 0 个点" ⇒ 报出"缺几万个点"的假漂移。
            #   症状：日志里一条巨大的漂移报告，而 Qdrant 其实好好的。
            if resp.status_code != 200:
                raise RuntimeError(
                    f"scroll {coll} 失败：HTTP {resp.status_code} {resp.text[:160]}"
                )
            data = resp.json()
            status = data.get("status")
            if status not in ("ok", {"status": "ok"}, None):
                raise RuntimeError(f"scroll {coll} 返回异常 status：{str(status)[:160]}")
            pts = ((data.get("result") or {}).get("points")) or []
            for p in pts:
                pid = p.get("id")
                if not isinstance(pid, int):
                    continue
                pl = p.get("payload") or {}
                try:
                    is_page = int(pl.get("chunk_index", 0)) < 0
                except (TypeError, ValueError):
                    is_page = False
                if is_page != page_level:
                    continue
                out.add(pid)
            offset = (data.get("result") or {}).get("next_page_offset")
            if offset is None:
                break
    return out


def check_drift(session, kb_ids: list[int] | None = None) -> dict[str, dict[str, Any]]:
    """逐腿对账。返回 `{腿: {expected, actual, missing, dangling}}`。

    ⚠️ `missing` 与 `dangling` 只给**数量与前若干个 id** —— 报告要能塞进一行日志，
    而几万条 id 打出来会把日志刷爆（真正要看全量时用 `GET /admin/consistency`）。
    """
    if kb_ids is None:
        kb_ids = current_kb_ids(session)
    expected = pg_point_ids(session, kb_ids)
    expected_pages = pg_page_point_ids(session, kb_ids)
    out: dict[str, dict[str, Any]] = {}
    for coll in LEGS:
        want = expected_pages if coll == COLL_VISUAL else expected
        got = qdrant_point_ids(
            [coll], kb_ids, page_level=(coll == COLL_VISUAL)
        )
        miss = want - got
        dang = got - want
        out[coll] = {
            "expected": len(want),
            "actual": len(got),
            "missing": len(miss),
            "dangling": len(dang),
            "missing_sample": sorted(miss)[:5],
            "dangling_sample": sorted(dang)[:5],
            "kb_ids": list(kb_ids),
        }
    return out


def drift_is_clean(drift: dict[str, dict[str, Any]]) -> bool:
    return all(d["missing"] == 0 and d["dangling"] == 0 for d in drift.values())


def format_drift(drift: dict[str, dict[str, Any]]) -> str:
    """一行摘要，给日志用。"""
    parts = [
        f"{coll}: 缺 {d['missing']}/多 {d['dangling']}"
        f"（期望 {d['expected']} · 实有 {d['actual']}）"
        for coll, d in drift.items()
    ]
    return " | ".join(parts)


def report_drift(kb_ids: list[int] | None = None) -> bool:
    """查一次并**记日志**；返回是否一致（供调用方决定要不要报警）。

    ⚠️ 不抛异常：漂移是**状态**不是故障，异常由调用方（worker 空闲循环）
    统一兜底。⚠️ **绝不删除**任何东西，理由见模块 docstring。
    """
    from .db import sync_session_scope

    with sync_session_scope() as s:
        drift = check_drift(s, kb_ids)
    if drift_is_clean(drift):
        logger.debug("索引对账：一致（%s）", format_drift(drift))
        return True
    logger.warning(
        "🔴 索引对账发现漂移：%s\n"
        "   这是**状态**不是故障；修复走 `POST /api/v1/admin/reindex?kb_id=`（按库重建），\n"
        "   或看 `GET /api/v1/admin/consistency?kb_id=` 取全量明细。\n"
        "   ⚠️ 本模块**故意不自动删点**：摄取时 chunk 行 flush 之后才 upsert 点，\n"
        "   而 flush 不提交 ⇒ 那个窗口里的「孤儿点」是正在写的，删了就成永久空洞。",
        format_drift(drift),
    )
    return False