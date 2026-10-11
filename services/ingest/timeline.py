"""D20 的数据生产侧：导入时落**按天聚合** + 抽**事件日期**。

## 为什么这些必须是纯函数

- `photo_date()`：抽错日期 = 照片墙上这张图**挂在错误的一天**，
  而时间线没有任何"看起来不对"的提示 → 必须能离线穷举各种文件名。
- `DayAgg`：累加逻辑错 = 重复导入后数字翻倍，而时间线**只显示数字**，
  用户没有别的地方可以对账。

## 日期来源的优先级（为什么是这个顺序）

| 顺序 | 来源 | 为什么优先 |
|---|---|---|
| 1 | **EXIF `DateTimeOriginal`** | 相机写的时间，唯一可信的事件时间 |
| 2 | 文件名里的 `YYYYMMDD` / `YYYY-MM-DD` | 手机相册导出普遍用 `chat-20240731-告白日-a.jpg` 这种 |
| 3 | `None` | ⚠️ **不猜**。`created_at` 是导入时间不是事件时间（见 `models.py::Document.taken_at`） |

第 3 条是关键：实测 23 张照片里有 3 张既没有 EXIF 也没有日期文件名。
把它们的导入时间当事件日期，会让时间线上"今天突然多了 3 张老照片"。
宁可那 3 张不参与时间线（照片墙仍然列得出来），也不要放错位置。
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import Iterable
from typing import Any

logger = logging.getLogger(__name__)

#: 文件名里的日期：`20240731` / `2024-07-31` / `2024_07_31` / `202407`
_DT8 = re.compile(r"(?<!\d)(\d{4})[-_]?(\d{2})[-_]?(\d{2})(?!\d)")
_YM = re.compile(r"(?<!\d)(\d{4})[-_]?(\d{2})(?!\d)")

#: 合理区间：手机照片 2000 年后、今天是 2026。⚠️ 上界必须有 ——
#: 否则 `20249999` 这种垃圾串会被"抽成"一个合法日期，
#: 而时间线上就多了一天根本不存在的数据。
MIN_YEAR, MAX_YEAR = 2000, 2030


def exif_date(data: bytes) -> dt.date | None:
    """从图片字节里读 EXIF 拍摄时间。

    ⚠️ 用 Pillow 而不是手写 TIFF 解析：EXIF 的偏移量与标签嵌套很容易错，
    而错出来的结果是**一个错日期**（还不报错）。
    读不到（无 EXIF / 解析异常 / 损坏文件）一律 `None`。
    """
    try:
        import io

        from PIL import Image

        # ⚠️ 标签编号直接写死，**不查 ExifTags.TAGS**：不同 Pillow 版本里
        #   它的方向不一样（2.x 是 `名字 → {tag: 编号}`，新版本是 `编号 → 名字`），
        #   按方向反着用会在某些版本上抛 TypeError ——
        #   而异常被下面的宽 except 吞掉，症状是"所有照片都没有拍摄日期"，
        #   看起来像"手机相机不写 EXIF"（第一版就这么错的）。
        #   这三个编号是 EXIF 规范里固定的：DateTimeOriginal / Digitized / DateTime。
        with Image.open(io.BytesIO(data)) as im:
            ex = im.getexif()
            if not ex:
                return None
            for tag_id in (36867, 36868, 306):        # Original/Digitized/DateTime
                raw = ex.get(tag_id)
                if not raw:
                    continue
                # ⚠️ **两种格式都要认**（实测踩过）：原始 EXIF 字节里是
                #   "2024:07:31 20:45:02"，但 Pillow 的 `getexif()` 会把它
                #   **规范化**成 "2024-07-31 20:45:02"。只认一种的话，
                #   另一种就永远返回 None —— 而症状是"所有照片都没有拍摄日期"，
                #   看起来像"手机相机不写 EXIF"。
                raw_s = str(raw).strip()
                parsed = None
                for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                    try:
                        parsed = dt.datetime.strptime(raw_s, fmt)
                        break
                    except ValueError:
                        continue
                if parsed is None:
                    continue
                if MIN_YEAR <= parsed.year <= MAX_YEAR:
                    return parsed.date()
    except Exception as exc:  # noqa: BLE001 — 图片损坏/无 PIL 不该让导入失败
        # ⚠️ 但**必须留痕**：宽 except 最容易把"我的代码写错了"伪装成
        #   "数据里没有"（踩过 —— 见上面 ExifTags 的注释）。
        logger.debug("读 EXIF 失败（当作没有拍摄日期）：%s", exc)
        return None
    return None


def name_date(file_name: str) -> dt.date | None:
    """从**文件名**抽日期（`chat-20240731-告白日-a.jpg` → 2024-07-31）。

    ⚠️ **只认完整的 8 位**（YYYYMMDD）或 `YYYY-MM`。只认 6 位是为了避开
    "第 3 张"这类序号；而 `20240731-a` 与 `20240731b` 会**各抽一次**，
    没关系 —— 同一张图的多个版本本来就该落在同一天。
    """
    stem = file_name.rsplit("/", 1)[-1]
    m = _DT8.search(stem) or _YM.search(stem)
    if not m:
        return None
    y, mo = int(m.group(1)), int(m.group(2))
    d = int(m.group(3)) if m.lastindex and m.lastindex >= 3 else 1
    if not (MIN_YEAR <= y <= MAX_YEAR and 1 <= mo <= 12 and 1 <= d <= 31):
        return None
    try:
        return dt.date(y, mo, d)
    except ValueError:      # 2024-02-31
        return None


def photo_date(file_name: str, data: bytes) -> dt.date | None:
    """照片的事件日期：EXIF → 文件名 → `None`（见模块 docstring）。"""
    return exif_date(data) or name_date(file_name)


# ============================================================
# 按天聚合
# ============================================================

#: 每天最多留几条 highlight。全量存会把行撑大（实测单天 743 条消息），
#: 而时间线只显示摘要。
MAX_HIGHLIGHTS = 3
#: 单条 highlight 的字符上限（契约里 highlight 展示也是截断的）
HIGHLIGHT_CHARS = 160


class DayAgg:
    """一天的聚合累加器（纯逻辑，不碰 DB）。

    🔑 幂等靠"累加"：重复导入同一天会**翻倍**。所以
    `handle_import_chat` 只在**新建文档**的切片上累加 ——
    内容重复被跳过时不能重复累加（否则时间线数字会随重导次数膨胀）。
    """

    __slots__ = ("total", "types", "speakers", "highlights", "docs")

    def __init__(self) -> None:
        self.total = 0
        self.types: dict[str, int] = {}
        self.speakers: dict[str, int] = {}
        self.highlights: list[dict[str, str]] = []
        self.docs = 0

    def add_message(self, msg: dict[str, Any]) -> None:
        self.total += 1
        t = str(msg.get("type") or "text")
        self.types[t] = self.types.get(t, 0) + 1
        who = str(msg.get("name") or "").strip() or "匿名"
        self.speakers[who] = self.speakers.get(who, 0) + 1

        text = str(msg.get("text") or "").strip()
        # 只有"足够长且不是空消息"的才值得当 highlight：
        # 实测 highlight 全是"最长消息"，而撤回/空消息会占满名额
        if len(text) >= 12:
            self.highlights.append(
                {"name": who, "text": text[:HIGHLIGHT_CHARS]}
            )
            # ⚠️ 保持"最长的 N 条"：按长度截断维护，而不是先来先占
            self.highlights.sort(key=lambda h: len(h["text"]), reverse=True)
            del self.highlights[MAX_HIGHLIGHTS:]

    def add_doc(self) -> None:
        self.docs += 1

    def as_row(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "types": dict(self.types),
            "speakers": dict(self.speakers),
            "highlights": list(self.highlights),
            "docs": self.docs,
        }

    def merge(self, other: "DayAgg") -> None:
        """把另一个累加器并进来（跨切片/跨文档的同一天）。"""
        self.total += other.total
        self.docs += other.docs
        for k, v in other.types.items():
            self.types[k] = self.types.get(k, 0) + v
        for k, v in other.speakers.items():
            self.speakers[k] = self.speakers.get(k, 0) + v
        self.highlights.extend(other.highlights)
        self.highlights.sort(key=lambda h: len(h["text"]), reverse=True)
        del self.highlights[MAX_HIGHLIGHTS:]


def aggregate_by_day(
    docs: Iterable[tuple[str, list[dict[str, Any]]]],
) -> dict[str, DayAgg]:
    """`(日期, 消息列表)` → 按天累加器。

    ⚠️ 没有日期的消息**直接丢弃**（不进任何一天）：把"日期未知"塞进
    某一天会让时间线出现凭空的一天，而那一天的数字永远对不上。
    `handle_import_chat` 会把它们计进 `undated_msgs` 报出来。
    """
    out: dict[str, DayAgg] = {}
    for day, msgs in docs:
        if not day:
            continue
        agg = out.setdefault(day, DayAgg())
        for m in msgs:
            agg.add_message(m)
        agg.add_doc()
    return out


__all__ = [
    "HIGHLIGHT_CHARS",
    "MAX_HIGHLIGHTS",
    "DayAgg",
    "aggregate_by_day",
    "exif_date",
    "name_date",
    "photo_date",
]