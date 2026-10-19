"""守卫：**已跟踪文件里不许出现真实信息**。

## 规矩

测试 / 验收脚本**不许**内嵌真实数据（姓名、证件号、私人文档片段…）。
需要真实数据的地方，一律从**不被追踪**的文件读：

- `eval/_qset.py`（真实语料的人工标注问题集）
- `eval/_local_data.py`（真实探针 PDF / 问题 / 原文标记）
- `reports/`（回放产物）
- `.env` / `data/`（运行期数据与语料）

## 这个守卫怎么工作

1. 🔑 **永远生效**：那些"只该在本地"的路径**不能**出现在 `git ls-files` 里
   （它们在 `.gitignore` 里）。
2. **本机生效**：若存在 `tests/_real_data_denylist.txt`（**它本身不被追踪**，里面
   每行一个真实词），就扫描**所有已跟踪文本文件**，命中即失败。
   CI 没有这份清单 ⇒ 这一步 skip（清单是个人数据，不该进仓库）；
   但第 1 步在 CI 上照样跑。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DENYLIST = Path(__file__).resolve().parent / "_real_data_denylist.txt"

#: 🔴 只该在本地的路径（含真实语料/真实值）。它们出现在 `git ls-files` 里就是泄露。
LOCAL_ONLY = (
    "eval/_qset.py",
    "eval/_local_data.py",
    "tests/_real_data_denylist.txt",
)

#: 二进制/大文件不必读（按扩展名跳过）。
_SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".woff", ".woff2",
    ".ttf", ".ico", ".bundle", ".pyc", ".lock",
}


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return [line for line in out.stdout.splitlines() if line.strip()]


def test_local_only_paths_are_not_tracked():
    """🔑 含真实语料/真实值的路径**不能**被 git 跟踪（`.gitignore` 兜底）。"""
    tracked = set(_tracked_files())
    leaked = sorted(p for p in LOCAL_ONLY if p in tracked)
    assert not leaked, (
        f"这些路径被 git 跟踪了（含真实信息，只该留本地）：{leaked}\n"
        "把它们加进 .gitignore，并 `git rm --cached`。"
    )
    # `reports/` 是目录：匹配任意前缀。
    assert not any(t.startswith("reports/") for t in tracked), (
        "reports/ 下有被跟踪的文件（回放产物可能含真实答案）"
    )


def _read_denylist() -> list[str]:
    terms: list[str] = []
    for line in DENYLIST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            terms.append(line)
    return terms


def test_no_denylisted_real_data_in_tracked_files():
    """🔑 本机 deny-list 里的真实词**不许**出现在任何已跟踪文件里。

    ⚠️ 清单本身（`tests/_real_data_denylist.txt`）**不进仓库** —— 它是个人数据。
    没有它（比如 CI）就 skip；那不代表"没问题"，只代表"这一步没测"。
    """
    if not DENYLIST.exists():
        pytest.skip(
            "没有 tests/_real_data_denylist.txt（个人真实词清单，不进仓库）"
        )
    terms = _read_denylist()
    if not terms:
        pytest.skip("deny-list 是空的")

    hits: list[str] = []
    for rel in _tracked_files():
        if Path(rel).suffix.lower() in _SKIP_SUFFIXES:
            continue
        try:
            text = (ROOT / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        for t in terms:
            if t in text:
                hits.append(f"{rel}: {t!r}")

    assert not hits, (
        "已跟踪文件里出现了 deny-list 里的真实信息：\n  "
        + "\n  ".join(hits[:20])
        + "\n把真实值移到不被追踪的文件（见本模块 docstring），别内嵌。"
    )
