"""分层校验（D166）—— 仿文章的"格式 / 解析 / Chunking"三道关卡。

**纯函数**，不碰 DB、不加载模型，所以能直接单测每一条判据。

## 为什么要有它

审计（`DESIGN-HISTORY.md §2`）发现：项目只在**上传时**做了格式
白名单与大小检查，而"解析出空内容 / 乱码 / 切分异常"这些**没有任何检查** ——
一份解析出 0 个 chunk 的 docx 会走 `if not parsed` 被当成"扫描件"直接
`ready`，**静默入库一个空文档**。本模块把这三层补齐，结论进
`documents.quality`（JSONB）与日志。

## 保守原则（很重要）

- 格式嗅探**只在能确凿判断"这是另一种二进制"时**才报错。识别不出 / 看着像
  纯文本一律放行 —— 否则会把测试里的 fixture、奇怪的但合法的文件全挡掉。
- 解析/切分校验**只报告、尽量不阻断**：空内容 → `failed`（那是真的没内容），
  乱码率高 / 块大小异常 → 记进 `quality`，让用户看得见。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

# ============================================================
# 第一层：格式嗅探
# ============================================================

#: 后缀 → 期望的"家族"。家族名与 `_sniff` 的返回值对齐。
_SUFFIX_FAMILY: dict[str, str] = {
    ".pdf": "pdf",
    ".docx": "zip", ".xlsx": "zip", ".pptx": "zip",
    ".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".webp": "webp",
    ".bmp": "bmp", ".tiff": "tiff",
    ".md": "text", ".markdown": "text", ".txt": "text",
    ".csv": "text", ".json": "text", ".html": "text", ".htm": "text",
}

#: 二进制家族（用于"期望文本却是二进制"的判定）。
_BINARY_FAMILIES = frozenset({"pdf", "zip", "jpeg", "png", "webp", "bmp", "tiff"})


def _sniff(data: bytes) -> str | None:
    """看文件头判断真实类型。识别不出返回 `None`。"""
    if not data:
        return None
    head = data[:16]
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"BM"):
        return "bmp"
    if head.startswith(b"II*\x00") or head.startswith(b"MM\x00*"):
        return "tiff"
    if b"\x00" in data[:1024]:
        return "binary"      # 有 NUL 又不认识 → 二进制
    return None


def expected_family(suffix: str) -> str | None:
    return _SUFFIX_FAMILY.get((suffix or "").lower())


def format_mismatch(suffix: str, data: bytes) -> str | None:
    """扩展名与实际内容**明显**不符时返回原因，否则 `None`。

    ⚠️ 只在确凿时返回原因（见模块 docstring 的保守原则）：
    - 期望文本却嗅到二进制 ⇒ 报；
    - 期望某二进制家族，嗅到**另一种**二进制家族 ⇒ 报；
    - 嗅到 `None`（识别不出）/ `binary` 且期望二进制 ⇒ 放行（可能是少见的合法变体）。
    """
    expected = expected_family(suffix)
    if expected is None:
        return None
    detected = _sniff(data)
    if detected is None:
        return None
    if expected == "text":
        if detected in _BINARY_FAMILIES or detected == "binary":
            return f"扩展名是文本 {suffix}，内容却是二进制（头部识别为 {detected}）"
        return None
    if expected in _BINARY_FAMILIES and detected in _BINARY_FAMILIES and detected != expected:
        return f"扩展名 {suffix} 期望 {expected}，实际内容头部是 {detected}"
    return None


# ============================================================
# 第二层：解析结果校验
# ============================================================

#: 乱码率阈值（文章给的 5%）。
MAX_GARBLED_RATE = 0.05
#: 太短视为解析失败；太长需要分片（文章的口径）。
MIN_CONTENT_CHARS = 100
MAX_CONTENT_CHARS = 10_000_000


def garbled_rate(text: str) -> float:
    """乱码率：替换符（U+FFFD）与不可打印控制字符的占比。

    ⚠️ 换行/制表符不算乱码（它们是合法空白）。
    """
    if not text:
        return 0.0
    bad = 0
    for ch in text:
        if ch == "\ufffd":
            bad += 1
        elif unicodedata.category(ch) == "Cc" and ch not in "\n\r\t":
            bad += 1
    return bad / len(text)


@dataclass
class ParseReport:
    ok: bool = True
    garbled_rate: float = 0.0
    char_count: int = 0
    issues: list[str] = field(default_factory=list)
    #: `True` = 必须失败（空内容）；否则只是"要注意"。
    fatal: bool = False

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "fatal": self.fatal,
            "garbled_rate": round(self.garbled_rate, 4),
            "char_count": self.char_count,
            "issues": self.issues,
        }


def validate_parse(text: str) -> ParseReport:
    """解析结果体检。"""
    rep = ParseReport(char_count=len(text or ""))
    if not (text or "").strip():
        rep.ok = False
        rep.fatal = True
        rep.issues.append("解析结果为空（没有可检索内容）")
        return rep

    rep.garbled_rate = garbled_rate(text)
    if rep.garbled_rate > MAX_GARBLED_RATE:
        rep.ok = False
        rep.issues.append(f"乱码率 {rep.garbled_rate:.1%} 过高（> {MAX_GARBLED_RATE:.0%}）")

    if rep.char_count < MIN_CONTENT_CHARS:
        rep.ok = False
        rep.issues.append(f"内容过短（{rep.char_count} 字，可能解析失败）")
    if rep.char_count > MAX_CONTENT_CHARS:
        rep.ok = False
        rep.issues.append(f"内容过长（{rep.char_count} 字，需要分片处理）")
    return rep


# ============================================================
# 第三层：Chunking 质量
# ============================================================


@dataclass
class ChunkQuality:
    total: int = 0
    avg_size: float = 0.0
    min_size: int = 0
    max_size: int = 0
    stddev: float = 0.0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def acceptable(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "avg_size": round(self.avg_size, 1),
            "min_size": self.min_size,
            "max_size": self.max_size,
            "stddev": round(self.stddev, 1),
            "acceptable": self.acceptable,
            "errors": self.errors,
            "warnings": self.warnings,
        }


def chunk_quality(chunks: list, *, min_ok: int = 50, max_ok: int = 5000) -> ChunkQuality:
    """块大小分布体检（文章 §如何设计分层校验策略 的口径）。

    @param chunks 任何带 `.text` 的对象列表（不强绑 `chunker.Chunk`）。
    """
    sizes = [len(getattr(c, "text", "") or "") for c in chunks]
    q = ChunkQuality(total=len(sizes))
    if not sizes:
        q.errors.append("没有任何 chunk（解析成功却没有可索引内容）")
        return q

    q.min_size = min(sizes)
    q.max_size = max(sizes)
    q.avg_size = sum(sizes) / len(sizes)
    var = sum((s - q.avg_size) ** 2 for s in sizes) / len(sizes)
    q.stddev = var ** 0.5

    if q.min_size < min_ok:
        q.errors.append(f"存在过小的 chunk（{q.min_size} 字 < {min_ok}），可能切分异常")
    if q.max_size > max_ok:
        q.warnings.append(f"存在过大的 chunk（{q.max_size} 字 > {max_ok}），可能超出模型上下文")
    if q.avg_size and q.stddev > q.avg_size * 0.5:
        q.warnings.append("chunk 大小分布不均匀（标准差 > 均值的一半）")
    return q


__all__ = [
    "MAX_GARBLED_RATE",
    "ChunkQuality",
    "ParseReport",
    "chunk_quality",
    "expected_family",
    "format_mismatch",
    "garbled_rate",
    "validate_parse",
]
