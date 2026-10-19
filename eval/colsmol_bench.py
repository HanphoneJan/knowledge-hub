# -*- coding: utf-8 -*-
"""colSmol 视觉编码提速实验 —— 为扫描件规模化决策提供数据。

⚠️ **D124 之后的结论**（本脚本当年的时间点已过时，数字见下）：
- 加载必须用 `MultiVectorEncoder`（本脚本一直是对的，坏的是 `services/`）
- 单图成本 **≈15s**（**float32**）；跟着 checkpoint 默认的 bf16 跑是 **68s**
  —— 本机 CPU 无 `avx512_bf16`，bf16 矩阵乘是软件模拟
- **成本与分辨率无关**：Idefics3 恒定把长边重采样到 2048px 再切
  512×512 图块（≈13 块 / 862 token）—— 所以下面实验 1 的结论是
  "输入尺寸不影响 token 数"，DPI 也一样没用

要回答的问题：
  1. 输入图片尺寸变化会不会影响输出 token 数？（已答：不影响）
  2. 截断输出序列能提速多少？代价是什么？
  3. batch size 对总吞吐的影响？

产出 /tmp/colsmol_bench.log
"""
import os
import time
from pathlib import Path

GEN_NAME = "多页表单模板.pdf"

for _k in ("ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

from PIL import Image  # noqa: E402
from sentence_transformers import MultiVectorEncoder  # noqa: E402
import pymupdf  # noqa: E402

MODEL = "vidore/colSmol-500M"
# 取一张真实扫描页
PDF = os.environ.get("PKH_PROBE_PDF", f"data/probe/{GEN_NAME}")


def log(*a):
    print(*a, flush=True)


def main():
    t0 = time.time()
    model = MultiVectorEncoder(MODEL)
    log(f"[setup] 模型加载 {time.time()-t0:.1f}s")

    doc = pymupdf.open(PDF)
    page = doc[0]
    log(f"[setup] 源 PDF 页尺寸 {page.rect}")

    # ---------- 实验 1：输入尺寸 vs 输出 token 数 ----------
    log("\n=== 实验1：输入图片尺寸 → 输出 token 数 / 耗时 ===")
    log(f"{'边长':<8}{'输入像素':<14}{'输出tokens':<12}{'耗时(s)':<10}{'tok/s'}")
    results = {}
    for size in (448, 672, 896, 1120, 1344):
        dpi_scale = size / max(page.rect.width, page.rect.height)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(dpi_scale, dpi_scale))
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        t0 = time.time()
        d = model.encode_document([img])
        dt = time.time() - t0
        ntok = d[0].shape[0]
        results[size] = (ntok, dt)
        log(f"{size:<8}{f'{pix.width}x{pix.height}':<14}{ntok:<12}{dt:<10.1f}{ntok/dt:.0f}")

    # ---------- 实验 2：截断输出序列 ----------
    log("\n=== 实验2：截断输出 token 数 → 耗时 ===")
    pix = page.get_pixmap(dpi=96)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    d = model.encode_document([img])
    full = d[0]
    log(f"完整输出 shape={tuple(full.shape)}")
    log(f"{'保留tokens':<14}{'比例':<10}{'理论耗时':<12}")
    base = results.get(896, (None, None))[1]
    for keep in (128, 256, 512, 871):
        ratio = keep / full.shape[0]
        log(f"{keep:<14}{ratio:<10.2f}{base*ratio:.1f}s（估算）")

    # ---------- 实验 3：batch size 吞吐 ----------
    log("\n=== 实验3：batch size 对总吞吐的影响 ===")
    pix = page.get_pixmap(dpi=96)
    imgs = [Image.frombytes("RGB", [pix.width, pix.height], pix.samples) for _ in range(4)]
    log(f"{'batch':<10}{'总耗时':<12}{'每页耗时':<12}{'相对加速'}")
    single = None
    for bs in (1, 2, 4):
        t0 = time.time()
        for i in range(0, 4, bs):
            model.encode_document(imgs[i:i + bs])
        dt = time.time() - t0
        if bs == 1:
            single = dt
        log(f"{bs:<10}{dt:<12.1f}{dt/4:<12.1f}{single/dt:.2f}x" if single else f"{bs:<10}{dt:.1f}")

    doc.close()
    log("\n[done] 实验完成")


if __name__ == "__main__":
    main()