"""🔑 D20 时间线的纯函数：`ingest/timeline.py`。

这些函数的错**不会报错**，只会让时间线上出现一个错的数字或一张放错位置的照片，
所以每条用例都对应一种具体的坏情况：

| 用例 | 坏情况 |
|---|---|
| `name_date` 8 位/6 位/非法值 | 照片挂到错误的一天，或凭空多出一天不存在的数据 |
| `exif_date` 两种日期格式 | "所有照片都没有拍摄日期"（看起来像相机不写 EXIF） |
| `exif_date` 损坏字节 | 导入任务整体失败（而不是"这张图没日期"） |
| `DayAgg` highlight 取最长 N 条 | 撤回/空消息占满名额，纪念日摘要全是垃圾 |
| `DayAgg.merge` 同键相加 | 跨切片的说话人计数被覆盖（JSONB `||` 的坑） |
| `aggregate_by_day` 丢弃无日期消息 | 时间线凭空多出一天，而那天的数字永远对不上 |
"""

from __future__ import annotations

import datetime as dt
import io

from PIL import Image

from services.ingest.timeline import (
    HIGHLIGHT_CHARS,
    MAX_HIGHLIGHTS,
    DayAgg,
    aggregate_by_day,
    exif_date,
    name_date,
    photo_date,
)


# ============================================================
# 1. 文件名日期
# ============================================================


def test_name_date_variants():
    """手机相册导出常见的三种写法都要认。"""
    assert name_date("chat-20240731-告白日-a.jpg") == dt.date(2024, 7, 31)
    assert name_date("IMG_20230715_203045.jpg") == dt.date(2023, 7, 15)
    assert name_date("2024-07-21.png") == dt.date(2024, 7, 21)
    assert name_date("20240731b.png") == dt.date(2024, 7, 31)


def test_name_date_year_month_only():
    """6 位（YYYYMM）→ 当月 1 号。"""
    assert name_date("202407.jpg") == dt.date(2024, 7, 1)


def test_name_date_rejects_garbage():
    """⚠️ 非法值必须给 None，不能"抽成一个看起来合法的日期"。"""
    for bad in ("no-date.jpg", "第3张.png", "20249999.jpg", "2024-02-31.jpg",
                "0000-00-00.jpg", "99999999.jpg"):
        assert name_date(bad) is None, f"{bad} 不该抽出日期"


def test_name_date_ignores_short_numbers():
    """"3.jpg"/"第 12 期" 这种不能被当成日期。"""
    assert name_date("3.jpg") is None
    assert name_date("episode12.jpg") is None


def test_name_date_strips_path():
    """目录名里的日期不能影响结果（`2024/05/photo.jpg` → 取文件名）。"""
    assert name_date("/data/2024/05/photo_20240731.jpg") == dt.date(2024, 7, 31)


# ============================================================
# 2. EXIF
# ============================================================


def _jpeg_with_exif(value: str) -> bytes:
    buf = io.BytesIO()
    im = Image.new("RGB", (8, 8), (10, 20, 30))
    ex = Image.Exif()
    ex[36867] = value          # DateTimeOriginal
    im.save(buf, "JPEG", exif=ex)
    return buf.getvalue()


def _jpeg_plain() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, "JPEG")
    return buf.getvalue()


def test_exif_date_read():
    """🔑 **两种格式都要认**：原始 EXIF 是 `2024:07:31 20:45:02`，
    而 Pillow 的 `getexif()` 会**规范化**成 `2024-07-31 20:45:02`。
    只认一种的话，症状是"所有照片都没有拍摄日期"。
    """
    assert exif_date(_jpeg_with_exif("2024:07:31 20:45:02")) == dt.date(2024, 7, 31)
    assert exif_date(_jpeg_with_exif("2024-07-31 20:45:02")) == dt.date(2024, 7, 31)


def test_exif_missing_returns_none():
    assert exif_date(_jpeg_plain()) is None


def test_exif_broken_bytes_do_not_raise():
    """🔑 损坏的图片必须给 None，不能让整个导入任务失败。"""
    assert exif_date(b"not-an-image") is None
    assert exif_date(b"") is None


def test_exif_implausible_year_rejected():
    """⚠️ 上界必须挡：`1999:01:01 00:00:00` 那种错日期会挂到错误的一年。"""
    assert exif_date(_jpeg_with_exif("1999:01:01 00:00:00")) is None


def test_photo_date_priority():
    """EXIF 胜过文件名（相机时间才是事件时间）。"""
    data = _jpeg_with_exif("2024:07:31 20:45:02")
    assert photo_date("chat-20200101-假日期.jpg", data) == dt.date(2024, 7, 31)


def test_photo_date_falls_back_to_name():
    assert photo_date("chat-20240731-x.jpg", _jpeg_plain()) == dt.date(2024, 7, 31)


def test_photo_date_all_missing_is_none():
    """🔑 **不猜**：两者都没有就 None。拿导入时间冒充事件日期，
    会让时间线上"今天突然多了 3 张老照片"。"""
    assert photo_date("no-date.jpg", _jpeg_plain()) is None


# ============================================================
# 3. 按天聚合
# ============================================================


def _m(i: int, *, day: str = "2024-07-21", who: str = "李四",
       kind: str = "text", text: str = "") -> dict:
    return {"date": day, "name": who, "type": kind,
            "text": text or f"第{i}条消息内容"}


def test_agg_counts():
    a = DayAgg()
    for i in range(5):
        a.add_message(_m(i))
    a.add_message(_m(9, kind="recall", text=""))
    a.add_doc()
    row = a.as_row()
    assert row["total"] == 6
    assert row["types"] == {"text": 5, "recall": 1}
    assert row["docs"] == 1


def test_agg_highlights_are_the_longest():
    """🔑 highlight 是"最长的几条"，不是"先来的几条"。

    实测后果：撤回/空消息占满名额 → 纪念日摘要全是"（recall）"。
    """
    a = DayAgg()
    a.add_message(_m(0, text="短"))
    a.add_message(_m(1, text="这是一条很长很长的消息" * 3))
    a.add_message(_m(2, text="中"))
    assert len(a.highlights) <= MAX_HIGHLIGHTS
    assert a.highlights[0]["text"].startswith("这是一条很长很长")


def test_agg_short_messages_are_not_highlights():
    """太短的消息（<12 字）不值得当摘要。"""
    a = DayAgg()
    a.add_message(_m(0, text="嗯"))
    assert a.highlights == []


def test_agg_highlight_is_truncated():
    a = DayAgg()
    a.add_message(_m(0, text="字" * (HIGHLIGHT_CHARS * 3)))
    assert len(a.highlights[0]["text"]) <= HIGHLIGHT_CHARS


def test_agg_merge_adds_same_keys():
    """🔑 同键必须**相加**。

    这正是 handler 里不用 JSONB `||` 的理由：`||` 是覆盖，
    跨切片合并会丢掉一边的计数，而时间线只显示数字、用户没地方对账。
    """
    a, b = DayAgg(), DayAgg()
    for i in range(3):
        a.add_message(_m(i, who="张三"))
    for i in range(2):
        b.add_message(_m(i, who="张三"))
    a.merge(b)
    assert a.total == 5
    assert a.speakers["张三"] == 5
    assert a.types["text"] == 5


def test_agg_merge_keeps_top_highlights():
    a, b = DayAgg(), DayAgg()
    a.add_message(_m(0, text="短一点的消息"))
    b.add_message(_m(1, text="很长" * 40))
    a.merge(b)
    assert len(a.highlights) == 1
    assert a.highlights[0]["text"].startswith("很长")


def test_agg_anonymous_speaker():
    """没有 name 的消息归到「匿名」，不能变成空字符串键。"""
    a = DayAgg()
    a.add_message(_m(0, who=""))
    assert a.speakers == {"匿名": 1}


def test_aggregate_by_day_drops_undated():
    """🔑 无日期的消息**不进任何一天**。

    把它们塞进某一天会让时间线凭空多出一天，而那天的数字永远对不上。
    """
    out = aggregate_by_day([
        ("2024-07-21", [_m(0), _m(1)]),
        ("", [_m(2)]),                  # 无日期
        ("2024-07-22", [_m(3)]),
    ])
    assert set(out) == {"2024-07-21", "2024-07-22"}
    assert out["2024-07-21"].total == 2
    assert out["2024-07-21"].docs == 1


def test_aggregate_by_day_same_day_two_docs_merges():
    """跨切片的同一天必须合并成一个聚合（否则时间线上"同一天两行"）。"""
    out = aggregate_by_day([
        ("2024-07-21", [_m(0)]),
        ("2024-07-21", [_m(1)]),
    ])
    assert len(out) == 1
    assert out["2024-07-21"].total == 2
    assert out["2024-07-21"].docs == 2