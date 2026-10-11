"""文档预览与页图渲染（L3 前端依赖）。

三个能力：
1. `render_page()` —— 页渲染成图片（前端拿它当底图，叠矩形框）
2. `render_chunk_overlay()` —— **直接把 chunk 的框画在页图上**（调试/验证用）
3. `preview_for()` —— 决定一份文档给前端什么预览源

D33/D55~D60 的坐标约定见 `CONVENTIONS.md` §6。

⚠️ 画框前必须把归一化坐标乘回 **point** 空间：
`pymupdf` 的 `Shape.draw_rect()` 要 point，不是像素。
传"归一化 × 像素宽"会让框整体下移约半行（预研实际踩过）。
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any

import pymupdf

from .locations import denormalize_bbox

logger = logging.getLogger(__name__)

#: 前端默认请求的页图宽度（D36 缩略图是 400px，这里是整页大图）
DEFAULT_WIDTH = 1200
#: 缩略图宽度（D36：入库时预生成，宽 400px jpg）
THUMB_WIDTH = 400


def render_page(
    pdf_path: Path | str,
    page_no: int,
    *,
    width: int = DEFAULT_WIDTH,
    as_jpeg: bool = True,
) -> bytes:
    """渲染单页为图片字节。

    @param page_no 1-based
    @param width 目标宽度（像素）。高度按页面宽高比自动。
    """
    doc = pymupdf.open(pdf_path)
    try:
        if not (1 <= page_no <= doc.page_count):
            raise IndexError(f"页码 {page_no} 超出范围 1~{doc.page_count}")
        page = doc[page_no - 1]
        zoom = width / page.rect.width if page.rect.width else 1.0
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        return _to_bytes(pix, as_jpeg)
    finally:
        doc.close()


def render_page_with_boxes(
    pdf_path: Path | str,
    page_no: int,
    locations: list[dict[str, Any]],
    *,
    width: int = DEFAULT_WIDTH,
    color: tuple[float, float, float] = (1.0, 0.1, 0.1),
    as_jpeg: bool = True,
) -> bytes:
    """渲染单页并**把框画上去**。

    这是验证 L3 坐标正确性最直接的手段：把
    `locations` 直接喂进来，看框有没有套住原文。
    """
    doc = pymupdf.open(pdf_path)
    try:
        page = doc[page_no - 1]

        shape = page.new_shape()
        drawn = 0
        for loc in locations:
            if loc.get("page") != page_no:
                continue
            bbox = loc["bbox"]
            if not _valid_bbox(bbox):
                continue
            # ⚠️ 关键：归一化 → point，不是像素
            shape.draw_rect(pymupdf.Rect(*denormalize_bbox(bbox, page)))
            shape.finish(color=color, width=1.2)
            drawn += 1
        if drawn:
            shape.commit(overlay=True)

        zoom = width / page.rect.width if page.rect.width else 1.0
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        return _to_bytes(pix, as_jpeg)
    finally:
        doc.close()


def _valid_bbox(bbox: list[float]) -> bool:
    return (
        len(bbox) == 4
        and bbox[0] <= bbox[2]
        and bbox[1] <= bbox[3]
        and all(0.0 <= v <= 1.0 for v in bbox)
    )


def _to_bytes(pix: pymupdf.Pixmap, as_jpeg: bool) -> bytes:
    buf = io.BytesIO()
    if as_jpeg:
        pix.pil_save(buf, format="JPEG", quality=85, optimize=True)
    else:
        pix.pil_save(buf, format="PNG")
    return buf.getvalue()


def preview_source(mime_type: str | None, *, has_preview_pdf: bool) -> str | None:
    """决定一份文档给前端什么预览源。

    D34 分两阶段：v1 支持 PDF/图片/MD/聊天，Office 走 LibreOffice 转 PDF（v1.1）。

    @return 预览源类型：`"pdf"` / `"image"` / `"text"` / `None`（无法预览）
    """
    mt = (mime_type or "").lower()
    if mt == "application/pdf":
        return "pdf"
    if mt.startswith("image/"):
        return "image"
    if has_preview_pdf:
        # Office 转出的 PDF。⚠️ 排版会变，UI 要标注"预览为转换版本"（D34）
        return "pdf"
    if mt.startswith("text/") or mt in ("text/markdown",):
        return "text"
    return None
