"""🔑 视觉页渲染的**长边封顶**：大页面不许把 pymupdf 顶死。

## 缺口是怎么撞出来的

给验证脚本做"自动清场"之后重跑，它**从 13 项全绿变成 4 项红**：
`embed_visual` 报 `FzErrorLimit: code=5: Overly large image`，
扫描件文档永远停在 `pending`。看起来像"视觉腿坏了"。

真因有两条，**都不是视觉腿本身**：

1. 验证脚本的素材源是 `sorted(blob 路径)` 的第一个 PDF ——
   而 blob 按 sha 命名，于是它把**自己上一轮的产物**当素材。
   页面尺寸每轮**翻倍**：A4 1075pt → 2240 → 4667 → 9723。
2. 🔴 **产品侧的真缺陷**：`_visual_page_image` 固定 150dpi、**不封顶**，
   于是 9723×13730pt 的页面要光栅化成 20256×28642px ≈ **1.74GB**，
   pymupdf 直接拒绝。而"工程图纸 / 海报 / CAD 导出"这种
   135×190 英寸的页面在真实世界里很常见。

## 为什么这条是"免费"的

colSmol 内部恒定把长边重采样到 2048px 再切 512×512 图块
（实测 400×300 与 3000×4000 都是 ~862 token）。
所以渲染出大于 2048 的图**一个 token 都多不出来**，只是白占内存。
实测（真数据，不是推算）：

| 页面 | 渲染结果 | 多向量 |
|---|---|---|
| 1075×1520pt | 1451×2048px | **871×128** |
| 9723×13730pt（之前炸） | 1451×2048px | **871×128** |

封顶之后两者**完全一致** —— 所以这是纯安全阀，不是降质。
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _scale(w: float, h: float) -> float:
    from services.worker.handlers import _visual_render_scale

    return _visual_render_scale(w, h)


# ============================================================
# 1. 封顶本身
# ============================================================


@pytest.mark.parametrize(
    ("w", "h", "note"),
    [
        (595, 842, "A4"),
        (1075, 1520, "本项目扫描件常见尺寸"),
        (2240, 3163, "2×"),
        (4667, 6590, "4×"),
        (9723, 13730, "8× —— 第一版在这里炸"),
        (20000, 28280, "10× —— 海报/CAD"),
        (543_000, 543_000, "荒谬尺寸也不许溢出"),
    ],
)
def test_render_long_side_is_always_capped(w: float, h: float, note: str):
    """🔑 渲染出来的长边**永远** ≤ `VISUAL_MAX_LONG_SIDE_PX`。"""
    from services.worker.handlers import VISUAL_MAX_LONG_SIDE_PX

    scale = _scale(w, h)
    long_px = max(w, h) * scale
    assert long_px <= VISUAL_MAX_LONG_SIDE_PX + 0.01, (
        f"{note}: 长边 {long_px:.0f}px 超过上限 "
        f"{VISUAL_MAX_LONG_SIDE_PX}px —— pymupdf 会报 Overly large image"
    )


def test_small_pages_still_get_150dpi():
    """🔑 封顶**不能**反过来把正常页面渲小。

    判据是"小页面仍然按 150dpi"：如果实现写成
    `scale = MAX / long_pt`（不取 min），A4 就只剩 1754px… 看似还行，
    但 300×300 的小图会被**放大**到 2048px —— 白做功还可能变形。
    """
    assert _scale(595, 842) == pytest.approx(150 / 72), "A4 必须按 150dpi 渲"


def test_scale_is_taken_as_the_minimum_of_dpi_and_cap():
    """🔑 `min(150/72, 2048/long_pt)` —— 两个约束都要在。"""
    from services.worker.handlers import VISUAL_MAX_LONG_SIDE_PX

    for w, h in ((595, 842), (9723, 13730)):
        assert _scale(w, h) == pytest.approx(
            min(150 / 72, VISUAL_MAX_LONG_SIDE_PX / max(w, h))
        )


def test_degenerate_page_does_not_divide_by_zero():
    """🔑 畸形 PDF（页面尺寸 0）不许抛 `ZeroDivisionError`。

    返回 0 会让 pymupdf 自己报错，那比返回一个假图**更容易定位**
    （§5.57：失败要指向真实原因）。

    ⚠️ 注意判据是"**不抛异常、且结果有限**"，**不是**"一律返回 0"：
    `0×842` 的页面长边仍然是 842，所以按 150dpi 渲染是**对的**。
    第一版我把这里写成 `_scale(0, 842) == 0.0`，于是把对的实现判成失败
    —— 与 §5.108 同一个形状：断言指错了性质。
    """
    import math

    assert _scale(0, 0) == 0.0, "两维都为 0 时返回 0（让 pymupdf 去报错）"
    for w, h in ((0, 842), (842, 0), (0, 0)):
        got = _scale(w, h)
        assert math.isfinite(got) and got >= 0, f"{w}×{h} → {got}"


# ============================================================
# 2. 真的接进渲染路径（不能只留一个没人调用的辅助函数）
# ============================================================


def test_page_image_uses_the_scale_helper():
    """🔑 `_visual_page_image` 必须**调用**那个辅助函数。

    ⚠️ 这条存在的理由：抽了个 helper 却忘了接线，是本项目踩过的形状
    （`_wait_queue_idle` 的返回值没人用、§5.109）。
    """
    import ast

    src = (ROOT / "services" / "worker" / "handlers" / "__init__.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(src)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_visual_page_image")
    body = ast.unparse(fn)
    assert "_visual_render_scale(" in body, (
        "_visual_page_image 没有用 _visual_render_scale —— helper 是死的"
    )
    # 而且不该再有硬编码的 150/72
    assert "150 / 72" not in body.replace("(150/72)", ""), (
        "_visual_page_image 里还有硬编码的 150/72 —— 封顶被绕过了"
    )


def test_constant_is_2048_matching_colsmol_internal_target():
    """🔑 上限取 **2048**，因为那是 colSmol 内部的重采样目标。

    取别的值（更大 = 白占内存；更小 = 真的降质）都不对。
    这条把"为什么是 2048"钉在代码里，而不是只在注释里说说。
    """
    from services.worker.handlers import VISUAL_MAX_LONG_SIDE_PX

    assert VISUAL_MAX_LONG_SIDE_PX == 2048


def test_a_1_74gb_raster_is_never_requested():
    """🔑 用第一版会炸的那个尺寸，断言**光栅化预算**在合理范围内。

    1.74GB 是实测数字（9723×13730pt @150dpi = 20256×28642px × 3 通道）。
    """
    w, h = 9723, 13730
    scale = _scale(w, h)
    px_w, px_h = w * scale, h * scale
    gb = px_w * px_h * 3 / 1e9
    assert gb < 0.05, f"仍要 {gb:.2f}GB 的光栅化内存（第一版是 1.74GB）"
