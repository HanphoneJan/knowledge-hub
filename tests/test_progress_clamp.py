"""🔑 `progress=1.0` **只归 `finish_job` 所有**（非终态不许出现 1.0）。

## 现象

`eval/api_1c_check.py` 偶发红在这一条（实测 200+ 次里 1 次）：

```
✓ 采样到中间态（worker 在跑时 progress 已可见）  5/6 个样本非终态
✗   中间态的 progress < 1   中间态 status：['pending', 'running']
```

采样序列是 `0.00 → 0.02 → 0.45 → 0.67 → **1.00** → 1.00` ——
中间那个 1.00 的样本，`status` 还是 `pending`/`running`。

## 根因：两次提交之间的窗口

```
handler 结尾  progress(1.0)   → 独立事务提交   ← 这一刻 progress=1.0，status=running
worker 收尾   finish_job(s)   → 另一个事务     ← 到这里才 status=done
```

`report_job` 的 docstring 说进度**故意**走独立事务（不然几万条消息的导入
期间 `/jobs` 上那个数会一直是 0）—— 这个设计是对的，问题只是
**1.0 这个值不该由 handler 写**：它表达"完成了"，而"完成"由
`finish_job` 判定（含重试判定）。

## 为什么必须修而不是"改断言迁就"

这个窗口只有几毫秒，所以它是**偶发红**。而偶发红比稳定红贵得多：
下一个人看到基线红，会以为索引坏了或数据不一致，去查一个
**根本不存在**的问题（我自己这次就差点去查视觉腿）。

封顶 `0.999` 而不是**丢弃**这次上报：进度必须单调不减
（`api_1c` 就在断言 `0.67 → 1.00` 不许倒退），丢掉会造成倒退。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "services" / "worker" / "main.py"


def _clamp(p: float) -> float:
    from services.worker.main import clamp_progress

    return clamp_progress(p)


# ============================================================
# 1. 封顶本身
# ============================================================


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        (0.0, 0.0),
        (0.02, 0.02),
        (0.67, 0.67),
        (0.999, 0.999),
        (1.0, 0.999),      # 🔑 handler 的"我干完了"不是"任务完成"
        (1.5, 0.999),      # 越界也不许穿透
    ],
)
def test_nonterminal_progress_never_reaches_one(reported, expected):
    assert _clamp(reported) == pytest.approx(expected)


def test_clamping_keeps_progress_monotonic():
    """🔑 封顶不能造成**倒退** —— `api_1c` 断言 progress 单调不减。

    如果实现是"丢弃 ≥1.0 的上报"，序列会变成
    `0.00 → 0.02 → 0.45 → 0.67 → （跳过）→ 1.00`，
    中间那次 1.00 消失倒不算倒退，但**导入类任务**里
    "阶段完成"也报 1.0 时就会真的倒退。所以必须压值而不是丢值。
    """
    seq = [0.0, 0.02, 0.45, 0.67, 1.0, 1.0]
    clamped = [_clamp(x) for x in seq]
    assert clamped == sorted(clamped), clamped
    assert clamped[-1] == 0.999


def test_never_exceeds_one_even_for_absurd_values():
    assert _clamp(1e9) < 1.0
    assert _clamp(float("inf")) < 1.0


# ============================================================
# 2. 真的接进上报路径
# ============================================================


def test_progress_reporter_uses_the_clamp():
    """🔑 `heartbeat` 必须收到**封顶后**的值。

    ⚠️ 抽了函数不接线是本项目踩过的形状（`_wait_queue_idle` 的返回值
    没人用、§5.109；`_visual_render_scale` 同理）。所以判据落在
    调用点，而不是"函数存在"。
    """
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "run_once")
    body = ast.unparse(fn)
    assert "clamp_progress(" in body, "run_once 没有用 clamp_progress —— helper 是死的"
    assert "heartbeat(ps, job_id, p)" not in body, (
        "还在把原始 p 写进去（封顶被绕过）"
    )


def test_finish_job_is_the_only_writer_of_one():
    """🔑 `progress = 1.0` 只该出现在 `finish_job` 里。

    理由：`1.0` 表达"完成"，而完成由 `finish_job` 判定。
    handler 里再写一次就制造了那个"running + 1.0"的窗口。
    """
    jobs_src = (ROOT / "services" / "common" / "jobs.py").read_text(encoding="utf-8")
    tree = ast.parse(jobs_src)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "finish_job")
    assert "1.0" in ast.unparse(fn), "finish_job 不再写 progress=1.0 —— 终态没进度了"

    # 而 worker 主循环里不该有裸的 1.0
    assert "progress = 1.0" not in MAIN.read_text(encoding="utf-8")


# ============================================================
# 3. 与验收脚本对齐（别让下一个人以为是自己搞坏了）
# ============================================================


def test_eval_assertion_matches_the_invariant():
    """`api_1c` 的判据就是这条不变量 —— 它红了不是索引坏了。"""
    src = (ROOT / "eval" / "api_1c_check.py").read_text(encoding="utf-8")
    assert "中间态的 progress < 1" in src, (
        "1c 不再断言这条 —— 那 D125 之前那个偶发红的判据被悄悄删了？"
    )
