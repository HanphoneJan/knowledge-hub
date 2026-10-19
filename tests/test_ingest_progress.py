"""🎯 摄取进度的**粒度**（`ingest/pipeline.py::embed_fraction` 等）。

## 为什么这条要单测，而不是在端到端里断言

`eval/api_1c_check.py` 会轮询 `/jobs` 采样 progress，但**采样能不能采到
中间点取决于轮询间隔与编码耗时的关系** —— 采不到时，分不清是
"实现没有中间点"还是"没采到"。那种断言要么 flaky，要么得放宽成
恒真（`any(p < 1 for p in seen)` 在只采到首尾时也是 False，
反过来写就成了"必须有中间点"，一遇到慢机器就红）。

所以把"进度怎么算"抽成纯函数在这里钉死，端到端只验"单调不减 + 终点是 1"。

## 这条约束的由来

1c 把 `parse`/`chunk`/`embed` 合并成一个 `KIND_INGEST` 之后，
`handle_ingest` 只在结束时 `progress(1.0)` ——
于是 `jobs.progress` 从 0 跳到 1，中间 6.5 秒（74 chunk × 88ms）
完全没有信息。PROGRESS §5.38。
"""

from __future__ import annotations

import pytest

from services.ingest.pipeline import (
    EMBED_BATCH,
    EMBED_FROM,
    EMBED_TO,
    STAGE_PROGRESS,
    embed_fraction,
)


def test_stage_progress_is_strictly_increasing():
    """阶段权重必须严格递增，且落在 [0,1]。

    递增是硬要求：`progress` 单调性由 worker 的 `heartbeat` 无条件写入，
    一旦某处报了回退的值，前端进度条会**往回跳** —— 那是明显的 bug，
    而 DB 里只看最终值永远看不出来。
    """
    seq = [STAGE_PROGRESS[k] for k in ("start", "parsed", "chunked", "indexed")]
    assert seq == sorted(seq) and len(set(seq)) == len(seq), f"阶段权重非严格递增：{seq}"
    assert all(0.0 <= v <= 1.0 for v in seq), seq


def test_embed_band_starts_where_chunking_ends():
    """编码段的起点要接在分块之后 —— 否则进度条会先退一格再前进。"""
    assert EMBED_FROM == STAGE_PROGRESS["chunked"]
    assert EMBED_TO == STAGE_PROGRESS["indexed"]


@pytest.mark.parametrize(
    "total,expected_points",
    [
        (0, 0),                 # 纯扫描件：没有文本 chunk，一次都不该报
        (1, 1),
        (EMBED_BATCH, 1),       # 正好一批：只有终点
        (EMBED_BATCH + 1, 2),   # 多一个 → 真的有中间点了
        (74, 5),                # 验收 PDF 的真实 chunk 数
    ],
)
def test_batch_count_gives_enough_intermediate_points(total, expected_points):
    """分批数 = 采样点数的上界（不含起终点）。

    `74 / 16 = 5` 批 → 前端最多能看到 5 个中间点。
    数字的意义在于：如果哪天有人把 `EMBED_BATCH` 调到 100，
    这条会红 —— 因为 74 个 chunk 就又只剩 0/1 两跳了。
    """
    seen = [embed_fraction(min(s + EMBED_BATCH, total), total)
            for s in range(0, total, EMBED_BATCH)]
    inner = [v for v in seen if EMBED_FROM < v < EMBED_TO]
    assert len(seen) == expected_points
    if total <= EMBED_BATCH:
        # 只有一批（或零批）：没有中间点
        assert not inner
        if seen:
            assert seen[-1] == pytest.approx(EMBED_TO)
    else:
        assert len(inner) == expected_points - 1
        assert seen == sorted(seen), f"进度回退了：{seen}"
        assert seen[-1] == pytest.approx(EMBED_TO)


def test_fraction_is_clamped_and_monotonic():
    """`done > total` 之类的不该把进度顶到 1 以外（DB 里会显示 130%）。"""
    assert embed_fraction(0, 10) == pytest.approx(EMBED_FROM)
    assert embed_fraction(10, 10) == pytest.approx(EMBED_TO)
    assert embed_fraction(99, 10) == pytest.approx(EMBED_TO)
    assert embed_fraction(-5, 10) == pytest.approx(EMBED_FROM)
    # total=0 不能抛 ZeroDivisionError
    assert embed_fraction(3, 0) == pytest.approx(EMBED_FROM)


def test_first_reported_value_is_not_zero():
    """第一个上报值要 > 0。

    `progress=0` 与"任务还没开始"在 UI 上长得一模一样 ——
    前端只能靠 status 区分，而 status 的 pending→running 切换
    与第一次回报之间的窗口很短。起点给 0.02 是刻意的：
    让"已入队、正在开始"这件事在 UI 上可区分。
    """
    assert STAGE_PROGRESS["start"] > 0