"""🔑 Office → PDF 转换（D34 v1.1）。

## 为什么转换结果放在 blob 目录里而不是临时文件

`data/blobs/{sha}/preview.pdf`：转换是**确定性的**（同一份文件转两次结果一样），
按内容寻址存就能复用 —— 5 个人上传同一份模板，只有一个人付 0.7s 的转换成本，
而且转换结果不会因为"临时目录被清"而消失。

## ⚠️ 转换会改变排版（这是 D34 明确接受的代价）

LibreOffice 换字体/重排表格，所以转换版的页码、坐标与原件**不一致**。
前端必须标注"预览为转换版本"（`preview_source()` 的注释也这么写）。

🔑 **D131：这个模块仍然只管"转换"，但坐标问题已经有结论了**
（`deploy/verify_l3_office.py` 实测：Office chunk 本来就没有坐标，
所以"迁移原件坐标"不存在；改成**在转换版上重新定位**，
平均覆盖 99%）。抽取发生在 worker 的 `KIND_PDF_PREVIEW` handler 里
（转换产物那时才存在），定位器是 `ingest/relocate.py`。

⚠️ 而"重排导致框指错字"这个风险**不存在**了：用户看到的页图就是这份 PDF，
文本与坐标来自**同一次渲染**。

## 超时与内存

实测 0.713s/份（docx）。但**一份 300 页的 pptx 可能要 30s+**，
所以给硬超时（默认 120s），超了当失败（可重试）而不是把 worker 占死。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

#: LibreOffice 可执行名（发行版不同；`soffice` 是更常见的名字）
SOFFICE = "soffice"

#: 转换超时（秒）。实测 0.7s/份，但 300 页 pptx 可能 30s+；
#: 没有超时的话一个畸形文件能把 worker 挂住。
CONVERT_TIMEOUT = 120

#: LibreOffice 需要一个**独立 profile**，否则并发转换会互相抢锁：
#: 第二个进程会静默退出（0 退出码但没有产物）—— 症状是"偶尔转换失败"。
#:
#: ⚠️ **模板里不能再写 `file://`**：`Path.as_uri()` 自己就带 scheme，
#:   拼出来是 `file://file:///tmp/...` —— 那个 URL LibreOffice 无法解析，
#:   进程**不报错也不退出**，一直等到我们的超时（本项目实测整整 120s）。
#:   而 shell 里手打同一条命令 1 秒就转完了，所以这个 bug 极难定位：
#:   命令行看起来完全一样，只有双 scheme 这一点不同。
_PROFILE_ARG = "-env:UserInstallation={profile}"

#: 这些扩展名走 LibreOffice（PDF 不需要，它本身就是）。
#:
#: ⚠️ **刻意是 `parsers.PARSERS` 的超集**，这不是第二份格式白名单（D140）：
#: 它回答的是"**LibreOffice 能转哪些**"，而不是"我们接受上传哪些"。
#: `.doc` `.xls` `.ppt` `.odt` `.ods` `.odp` `.rtf` 目前**不在**上传白名单里
#: （所以走不到转换），但它们是 soffice 的真实能力；哪天开放其中一种，
#: 转换路径**不需要**再改。
#: 🔑 两者的关系由 `tests/test_parser_registry.py` 钉住：
#: "PARSERS 里的 Office 格式 ⊆ OFFICE_SUFFIXES，且后者严格更大"。
OFFICE_SUFFIXES = frozenset({".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt",
                             ".odt", ".ods", ".odp", ".rtf"})


class ConversionError(RuntimeError):
    """转换失败（可重试：换台机器/换个 LibreOffice 可能就成功）。"""


def soffice_available() -> str | None:
    """返回可执行名；没有则 `None`（调用方据此如实报 skipped）。"""
    for name in (SOFFICE, "libreoffice"):
        path = shutil.which(name)
        if path:
            return path
    return None


def convert_to_pdf(
    src: Path, dest: Path, *, timeout: int = CONVERT_TIMEOUT
) -> Path:
    """Office 文件 → PDF，写到 `dest`。已存在则**直接复用**。

    @return `dest`
    @raises ConversionError 没装 LibreOffice / 超时 / 产物缺失
    """
    if dest.exists() and dest.stat().st_size > 0:
        return dest          # 内容寻址 → 转一次就够（见模块 docstring）

    exe = soffice_available()
    if exe is None:
        raise ConversionError(
            "没装 LibreOffice（Office 预览需要它）："
            "apt install libreoffice-writer libreoffice-impress libreoffice-calc"
        )
    if not src.exists():
        raise ConversionError(f"源文件不存在：{src}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pkh-lo-") as tmp:
        outdir = Path(tmp) / "out"
        profile = Path(tmp) / "profile"
        outdir.mkdir()
        cmd = [
            exe,
            _PROFILE_ARG.format(profile=profile.as_uri()),
            "--headless", "--norestore", "--nolockcheck",
            "--convert-to", "pdf", "--outdir", str(outdir), str(src),
        ]
        try:
            proc = subprocess.run(   # noqa: S603 — 命令是固定拼装的
                cmd, capture_output=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise ConversionError(
                f"LibreOffice 转换超时（{timeout}s）：{src.name}"
            ) from exc

        produced = outdir / f"{src.stem}.pdf"
        if not produced.exists() or produced.stat().st_size == 0:
            # ⚠️ 报错要带 stderr 摘要：LibreOffice 的失败原因只在 stderr 里，
            #   而"转换失败"这四个字会让人以为是文件坏了
            tail = (proc.stderr or b"").decode("utf-8", "replace").strip()[-300:]
            raise ConversionError(
                f"LibreOffice 没产出 PDF（exit={proc.returncode}）："
                f"{src.name}{'；stderr: ' + tail if tail else ''}"
            )
        shutil.copyfile(produced, dest)
    logger.info("Office → PDF：%s → %s（%d KB）",
                src.name, dest.name, dest.stat().st_size // 1024)
    return dest


def needs_conversion(file_name: str, mime_type: str | None) -> bool:
    """这份文档要不要走转换。

    ⚠️ 判据看 **`file_name`** 而不是 `path.suffix` —— blob 路径是
    `{sha}/original`，**没有扩展名**（内容寻址刻意不保留原文件名）。
    看错会导致**所有**文档都判成"不用转"，症状是"Office 文档还是没有预览"。
    （同一个根因炸过两处，见 CONVENTIONS §0.8。）
    """
    name = (file_name or "").lower()
    if name.endswith(".pdf"):
        return False        # 本身就是 PDF
    if name.rsplit(".", 1)[-1:] and any(
        name.endswith(s) for s in OFFICE_SUFFIXES
    ):
        return True
    # ⚠️ 没有扩展名时退回 mime 判据（数据库里 mime 也可能为空）
    mt = (mime_type or "").lower()
    return mt in (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
    )


__all__ = [
    "CONVERT_TIMEOUT",
    "OFFICE_SUFFIXES",
    "ConversionError",
    "convert_to_pdf",
    "needs_conversion",
    "soffice_available",
]