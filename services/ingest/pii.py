"""PII 检测与掩码（实现层，D121 · M8）。

## 为什么要搬进 `services/`

CONVENTIONS §0.1：`pipeline/` 是离线工具层，**被调用方**必须住在 `services/`。
搬动理由同 `services/ingest/privacy.py`：运行时摄取在 `services/worker` 里，
而 M8 要让"运行时上传的文档"也能掩码 —— 于是掩码器成了被调用方。
`pipeline/privacy/pii_detector.py` 现在只是 re-export（同一纪律：
**不能复制两份实现**，隐私逻辑漂移的方向永远是"漏掩"）。

## 🔑 三条设计约束（D121 · 与 D91/D114 同源的"不可逆"纪律）

1. **默认关闭**（`knowledge_bases.pii_mask` 默认 `False`）。
   个人知识库的核心用途是"搜我自己的手机号/身份证号"，
   默认掩码等于把这个用途关掉。开关是**按库**的：同一个工作区里
   "自己的日记库"保持原文，"要发给别人的证件库"开启掩码。
2. **只掩码索引侧，不动原件**。
   blob 里的原始文件、`/preview` 页图、`/documents/{id}/file` 下载
   全部保持原文 —— 掩码只发生在"解析出来的文本 → chunk"这一步。
   这样"我要看原文"永远有路可走，也就不会因为掩码而丢数据。
3. **检出报告只读**。`GET /kb/{id}/pii` 只报告"哪里有、有多少"，
   **不返回原文片段**（否则报告本身就成了新的泄露面）。

## 掩码后长度会变 → 坐标会偏（已知取舍）

`email` 的掩码比原文短（`ab***@qq.com`），所以命中邮箱之后
`Chunk.char_start/char_end` 会相对原文偏移几个字符。
⚠️ 影响很小：L3 高亮用的是**块级 bbox**（`Chunk.locations`），
不是字符级偏移；偏移量本身只是给人看的定位信息。
反过来说，**不能为了保持长度而保留完整邮箱**—— 那等于没掩。

## 修掉的两个真实缺陷（原 `pipeline/privacy/pii_detector.py`）

| 缺陷 | 症状 | 现在 |
|---|---|---|
| **重叠命中会把文本复制一份** | 一串 18 位数字能同时命中 `id_card` 与 `bank_card`，两个命中**跨度完全相同**；原实现按 `start` 排序后依次拼接，第二段把同一段文字**再写了一遍** → "身份证 441***0123***5678" 变成两份 | 先合并同跨度命中（按类型优先级去重），且**跳过被前一段覆盖的命中** |
| `mask_value` 对 6~7 位数字**比原文更长** | `raw[:3] + "*"*(len-7) + raw[-4:]`，len=6 时 `"*"*(-1)` = "" → 3+4=7 字符，**比 6 位原文还长**，掩了个寂寞还改了内容 | 长度自适应：≤7 位只留首尾各 1 位 |

⚠️ 这两个都不是"理论上"，第二个在 `ID_CARD` 的年份段与
`BANK_CARD` 前缀凑巧重叠时就会触发（例如 `62212320201234567`：
`622123` + 年 `2020` + 月 `12` + 日 `01` + `234` + 校验 `5`，
既满足 18 位身份证也满足 16~19 位银行卡）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable

#: 身份证 18 位（含校验位，宽松）
ID_CARD = re.compile(
    r"(?<!\d)([1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])(?!\d)"
)
#: 手机号（大陆）
PHONE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
#: 邮箱
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
#: 学号（常见 8-12 位数字，排除手机号冲突）
STUDENT_ID = re.compile(r"(?<!\d)(202\d{5,9})(?!\d)")
#: 中国大陆银行卡（16-19 位）
BANK_CARD = re.compile(r"(?<!\d)(62\d{14,17})(?!\d)")
#: QQ 号（5-11 位，2000 万量级起）— 用于统计**不掩码**
QQ = re.compile(r"(?<!\d)([1-9]\d{7,10})(?!\d)")

PATTERNS: dict[str, re.Pattern[str]] = {
    "id_card": ID_CARD,
    "phone": PHONE,
    "email": EMAIL,
    "student_id": STUDENT_ID,
    "bank_card": BANK_CARD,
}

#: 🔑 掩码时的类型优先级：**同跨度命中时保留哪一个**。
#: 身份证与银行卡规则可能命中同一串数字（见模块 docstring），
#: 取"更具体的"——身份证带日期结构，银行卡只是 62 开头。
TYPE_PRIORITY = ("id_card", "bank_card", "phone", "student_id", "email")


def detect_pii(text: str) -> list[dict]:
    """返回命中列表：`[{type, raw, start, end}]`。

    ⚠️ **顺序**按类型分组（`PATTERNS` 的插入序），不是按位置 ——
    调用方要按位置拼装掩码后文本时必须自己排序（`mask_text` 里有）。
    """
    hits: list[dict] = []
    for ptype, pat in PATTERNS.items():
        for m in pat.finditer(text):
            raw = m.group(1) if m.lastindex else m.group(0)
            hits.append(
                {"type": ptype, "raw": raw, "start": m.start(), "end": m.end()}
            )
    return hits


def counts_by_type(hits: Iterable[dict]) -> dict[str, int]:
    """命中按类型计数（`{}` 而不是所有类型 0 值 —— 报告里只列真的有）。

    ⚠️ 用 `dict` 而非 `Counter` 转普通 dict：JSON 序列化与
    「前端 `Object.keys(x).length` 就是类型数」这两个用法都需要它。
    """
    out: dict[str, int] = {}
    for h in hits:
        out[h["type"]] = out.get(h["type"], 0) + 1
    return out


def mask_value(ptype: str, raw: str) -> str:
    """把一个命中值掩成不可还原、**且不比原文长**的形式。

    ⚠️ 长度安全是硬要求：掩码后**更长**等于"掩了个寂寞还改了内容"。
    原实现对 6~7 位数字会算出 `raw[:3] + raw[-4:]`（3+4=7 > 6）。
    """
    n = len(raw)
    if ptype == "email" and "@" in raw:
        local, _, domain = raw.partition("@")
        # ⚠️ 本地部分只留 1 个字符：留 2 个时"ha***@qq.com"还留着整个
        #   英文名（实测 `zhangsan@example.com` → `z***@example.com` 才算掩住）。
        #   域名**必须留着** —— 不然"哪个公司的邮箱"这条信息全丢，
        #   而它在多数场景里不是隐私。
        out = f"{local[:1]}***@{domain}"
        if len(out) <= len(raw):
            return out
        # ⚠️ 极短邮箱（`a@b.co` → `a***@b.co`，9 > 6）：`***@` 是净增的，
        #   对短值来说掩码反而把文本撑长。改走等长打码。
        return raw[:1] + "*" * (n - 2) + raw[-1:]
    # ⚠️ 没有 "@" 的值不走 email 分支（`"1"` → `"1***@"` 比原文长 4 倍）。
    #   `detect_pii` 不会产出这种 email 命中，所以线上碰不到；
    #   但 `mask_value` 是公开函数，"掩码不比原文长"这条承诺要对**所有**入参成立。
    if n <= 2:
        # 极短值：全打码。⚠️ 这一档原来会算出 `raw[:1] + raw[-1:]`（2 倍长）
        return "*" * n
    if n <= 7:
        # 6~7 位：留首尾各 1 位。⚠️ 长度陷阱就出在这一档
        #   （原实现 `raw[:3] + "*"*(len-7) + raw[-4:]`，len=6 时算出 7 位）
        return raw[:1] + "*" * (n - 2) + raw[-1:]
    # 8 位以上：前 3 后 4。**对含字母的值同样适用** ——
    # 身份证末位可能是 X，若按"非纯数字"另走一条分支就会缩成 5 位，
    # 徒增文本长度偏移（掩码后长度 ≠ 原文长度）。
    return raw[:3] + "*" * (n - 7) + raw[-4:]


def _dedupe_hits(hits: list[dict]) -> list[dict]:
    """按位置排序 + 丢掉被覆盖/同跨度的冗余命中。

    🔑 两条规则，缺一不可：

    1. **同跨度**：保留 `TYPE_PRIORITY` 里排在前面的那个类型。
       （`62212320201234567` 同时是身份证和银行卡。）
    2. **被覆盖**：跨度结束位置不超过前一段末尾的，直接丢 ——
       否则拼接时会把同一段文字写两遍（实测：身份证掩码后面紧跟
       一模一样的银行卡掩码）。
    """
    by_span: dict[tuple[int, int], dict] = {}
    for h in hits:
        key = (h["start"], h["end"])
        cur = by_span.get(key)
        if cur is None or _rank(h["type"]) < _rank(cur["type"]):
            by_span[key] = h

    kept: list[dict] = []
    last_end = -1
    for h in sorted(by_span.values(), key=lambda x: (x["start"], -x["end"])):
        if h["start"] < last_end:
            continue                      # 被前一段盖住
        kept.append(h)
        last_end = h["end"]
    return kept


def _rank(ptype: str) -> int:
    try:
        return TYPE_PRIORITY.index(ptype)
    except ValueError:
        return len(TYPE_PRIORITY)


def mask_text(text: str) -> tuple[str, list[dict]]:
    """掩码全文里的 PII，返回 `(掩码后文本, 去重后的命中列表)`。

    ⚠️ 返回的命中是**去重后**的：同跨度只算一次，
    否则计数会虚高（一个身份证被当成"身份证+银行卡"两处）。
    """
    hits = _dedupe_hits(detect_pii(text))
    if not hits:
        return text, hits
    parts: list[str] = []
    last = 0
    for h in hits:
        parts.append(text[last:h["start"]])
        parts.append(mask_value(h["type"], h["raw"]))
        last = h["end"]
    parts.append(text[last:])
    return "".join(parts), hits


def summarize(text: str) -> tuple[dict[str, int], int]:
    """`(按类型计数, 总数)` —— 报告端点用（只计数，不返回原文）。

    ⚠️ 这里用 `mask_text` 的去重逻辑而不是裸 `detect_pii`：
    报告的数字必须与真正掩掉的数量一致，否则"报告说 1 处、
    实际掩了 2 处"会让用户以为掩码没生效。
    """
    _, hits = mask_text(text)
    return counts_by_type(hits), len(hits)


__all__ = [
    "BANK_CARD",
    "EMAIL",
    "ID_CARD",
    "PATTERNS",
    "PHONE",
    "QQ",
    "STUDENT_ID",
    "TYPE_PRIORITY",
    "counts_by_type",
    "detect_pii",
    "mask_text",
    "mask_value",
    "summarize",
]