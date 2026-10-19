"""e2e/eval 共用的 fixture 生成（时间线与照片墙）。

## 为什么单独一个模块

- `web/e2e/check_timeline.mjs` 要造"跨 3 个月 + 多年份"的聊天与带日期的照片，
  Node 侧**写不出合法的 JPEG**（要么引一个图像库，要么手搓 zlib），
  而 e2e 里 `execFileSync("uv", ["run", "python", ...])` 已经是既有模式
  （重建 BM25 就走这条）。
- 所以生成逻辑放在 Python 这边，Node 只调一次。

## 为什么数据要**跨年**

`web/src/lib/timeline.ts::shiftMonth` 最该验的是 `2024-12` 的下个月是 `2025-01`。
只造一个月的数据时，翻页按钮点错了也会"看起来对"（都是空月份），
所以这里造 4 个月份：`2024-07`（密）、`2024-12`（疏）、`2025-01`（跨年相邻）、
`2025-03`（一个爆量日 → 里程碑第一名）。

## 说话人要两个人

`chat_day_stats.speakers` 只在两个人时才有意义：一个人时
「张三 60 · 李四 54」这条 UI 的正确性验不出来（只有一半）。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

#: 白名单根目录（导入端点只认这里，见 D92）
IMPORTS = Path(__file__).resolve().parents[1] / "data" / "imports"

SPEAKER_A = "张三"
SPEAKER_B = "李四"


def _add(rows: list[str], state: dict, day: str, hour: int, who: str, text: str) -> None:
    """追加一条消息。⚠️ 时间戳用 `datetime` 造，不要手拼字符串 `int()`。"""
    dt = datetime.fromisoformat(f"{day} {hour:02d}:00:00+08:00")
    rows.append(
        json.dumps(
            {
                "id": f"m{state['n']}",
                "ts": int(dt.timestamp() * 1000),
                # ⚠️ `date` **字面量**才是聚合的键（D97）：时间戳与日期字面量
                #   是两回事，带 +08:00 的偏移不会让消息串到别的日子。
                "date": day,
                "uid": "u_a" if who == SPEAKER_A else "u_b",
                "name": who,
                "text": text,
                "type": "text",
            },
            ensure_ascii=False,
        )
    )
    state["n"] += 1


def make_chat_fixture(path: Path | None = None) -> Path:
    """4 个月份的聊天 jsonl（200 + 12 + 42 + 120 条）。"""
    dest = path or (IMPORTS / "chat_multi.jsonl")
    dest.parent.mkdir(parents=True, exist_ok=True)
    rows: list[str] = []
    state = {"n": 0}

    # 7 月：密集（每天 6~10 条）—— 月视图里出现多个"值得点开"的日子
    for d in range(1, 15):
        for i in range(6 + d % 5):
            who = SPEAKER_A if i % 2 == 0 else SPEAKER_B
            _add(rows, state, f"2024-07-{d:02d}", 8 + i % 12, who,
                 f"7月{d}日 第{i}条：导入验证内容")

    # 12 月：只有 3 天（稀疏月份）
    for d in (24, 25, 31):
        for i in range(4):
            _add(rows, state, f"2024-12-{d:02d}", 20 + i, SPEAKER_A, f"跨年的第{i}条")

    # 2025-01 与 2024-12 相邻 → 验「上个月/下个月」跨年
    for i in range(30):
        _add(rows, state, "2025-01-01", 9 + i % 6,
             SPEAKER_B if i % 2 else SPEAKER_A, f"元旦第{i}条消息")
    for i in range(12):
        _add(rows, state, "2025-01-15", 10 + i % 8, SPEAKER_A, f"一月十五第{i}条")

    # 2025-03：一个爆量日（里程碑的第一名必须是它）
    for i in range(120):
        _add(rows, state, "2025-03-08", 6 + i % 16,
             SPEAKER_A if i % 3 else SPEAKER_B,
             f"三月八日第{i}条消息" + ("重要通知" if i % 20 == 0 else ""))

    dest.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return dest


def make_photo_fixtures() -> tuple[Path, Path]:
    """`(带日期的目录, 无日期的目录)`。

    ⚠️ 无日期那批**必须有**：它们验的是 D98「不猜」的下游行为 ——
    `taken_at` 留空的照片**仍要**在照片墙里列得出来（排在最后一档）。
    没有这批数据，"无日期"这条路径在界面上完全验不到。
    """
    from PIL import Image

    colors = [(200, 60, 60), (60, 160, 90), (70, 90, 200), (200, 170, 60)]
    dated = IMPORTS / "photos_multi"
    dated.mkdir(parents=True, exist_ok=True)
    n = 0
    for day, cnt in (("2024-07-31", 2), ("2024-12-25", 3),
                     ("2025-01-01", 4), ("2025-03-08", 2)):
        for i in range(cnt):
            Image.new("RGB", (160, 120), colors[n % len(colors)]).save(
                dated / f"chat-{day.replace('-', '')}-图{i}.jpg"
            )
            n += 1

    undated = IMPORTS / "photos_undated"
    undated.mkdir(parents=True, exist_ok=True)
    for i in range(2):
        Image.new("RGB", (120, 90), colors[i]).save(undated / f"随手拍{i}.jpg")
    return dated, undated


#: 「持久照片库」用到的**每张图的形状与配色**。
#:
#: 🔑 为什么画成**形状 + 颜色**而不是纯色块：colSmol 的向量是对**画面内容**
#:   的编码。纯色块的 8 张图向量几乎一样 ⇒ 即便视觉腿正常写入，
#:   "视觉召回能不能区分它们"这件事也**测不出来**（相似度全一样）。
#:   有了明确的形状差异，才能拿它对照文本腿看两条腿的分工。
_KEEPER_SHAPES: tuple[tuple[str, tuple[int, int, int]], ...] = (
    ("circle", (200, 60, 60)),
    ("square", (60, 160, 90)),
    ("triangle", (70, 90, 200)),
    ("cross", (200, 170, 60)),
    ("ring", (150, 60, 160)),
    ("bars", (60, 170, 180)),
    ("dot-grid", (210, 120, 50)),
    ("checker", (110, 110, 110)),
)


def make_photo_keeper_fixture(n: int = 8) -> Path:
    """**持久**照片库用的图片目录（`deploy/seed_photos.py` 的输入）。

    ## 为什么需要这一份"不会被清掉"的 fixture

    视觉腿（`kb_visual`）在本机长期是 **0 个点**，而那**不是坏了**：
    唯一扫过图的文档都被 e2e 清库软删了，按 D7「先删 PG、异步清 Qdrant」
    点也被清掉 —— 0 点是正确的稳态。但后果是两个"承诺"无法兑现：

    - M4「扫描件走 colSmol 视觉召回」在当前数据上**看不到**（代码跑过，
      没有活数据可看）
    - **D50**（要不要让 API 进程也加载 colSmol）**没法决策** ——
      没有数据就测不出"视觉腿查询"的收益，而代价是确定的
      （实测模型加载 13s / 2.5GB）

    ⚠️ 所以这一份**故意用与其它 fixture 不同的域/前缀**（见
    `deploy/seed_photos.py` 的 `KEEPER_EMAIL`）：eval 清 `@t.local`、
    e2e 清 `1e-`/`1f-`/`1g-` 前缀 ⇒ 造完之后**每轮基线都不会把它删掉**。
    """
    from PIL import Image, ImageDraw

    out = IMPORTS / "photos_keeper"
    out.mkdir(parents=True, exist_ok=True)
    for i in range(min(n, len(_KEEPER_SHAPES))):
        name, color = _KEEPER_SHAPES[i]
        img = Image.new("RGB", (320, 240), (245, 245, 240))
        d = ImageDraw.Draw(img)
        box = (80, 60, 240, 180)
        if name == "circle":
            d.ellipse(box, fill=color)
        elif name == "square":
            d.rectangle(box, fill=color)
        elif name == "triangle":
            d.polygon([(160, 50), (60, 190), (260, 190)], fill=color)
        elif name == "cross":
            d.rectangle((140, 50, 180, 190), fill=color)
            d.rectangle((80, 110, 240, 150), fill=color)
        elif name == "ring":
            d.ellipse(box, fill=color)
            d.ellipse((120, 100, 200, 160), fill=(245, 245, 240))
        elif name == "bars":
            for k in range(5):
                d.rectangle((80 + k * 32, 190 - (k + 1) * 26, 104 + k * 32, 190),
                            fill=color)
        elif name == "dot-grid":
            for gx in range(4):
                for gy in range(3):
                    d.ellipse((90 + gx * 50, 70 + gy * 45, 120 + gx * 50,
                               100 + gy * 45), fill=color)
        elif name == "checker":
            for gx in range(6):
                for gy in range(4):
                    if (gx + gy) % 2 == 0:
                        d.rectangle((80 + gx * 28, 60 + gy * 30,
                                     108 + gx * 28, 90 + gy * 30), fill=color)
        else:
            # 🔴 **不要**写成 `else: # checker` —— 那样清单里任何拼错的形状
            #   都会**静默**画成棋盘格，于是 8 张图里有两张一模一样，
            #   而"形状明显不同"正是这个 fixture 存在的**唯一理由**。
            #   （守卫 `tests/test_seed_photos.py` 就是靠这条抓到的。）
            raise ValueError(
                f"清单里的形状 {name!r} 没有对应的绘制分支 —— "
                "它会落进兜底分支画成棋盘格，视觉召回就测不出区分度了"
            )
        img.save(out / f"keeper-{i:02d}-{name}.jpg", quality=88)
    return out


def make_timeline_fixtures() -> dict[str, str]:
    """e2e 入口：造全部 fixture，返回路径字典。"""
    IMPORTS.mkdir(parents=True, exist_ok=True)
    chat = make_chat_fixture()
    dated, undated = make_photo_fixtures()
    keeper = make_photo_keeper_fixture()
    return {
        "chat": str(chat),
        "photos_dated": str(dated),
        "photos_undated": str(undated),
        # 🔑 持久照片库那一批（D152）：单独列出来，因为**只有** seed_photos.py
        #   会用它 —— 其余验收脚本碰它就会把视觉腿的数据清掉。
        "photos_keeper": str(keeper),
        "chat_msgs": str(len(chat.read_text(encoding="utf-8").splitlines())),
    }


if __name__ == "__main__":
    print(make_timeline_fixtures())