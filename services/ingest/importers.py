"""🔑 目录/JSONL 导入的**公共层**（D47 聊天 / D48 图片目录）。

两个导入端点的形状完全不同（一个是 87k 行 jsonl，一个是几千张图），
但底下是**同一件事**：把外部文件变成 `documents` 行 + blob + `KIND_INGEST` 任务。
所以这段逻辑只写一份，`import/chat` 与 `import/dir` 的 worker handler
都调它 —— §0.9 的"同源"纪律在这里的体现。

## 为什么拆成纯函数

三个最难测的部分都做成纯函数（不碰 DB、不读文件系统）：

| 函数 | 为什么必须是纯的 |
|---|---|
| `plan_chat_slices` | 切分边界错了会**静默丢消息**（D41 的 50 条） |
| `render_chat_slice` | 渲染格式决定检索能不能命中（时间戳/说话人必须进正文） |
| `resolve_import_path` | 🔑 **路径越界**是安全问题，逻辑必须能离线穷举 |

剩下的 IO 部分（读文件、创建文档）才碰盘/DB。
"""

from __future__ import annotations

import datetime as dt
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..common.errors import InvalidRequest, ValidationFailed

#: D41：每 50 条消息切一个文档。
DEFAULT_MESSAGES_PER_DOC = 50

#: 单个 jsonl 行的字节上限（防御畸形行：一行 200MB 会把内存吃穿）。
_MAX_LINE_BYTES = 1 << 20


# ============================================================
# 路径安全（D92）
# ============================================================


def import_roots() -> list[Path]:
    """允许导入的根目录列表。

    🔑 **必须有白名单**：端点收的是"服务器上的路径"，没有白名单的话
    任何租户都能 `{"path":"/etc"}` 或 `{"path":"../../另一个租户的数据"}`
    把别人的文件读进自己的库 —— 那不是路径穿越的小瑕疵，是**跨租户数据泄漏**。

    顺序有意义：**第一个命中即采用**，所以运维可以先列更窄的目录。
    """
    from ..common.config import get_settings

    raw = (get_settings().import_roots or "").strip()
    if not raw:
        # 默认只允许 `<data_dir>/imports` —— 要导入别处的文件必须显式配置
        return [(get_settings().data_path / "imports").resolve()]
    out: list[Path] = []
    for part in raw.split(","):
        p = Path(part.strip()).expanduser()
        if p:
            out.append(p.resolve())
    return out


def resolve_import_path(raw: str, *, expect: str = "any") -> Path:
    """把用户给的路径解析成**白名单内**的真实路径。

    @param expect `"file"` / `"dir"` / `"any"`。不符时报 400 而不是 404 ——
        路径在白名单内但类型不对，那是请求写错了。
    @return 解析后的绝对路径（`resolve()` 过，符号链接已展开）
    @raises InvalidRequest 不在白名单内 / 不存在 / 类型不对

    ⚠️ **必须 `resolve()` 之后再判前缀**：先判前缀再 resolve 会被
    `imports/../../../etc/passwd` 这种相对路径绕过。
    ⚠️ 前缀比较要用 `is_relative_to` 而不是 `str.startswith`：
    后者会把 `data/imports_evil/` 判成在 `data/imports` 里（踩过同类坑）。
    """
    if not (raw or "").strip():
        raise InvalidRequest("path 不能为空")

    roots = import_roots()
    try:
        cand = Path(raw).expanduser().resolve()
    except OSError as exc:
        raise InvalidRequest(f"路径无法解析：{raw}（{exc}）") from exc

    if not any(cand == r or cand.is_relative_to(r) for r in roots):
        allowed = "、".join(str(r) for r in roots)
        # ⚠️ 如实列出允许的目录：用户需要知道该把文件放哪，
        #   而"403 路径非法"这种回复只会让人反复试
        raise InvalidRequest(
            f"路径 {cand} 不在允许导入的目录内；允许：{allowed}"
            f"（配置项 IMPORT_ROOTS）"
        )

    if not cand.exists():
        # ⚠️ 用 422 而不是 `ResourceNotFound`（403 / KB_NOT_FOUND）：
        #   那个码是给"知识库/文档"的，拿它报"文件不存在"会让调用方
        #   以为自己没权限 —— 然后去申请权限，而真正的原因是路径写错了。
        raise ValidationFailed(f"路径不存在：{cand}")
    if expect == "file" and not cand.is_file():
        raise InvalidRequest(f"{cand} 不是文件")
    if expect == "dir" and not cand.is_dir():
        raise InvalidRequest(f"{cand} 不是目录")
    return cand


# ============================================================
# 聊天：读文件 + 切分 + 渲染（纯函数为主）
# ============================================================


def count_messages(path: Path) -> int:
    """数 jsonl 有多少**有效**行（请求时估 `estimated_docs` 用）。

    ⚠️ 流式逐行数，不整文件载入：`len(f.readlines())` 在 200MB 的导出上
    会把内存吃掉，而请求线程不该为一次估算付出几百 MB。
    空行不算消息（导出工具常在结尾留空行）。

    第一版写成"按 1MB 分块数换行符再数空行"，两处错：分块边界会把一行
    劈成两半（多算），而 `count(b"\n\n")` 在连续的 3 个换行上只扣 2 个
    （少算）。逐行读让 Python 处理边界，我们只判 `strip()`。
    """
    with path.open("rb") as f:
        return sum(1 for line in f if line.strip())


def iter_messages(path: Path) -> Iterator[dict[str, Any]]:
    """流式读 chat jsonl。**畸形行跳过并计数**，不让整批导入失败。

    ⚠️ 87k 行里坏掉 1 行不该让 87056 条消息白导 —— 但**必须报出来**，
    否则用户以为全导进去了。所以调用方拿得到 `bad_lines`。
    """
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                yield {"__malformed__": True}
                continue
            if not isinstance(obj, dict):
                yield {"__malformed__": True}
                continue
            yield obj


def is_malformed(msg: dict[str, Any]) -> bool:
    return bool(msg.get("__malformed__"))


def plan_chat_slices(
    messages: list[dict[str, Any]], messages_per_doc: int
) -> list[list[dict[str, Any]]]:
    """按条数切分（D41）。**纯函数**：切分边界错了会静默丢消息。

    ⚠️ 不按日期切：日期边界会产出"一天 3000 条"这种超大文档，
    也产出"一天 1 条"这种碎片（实测文档数从 666 变 1740）。
    按固定条数切 → 引用粒度稳定、删除粒度一致。
    """
    n = max(1, int(messages_per_doc))
    return [messages[i : i + n] for i in range(0, len(messages), n)]


def _fmt_ts(msg: dict[str, Any]) -> str:
    """毫秒时间戳 → `2024-07-21 20:45:02`（检索"那天"要用得到秒级）。

    ⚠️ 拿不到 `ts` 时回落到 `date` 字段（导出工具两种都可能有）。
    """
    ts = msg.get("ts")
    if isinstance(ts, (int, float)) and ts > 0:
        return dt.datetime.fromtimestamp(ts / 1000, dt.UTC).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    d = str(msg.get("date") or "").strip()
    return d or "日期未知"


def render_chat_slice(msgs: list[dict[str, Any]], index: int) -> tuple[str, str]:
    """渲染一个切片 → `(file_name, markdown 文本)`。

    ## 格式为什么长这样

    ```
    # 聊天记录切片 0001（2024-07-21 ~ 2024-07-23 · 50 条）

    [2024-07-21 20:45:02] 李四：王五 撤回了一条消息
    [2024-07-21 20:45:34] 张三：晚上去吃火锅？
    ```

    - **时间戳必须进正文**：`routes` 的 date 意图、"那天我们聊了什么"
      都靠它；只进文件名的话正文检索完全找不到时间（D71 的失败模式之一）。
    - **说话人必须进正文**：`me:` / `other:` 这种归一化不能丢 ——
      "我们第一次约会在哪"要靠"我/她"的位置判断。
      ⚠️ 但**不要**把 uid 写进正文（那是对话方的账号标识，属于 PII）。
    - 首行放日期范围：`/preview` 与标题都能给出上下文。

    @return `(file_name, text)`；file_name 形如 `chat-0001-20240721-20240723.md`
    """
    dates = [str(m.get("date") or "") for m in msgs if m.get("date")]
    lo = dates[0].replace("-", "") if dates else "unknown"
    hi = dates[-1].replace("-", "") if dates else lo
    name = f"chat-{index:04d}-{lo}-{hi}.md"

    lines = [f"# 聊天记录切片 {index:04d}（{lo} ~ {hi} · {len(msgs)} 条）", ""]
    for m in msgs:
        # 🔑 **uid 绝不进正文**：那是对话方的账号标识（PII），
        #   而正文的每一段都会被切块、嵌入、进索引。第一版拿 uid 兜底显示，
        #   于是"没有昵称的消息"把账号 ID 写进了可检索文本。
        #   缺昵称时用中性占位 —— 少一个说话人标识不影响检索。
        who = str(m.get("name") or "").strip() or "匿名"
        text = str(m.get("text") or "").replace("\r\n", "\n").strip()
        if not text:
            # 撤回/系统消息可能只有 type 没有 text —— 保留行，否则"撤回"
            # 这类事件会静默消失，用户以为记录不完整
            kind = str(m.get("type") or "")
            text = f"（{kind or '空消息'}）"
        lines.append(f"[{_fmt_ts(m)}] {who}：{text}")
    return name, "\n".join(lines) + "\n"


def slice_stats(msgs: list[dict[str, Any]]) -> dict[str, Any]:
    """一个切片的统计（写进 job payload，让进度可读）。"""
    names = [str(m.get("name") or "") for m in msgs]
    return {
        "messages": len(msgs),
        "first_date": str(msgs[0].get("date") or "") if msgs else "",
        "last_date": str(msgs[-1].get("date") or "") if msgs else "",
        "speakers": len({n for n in names if n}),
    }


# ============================================================
# 图片目录扫描
# ============================================================


def scan_dir(
    path: Path,
    *,
    recursive: bool,
    suffixes: frozenset[str],
    limit: int = 20_000,
) -> list[Path]:
    """列出目录里的匹配文件（按名字排序，保证切分可复现）。

    ⚠️ **按名字排序**：不排序的话 `os.listdir` 的顺序取决于文件系统，
    同一个目录两次导入产生不同的 doc 顺序与编号 —— 用户看不出区别，
    但"重跑一次"的 diff 会很大，排查问题时很痛苦。

    @param limit 硬上限。⚠️ 没有上限的话一个指向 `/usr` 的路径会
      让 worker 遍历几十万文件直到内存/时间爆掉（worker 只有一个）。
    """
    out: list[Path] = []
    if recursive:
        for root, dirs, files in os.walk(path):
            dirs.sort()          # 同上：让遍历顺序可复现
            for fn in sorted(files):
                if Path(fn).suffix.lower() in suffixes:
                    out.append(Path(root) / fn)
                    if len(out) >= limit:
                        return out
    else:
        for fn in sorted(os.listdir(path)):
            p = path / fn
            if p.is_file() and p.suffix.lower() in suffixes:
                out.append(p)
                if len(out) >= limit:
                    return out
    return out


__all__ = [
    "DEFAULT_MESSAGES_PER_DOC",
    "count_messages",
    "import_roots",
    "is_malformed",
    "iter_messages",
    "plan_chat_slices",
    "render_chat_slice",
    "resolve_import_path",
    "scan_dir",
    "slice_stats",
]