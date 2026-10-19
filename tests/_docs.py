"""测试共用的"读契约文本"小工具。

🔑 契约在 `docs/DESIGN-API*.md`（当前是一份；glob 兼容历史上拆成多份的形态）。
多个测试要 grep 契约里的某一行 —— 它们**必须读全部**匹配文件，否则契约一拆开，
那条 grep 就会"找不到"，症状是测试**假红**（或守卫**假绿**：grep 不到就跳过）。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def contract_text() -> str:
    """`docs/DESIGN-API*.md` 全文拼接（当前一份；glob 兼容拆成多份）。"""
    return "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted((ROOT / "docs").glob("DESIGN-API*.md"))
    )
