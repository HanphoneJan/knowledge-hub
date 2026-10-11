"""🔑 时间线与照片墙（D20）—— **特定 KB 的专属视图**。

## 五个端点的口径

| 端点 | 参数 | 数据源 |
|---|---|---|
| `GET /kb/{id}/timeline?month=YYYY-MM` | 月视图 | `chat_day_stats` 按月聚合 |
| `GET /kb/{id}/timeline?date=YYYY-MM-DD` | 当天明细 | 单行 `chat_day_stats` + 当天照片 |
| `GET /kb/{id}/timeline/anniversaries` | 纪念日 | 有照片的日子（按照片数排） |
| `GET /kb/{id}/timeline/milestones?limit=` | 里程碑 | 消息量突出的日子 |
| `GET /kb/{id}/photos?page=` | 照片墙 | `documents.taken_at` 有值的图片文档 |

## 🔑 只对 `source_kind ∈ {chat, photo}` 有内容（契约 §6）

其它库返回 **400**，而不是返回空列表。理由：在一个"我的文档"库里点时间线，
返回空会让用户以为"这段时间没有记录"，而真相是"这个视图本来就不适用"。
**空 ≠ 不适用**，两者必须能被区分。

## 租户过滤

照片墙走 `assert_kb_access`（与其它端点同源，§0.6），然后只用
`kb_id = :kb_id` 取数据 —— **不在这里重算可见性**。
`taken_at IS NULL` 的照片在照片墙里**不出现**（见 `timeline.py` 的
"不猜"原则），但 `/kb/{id}/documents` 仍然列得出来。
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from ...common.auth import assert_kb_access
from ...common.deps import CurrentUser, DbSession
from ...common.errors import InvalidRequest
from ...common.models import ChatDayStat, Document, KnowledgeBase
from ...common.pagination import Page
from ...common.schemas_ops import (
    AnniversaryOut,
    MilestoneOut,
    MonthDayOut,
    PhotoOut,
    TimelineDayOut,
    TimelineMonthOut,
)

router = APIRouter(tags=["timeline"])

#: 契约 §6 允许的库类型
VIEW_SOURCE_KINDS = ("chat", "photo")

#: 月视图最多返回多少个月（超了要翻页；不设限的话两年数据一次返回几百 KB）
MAX_MONTHS = 60
#: 单日的 highlights 上限（与 `timeline.MAX_HIGHLIGHTS` 一致，这里是响应侧）
MAX_HIGHLIGHTS_OUT = 3


async def _require_view_kb(
    session: DbSession, ctx: CurrentUser, kb_id: int
) -> KnowledgeBase:
    """鉴权 + 校验这是"能看时间线"的库（契约 §6）。"""
    await assert_kb_access(session, ctx, kb_id)
    kb = (
        await session.execute(
            select(KnowledgeBase).where(KnowledgeBase.id == kb_id)
        )
    ).scalar_one_or_none()
    if kb is None:
        # 理论上 assert_kb_access 已挡住
        raise InvalidRequest(f"知识库 {kb_id} 不存在")
    if kb.source_kind not in VIEW_SOURCE_KINDS:
        raise InvalidRequest(
            f"时间线视图只对 source_kind ∈ {list(VIEW_SOURCE_KINDS)} 的库有意义"
            f"（当前 {kb.source_kind}）。返回空列表会让人误以为"
            f"「这段时间没有记录」——那是不适用，不是没有。"
        )
    return kb


@router.get(
    "/kb/{kb_id}/timeline",
    response_model=TimelineMonthOut | TimelineDayOut,
    summary="时间线（`?month=YYYY-MM` 月视图 / `?date=YYYY-MM-DD` 当天明细）",
)
async def timeline(
    kb_id: int,
    ctx: CurrentUser,
    session: DbSession,
    month: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
    date: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
) -> TimelineMonthOut | TimelineDayOut:
    """⚠️ `month` 与 `date` **互斥**且必须给一个。

    两个都给会返回哪一个？不说清的话前端会"两个都带、拿到月视图、
    当成当天明细渲染"，显示成空白页。所以这里明确报 400。
    """
    await _require_view_kb(session, ctx, kb_id)
    if bool(month) == bool(date):
        raise InvalidRequest("要带 ?month=YYYY-MM 或 ?date=YYYY-MM-DD 其中之一")
    return (
        await _month_view(session, kb_id, month)
        if month
        else await _day_view(session, kb_id, date)
    )


async def _month_view(
    session: DbSession, kb_id: int, month: str
) -> TimelineMonthOut:
    """月视图：**逐月**聚合（不是把月内每天原样返回）。

    ⚠️ 为什么要逐月聚合：`timeline_daily` 有 697 天，直接返回是几百 KB；
    而月视图的用途是"这个月发生了什么"，需要的恰恰是**月内合计**。
    返回 `days` 里只放**有照片或消息量突出的几天**，其余用月合计表达。
    """
    y, m = (int(x) for x in month.split("-"))
    first = dt.date(y, m, 1)
    last = dt.date(y + (m == 12), (m % 12) + 1, 1)   # 下月 1 号（12 月跨年）

    rows = (
        await session.execute(
            select(ChatDayStat).where(
                ChatDayStat.kb_id == kb_id,
                ChatDayStat.day >= first,
                ChatDayStat.day < last,
            ).order_by(ChatDayStat.day)
        )
    ).scalars().all()

    # 合并该月的照片（照片在 photo 库里，与聊天聚合不同表）
    photos = await _photos_between(session, kb_id, first, last)

    speakers: dict[str, int] = {}
    total = 0
    active_days = 0
    for r in rows:
        total += r.total
        if r.total:
            active_days += 1
        for k, v in (r.speakers or {}).items():
            speakers[k] = speakers.get(k, 0) + int(v)

    # ⚠️ `busy_days` 只列"有照片"或"消息量 ≥ 该月中位数"的日子：
    #   697 天全列出来没人看得完，而时间线页要的是"值得点开的那几天"
    counts = sorted(r.total for r in rows if r.total)
    median = counts[len(counts) // 2] if counts else 0
    photo_days = {p["date"] for p in photos}
    days = [
        MonthDayOut(
            date=r.day.isoformat(), total=r.total,
            photo_count=len(
                [p for p in photos if p["date"] == r.day.isoformat()]
            ),
            highlights=(r.highlights or [])[:MAX_HIGHLIGHTS_OUT],
        )
        for r in rows
        if (r.highlights and len(r.highlights)) or photo_days
        or (r.total >= median and median and r.total > 0)
    ]
    return TimelineMonthOut(
        kb_id=kb_id, month=month, total=total, active_days=active_days,
        photo_count=len(photos), speakers=speakers, days=days,
    )


async def _day_view(
    session: DbSession, kb_id: int, date: str
) -> TimelineDayOut:
    d = dt.date.fromisoformat(date)
    row = (
        await session.execute(
            select(ChatDayStat).where(
                ChatDayStat.kb_id == kb_id, ChatDayStat.day == d
            )
        )
    ).scalar_one_or_none()
    photos = await _photos_between(session, kb_id, d, d + dt.timedelta(days=1))
    return TimelineDayOut(
        date=date,
        total=row.total if row else 0,
        types=dict(row.types or {}) if row else {},
        speakers=dict(row.speakers or {}) if row else {},
        highlights=(row.highlights or [])[:MAX_HIGHLIGHTS_OUT] if row else [],
        photos=photos,
        docs=row.docs if row else 0,
    )


async def _photos_between(
    session: DbSession, kb_id: int, lo: dt.date, hi: dt.date
) -> list[dict]:
    """`[lo, hi)` 区间内**有事件日期**的图片文档。

    ⚠️ `taken_at IS NULL` 的**排除**：没有日期的照片挂不上时间线，
    而把它们塞进"今天"或"导入那天"都是错的（见 `timeline.py` 的"不猜"）。
    """
    rows = (
        await session.execute(
            select(Document).where(
                Document.kb_id == kb_id,
                Document.is_current.is_(True),
                Document.deleted_at.is_(None),
                Document.taken_at.isnot(None),
                Document.taken_at >= lo,
                Document.taken_at < hi,
                Document.mime_type.like("image/%"),
            ).order_by(Document.taken_at, Document.file_name)
        )
    ).scalars().all()
    return [
        {
            "date": d.taken_at.isoformat(),
            "doc_id": d.id,
            "file_name": d.file_name,
            "thumb_url": f"/api/v1/documents/{d.id}/thumbs/1",
        }
        for d in rows
    ]


@router.get(
    "/kb/{kb_id}/timeline/anniversaries",
    response_model=list[AnniversaryOut],
    summary="纪念日（有照片的日子）",
)
async def anniversaries(
    kb_id: int, ctx: CurrentUser, session: DbSession, limit: int = 20
) -> list[AnniversaryOut]:
    """有照片的日子，按照片数降序。

    ⚠️ **只列有照片的日子**：契约的原话就是"纪念日（有照片的日子）"。
    列出没有照片的日子会让"纪念日"退化成"消息最多的一天"，
    而那不是用户对"纪念日"的预期。
    """
    await _require_view_kb(session, ctx, kb_id)
    rows = (
        await session.execute(
            select(Document.taken_at, func.count(Document.id))
            .where(
                Document.kb_id == kb_id,
                Document.is_current.is_(True),
                Document.deleted_at.is_(None),
                Document.taken_at.isnot(None),
                Document.mime_type.like("image/%"),
            )
            .group_by(Document.taken_at)
            .order_by(func.count(Document.id).desc(), Document.taken_at)
            .limit(max(1, min(limit, 100)))
        )
    ).all()

    out: list[AnniversaryOut] = []
    for day, n in rows:
        photos = await _photos_between(
            session, kb_id, day, day + dt.timedelta(days=1)
        )
        stat = (
            await session.execute(
                select(ChatDayStat).where(
                    ChatDayStat.kb_id == kb_id, ChatDayStat.day == day
                )
            )
        ).scalar_one_or_none()
        out.append(
            AnniversaryOut(
                date=day.isoformat(), count=int(n),
                photos=[p["file_name"] for p in photos],
                chat_total=stat.total if stat else 0,
            )
        )
    return out


@router.get(
    "/kb/{kb_id}/timeline/milestones",
    response_model=list[MilestoneOut],
    summary="里程碑（消息量突出的日子）",
)
async def milestones(
    kb_id: int, ctx: CurrentUser, session: DbSession, limit: int = 20
) -> list[MilestoneOut]:
    """消息量 top-N 的日子。

    ⚠️ **判据写清楚**：按 `total` 降序，不设"阈值"魔法数 ——
    「这个月哪天最热闹」是可解释的规则，而"超过均值 1.5 倍"不是
    （换个语料规模含义就变了，用户也看不懂为什么某天没进列表）。
    """
    await _require_view_kb(session, ctx, kb_id)
    rows = (
        await session.execute(
            select(ChatDayStat)
            .where(ChatDayStat.kb_id == kb_id, ChatDayStat.total > 0)
            .order_by(ChatDayStat.total.desc(), ChatDayStat.day)
            .limit(max(1, min(limit, 200)))
        )
    ).scalars().all()

    out: list[MilestoneOut] = []
    for r in rows:
        n_photos = (
            await session.execute(
                select(func.count(Document.id)).where(
                    Document.kb_id == kb_id,
                    Document.taken_at.isnot(None),
                    Document.mime_type.like("image/%"),
                    Document.taken_at == r.day,
                )
            )
        ).scalar_one()
        out.append(
            MilestoneOut(
                date=r.day.isoformat(), chat_total=r.total, photos=int(n_photos),
                note=(
                    "有照片的一天" if n_photos else f"当天 {r.total} 条消息"
                ),
            )
        )
    return out


@router.get(
    "/kb/{kb_id}/photos",
    response_model=Page[PhotoOut],
    summary="照片墙（photo KB）",
)
async def photos(
    kb_id: int,
    ctx: CurrentUser,
    session: DbSession,
    date: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = 1,
    page_size: int = 50,
) -> Page[PhotoOut]:
    """按事件日期倒序的照片墙（分页包，契约 §0.4）。

    ⚠️ **没有日期的排在最后**而不是被过滤掉：`taken_at IS NULL` 的照片
    在时间线/纪念日里没有位置，但照片墙是"看看我的照片"，
    直接藏起来等于让用户以为照片丢了。所以用 `NULLS LAST` 兜底列出。
    """
    await _require_view_kb(session, ctx, kb_id)
    stmt = select(Document).where(
        Document.kb_id == kb_id,
        Document.is_current.is_(True),
        Document.deleted_at.is_(None),
        Document.mime_type.like("image/%"),
    )
    if date:
        stmt = stmt.where(Document.taken_at == dt.date.fromisoformat(date))

    total = (
        await session.execute(
            select(func.count()).select_from(stmt.subquery())
        )
    ).scalar_one()
    rows = (
        await session.execute(
            stmt.order_by(
                Document.taken_at.desc().nullslast(), Document.file_name
            ).offset(max(0, (page - 1) * page_size)).limit(page_size)
        )
    ).scalars().all()

    return Page[PhotoOut](
        items=[
            PhotoOut(
                doc_id=d.id,
                file_name=d.file_name,
                date=d.taken_at.isoformat() if d.taken_at else None,
                size_bytes=d.size_bytes,
                mime_type=d.mime_type,
                status=d.status,
                # ⚠️ 缩略图端点要 Bearer，前端必须走 fetch（§6.8），
                #   不能直接塞进 `<img src>`
                thumb_url=f"/api/v1/documents/{d.id}/thumbs/1",
            )
            for d in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


__all__ = ["router", "VIEW_SOURCE_KINDS", "MAX_MONTHS"]