"""🔑 Office 预览（D34 v1.1）：转换判据 / 命令行 / 预览源 / 头编码。

本轮的真实 bug 几乎全在这一层，且**都不在 happy path 上**：

| bug | 症状 |
|---|---|
| `-env:UserInstallation=file://` + `as_uri()` | `file://file:///…` → soffice **不报错也不退出**，卡到超时（120s）；而 shell 手打同一条命令 1 秒就完 |
| 判据看 `path.suffix` | blob 路径是 `{sha}/original`（无扩展名）→ 全部判成"不用转" |
| `Content-Disposition` 里放中文名 | HTTP 头是 latin-1 → `UnicodeEncodeError` → **整个响应 500**，且只在中文名下出现 |
| `if doc.status == "ready"` 守卫 | `status` 在几行之后才赋值 → 守卫是**死的** → 从不派生预览任务（D36 同类） |
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.api.routers.documents import (
    _inline_disposition,
    _is_image,
    mime_type_for,
)
from services.ingest.office import (
    OFFICE_SUFFIXES,
    ConversionError,
    needs_conversion,
    soffice_available,
)

ROOT = Path(__file__).resolve().parents[1]


# ============================================================
# 1. 转换判据
# ============================================================


@pytest.mark.parametrize(
    "name",
    ["a.docx", "a.xlsx", "a.pptx", "张三-申请书.docx", "A.DOCX", "a.odt"],
)
def test_office_needs_conversion(name):
    assert needs_conversion(name, None) is True


def test_pdf_does_not_need_conversion():
    """PDF 本身就是 PDF，转它只会白白跑 0.7s。"""
    assert needs_conversion("a.pdf", "application/pdf") is False


@pytest.mark.parametrize("name", ["a.jpg", "a.png", "a.webp", "a.txt", "a.md"])
def test_non_office_does_not_need_conversion(name):
    assert needs_conversion(name, None) is False


def test_judges_by_file_name_not_blob_path():
    """🔑 blob 路径是 `{sha}/original`，**没有扩展名**（内容寻址不保留原名）。

    看 `path.suffix` 的症状：所有 Office 文档都判成"不用转"，
    表现为"Office 文档还是没有预览"（CONVENTIONS §0.8 记的同一个根因）。
    """
    blob_like = "/data/blobs/12/12b63d…/original"
    assert needs_conversion("张三-申请书.docx", None) is True
    # 传一个"无扩展名路径"当 file_name 时不能崩
    assert needs_conversion("", None) is False
    assert isinstance(needs_conversion(blob_like, None), bool)


def test_mime_fallback_when_no_extension():
    """没有扩展名时退回 mime 判据（导出工具可能不给后缀）。"""
    assert needs_conversion(
        "no-extension", "application/vnd.openxmlformats-officedocument"
                        ".wordprocessingml.document"
    ) is True


def test_office_suffixes_contains_only_office():
    """`.pdf` 不该在里面（它不需要转换）。"""
    assert ".pdf" not in OFFICE_SUFFIXES
    assert {".docx", ".xlsx", ".pptx"} <= OFFICE_SUFFIXES


# ============================================================
# 2. LibreOffice 调用
# ============================================================


def test_profile_arg_has_no_double_scheme():
    """🔑 `-env:UserInstallation` 的值来自 `Path.as_uri()`（自带 `file://`）。

    模板里再写一次 `file://` 就拼成 `file://file:///tmp/...`，
    那个 URL LibreOffice **无法解析，且不报错也不退出** ——
    表现是"转换卡到超时"，而 shell 里手打同一条命令 1 秒就完。
    """
    from services.ingest.office import _PROFILE_ARG

    assert _PROFILE_ARG == "-env:UserInstallation={profile}"
    uri = Path("/tmp/pkh-lo-x/profile").as_uri()
    assert _PROFILE_ARG.format(profile=uri).count("file://") == 1


def test_headless_and_norestore_flags_present():
    """缺 `--headless` 会弹界面卡住；缺 `--norestore` 会被上次崩溃的
    恢复流程挡住。两者都在源码里钉住，免得"清理一下命令"时顺手删掉。"""
    src = (ROOT / "services" / "ingest" / "office.py").read_text(encoding="utf-8")
    assert '"--headless"' in src
    assert '"--norestore"' in src
    # 独立 profile：否则并发转换互相抢锁，第二个静默失败
    assert "-env:UserInstallation" in src


def test_missing_file_raises_conversion_error(tmp_path):
    from services.ingest.office import convert_to_pdf

    if soffice_available() is None:
        pytest.skip("本机没装 LibreOffice")
    with pytest.raises(ConversionError):
        convert_to_pdf(tmp_path / "nope.docx", tmp_path / "out" / "preview.pdf")


def test_existing_preview_is_reused(tmp_path):
    """🔑 内容寻址 → 转一次就够，重复导入不该再付 0.7s。"""
    from services.ingest.office import convert_to_pdf

    dest = tmp_path / "preview.pdf"
    dest.write_bytes(b"%PDF-1.7 already there")
    # 源文件不存在也不报错 —— 说明确实走了"已存在就复用"的分支
    assert convert_to_pdf(tmp_path / "gone.docx", dest) == dest
    assert dest.read_bytes().startswith(b"%PDF")


def test_conversion_timeout_is_bounded():
    """必须有超时：没有它一个畸形文件能把 worker 挂住。"""
    from services.ingest.office import CONVERT_TIMEOUT

    assert 10 <= CONVERT_TIMEOUT <= 600


# ============================================================
# 3. 预览源选择（三个端点共用）
# ============================================================


def test_is_image_suffixes():
    for n in ("a.jpg", "a.JPEG", "a.png", "a.webp", "a.bmp", "a.tiff"):
        assert _is_image(n) is True
    for n in ("a.pdf", "a.docx", "a.txt", ""):
        assert _is_image(n) is False


def test_mime_type_for_uses_mimetypes():
    """复用 `guess_mime` 而不是自己维护映射表 ——
    自己那份必然漏项，症状是浏览器把 PDF 当下载而不是内联预览。"""
    assert mime_type_for("a.pdf") == "application/pdf"
    assert mime_type_for("a.jpg") == "image/jpeg"
    # 未知后缀要有兜底，不能返回 None（Header 赋 None 会报错）
    assert mime_type_for("weird.zzz").endswith("octet-stream")


# ============================================================
# 4. 中文文件名的响应头
# ============================================================


def test_disposition_encodes_non_ascii():
    """🔑 HTTP 头是 latin-1 的 —— 中文文件名直��放进去会 `UnicodeEncodeError`
    → **整个响应 500**，而且只在中文名下出现（英文名测试全绿）。"""
    v = _inline_disposition("张三-申请书.docx")
    assert "filename*=UTF-8''" in v
    assert "%E5%BC%A0" in v, "中文名要 percent-encode"
    # 整体必须是纯 latin-1（能被 HTTP 头编码）
    v.encode("latin-1")


def test_disposition_keeps_ascii_fallback():
    """老客户端只认 `filename=`，所以 ASCII 回退也要给。"""
    v = _inline_disposition("report.pdf")
    assert 'filename="report.pdf"' in v
    assert "filename*=UTF-8''report.pdf" in v


def test_disposition_escapes_quotes():
    """文件名里带引号会破坏头结构（头注入）。"""
    v = _inline_disposition('a"b.pdf')
    assert 'a_b.pdf' in v
    v.encode("latin-1")


def test_disposition_empty_name():
    v = _inline_disposition("")
    assert "preview" in v
    v.encode("latin-1")


# ============================================================
# 5. handler 注册与接线
# ============================================================


def test_all_kinds_have_handlers():
    """`pdf_preview` 曾是**唯一**没有 handler 的 kind（已知缺口）。"""
    from services.common.jobs import ALL_KINDS
    from services.worker.handlers import HANDLERS

    assert set(ALL_KINDS) - set(HANDLERS) == set(), (
        f"这些 kind 没有 handler：{sorted(set(ALL_KINDS) - set(HANDLERS))}"
    )


def test_pipeline_enqueues_preview_without_a_dead_guard():
    """🔑 不能有 `if doc.status == "ready"` 守卫。

    `doc.status = "ready"` 在入队那段**之后**才赋值，所以那个守卫是死的
    → 从不派生预览任务。症状与 D36 的"handler 从没人入队"完全一样：
    代码写好了、功能没有任何反应。
    """
    src = (ROOT / "services" / "ingest" / "pipeline.py").read_text(encoding="utf-8")
    i = src.index("KIND_PDF_PREVIEW")
    window = src[max(0, i - 600):i]
    assert 'doc.status == "ready"' not in window, (
        "入队 pdf_preview 之前有 `doc.status == 'ready'` 守卫 —— 它在该赋值之前，是死的"
    )


def test_pipeline_imports_the_kind():
    """⚠️ 忘导入常量会被 `except Exception` 吞成一条 warning ——
    摄取照常完成、预览永远不生成（本项目真踩过）。"""
    src = (ROOT / "services" / "ingest" / "pipeline.py").read_text(encoding="utf-8")
    import_block = src[: src.index("\n\n", src.index("from ..common.jobs"))]
    assert "KIND_PDF_PREVIEW" in import_block, "KIND_PDF_PREVIEW 没有导入"


def test_broad_except_around_enqueue_is_narrowed():
    """判据那段只能吞 `ImportError`/`OSError`；吞 `Exception` 会把
    `NameError`（我写错了）伪装成"环境问题"。"""
    src = (ROOT / "services" / "ingest" / "pipeline.py").read_text(encoding="utf-8")
    i = src.index("needs_conversion")
    window = src[i: i + 700]
    # ⚠️ **只看代码行**：本文件的注释里就写着 `except Exception`（记录旧 bug），
    #   按原文匹配会被自己的注释绊倒 —— 第一版就是这么失败的。
    code = "\n".join(
        ln for ln in window.splitlines() if not ln.lstrip().startswith("#")
    )
    assert "except (ImportError, OSError)" in code, (
        "这段的 except 必须收窄 —— 宽 except 把代码错误伪装成环境问题"
    )
    assert "except Exception" not in code


def test_preview_failure_does_not_fail_the_document():
    """转换失败**只**标任务失败，不动文档状态（否则用户以为没导入成功）。"""
    src = (ROOT / "services" / "worker" / "handlers" / "__init__.py").read_text(
        encoding="utf-8"
    )
    start = src.index("def handle_pdf_preview(")
    body = src[start: start + 2200]
    assert "raise IngestError" in body
    # 不允许出现 doc.status = "failed"
    assert 'doc.status = "failed"' not in body