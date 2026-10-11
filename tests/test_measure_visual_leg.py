"""🔌 `deploy/measure_visual_leg.py` 的守卫（CONVENTIONS §4.1b/c 的可执行性）。

## 为什么需要它

`CONVENTIONS.md` §4.1b 写着"谁要在自己的部署上重测这一条，请**连并发一起量**"。
⚠️ 那句话**一度无法执行** —— 数字在文档里，量法在 `/tmp` 里，`/tmp` 一清就没了。
现在脚本进了仓，但**它自己也会被改坏**：

| 脚本被改成什么 | 后果 | 症状 |
|---|---|---|
| 去掉并发那一段 | §4.1b/c 的指示又变成"没法执行" | 文档仍在说"要连并发一起量"，而没有能照着改的东西 |
| 去掉"无活数据就拒绝跑" | 在空库上跑出**空的召回**，而输出看起来完全正常 | 结论完全错，且没有任何报错（比报错难查得多）|
| 去掉 CPU/并行度统计 | 只剩延迟数字，"吃满几个核"又说不清 | §4.1 的原理由重新变成"理论上担心" |

## 三条判据

1. **必须量并发**（有 `--concurrency`，且报并行度 = CPU秒 / 墙钟秒）
2. **必须能在无活数据时拒绝运行**（否则召回那一节输出空结果而不报错）
3. **必须说清"不要在 API 进程里跑"**（那正是要测的决策本身）

⚠️ **本守卫的盲区**：它只查**文本与 AST**，不真的加载模型（那要 13s + 2.6GB）。
真跑一次是 `MEMORY.md` 里记着的手工命令，输出见 `CONVENTIONS.md` §4.1c。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "measure_visual_leg.py"


def _src() -> str:
    return SCRIPT.read_text(encoding="utf-8")


# ============================================================
# 1. 并发那一维（§4.1b/c 存在的理由）
# ============================================================


def test_has_concurrency_option_and_reports_parallelism():
    """🔑 有 `--concurrency`，且报**并行度**（CPU 秒 / 墙钟 秒）。

    🔑 为什么判"并行度"而不是只判"用了线程池"：§4.1 的原理由是
    "吃满 12 核"，那是个**并行度**的事实。只看线程池的话，
    一个串行跑完再返回的假并发也能过。
    """
    src = _src()
    assert "--concurrency" in src, "没有 --concurrency —— §4.1b 的指示就没法执行"
    tree = ast.parse(src)
    funcs = {n.name: n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef)}
    main_body = ast.unparse(funcs["main"])
    assert "ThreadPoolExecutor" in main_body, "没有真的并发（只有一个装饰器名字不够）"
    # 🔴 判据必须落到"**并行度的分子是两次测量之差**"上。
    #   只查"文件里出现过 cpu_seconds()"会被骗：`main()` 里那一句
    #   `cpu_used = cpu_seconds() - cpu_before` 被换成 `cpu_used = 0.0`
    #   （假装不耗 CPU）时，函数里**另外两处** `cpu_seconds()` 还在 ⇒ 照样全绿。
    #   而那正是最坏的情况：并行度会报成 0.0x，"吃几个核"重新变成说不清。
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "main")
    diffs = {
        tgt.id
        for n in ast.walk(fn) if isinstance(n, ast.Assign)
        for tgt in n.targets if isinstance(tgt, ast.Name)
        if isinstance(n.value, ast.BinOp) and isinstance(n.value.op, ast.Sub)
        and isinstance(n.value.left, ast.Call)
        and isinstance(n.value.left.func, ast.Name)
        and n.value.left.func.id == "cpu_seconds()".split("(")[0]
    }
    assert diffs, (
        "没有任何 `X = cpu_seconds() - Y` 的测量 ⇒ 并行度算不出来"
    )
    # 且打印并行度时用的分子**必须是**那个差值（不是常数、不是别的量）
    printed = [ast.unparse(n) for n in ast.walk(fn)
               if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Name) and n.func.id == "print"]
    assert any(any(d in s for d in diffs) and "并行度" in s for s in printed), (
        f"打印并行度时没用 CPU 差值 {sorted(diffs)} ⇒ {printed[-1][:80]}"
    )
    assert "并行度" in src, "输出里没说并行度（§4.1c 记的就是这个数字）"


def test_default_concurrency_is_at_least_four():
    """🔑 默认并发 ≥4 —— §4.1b 明写"≥4 个并发查询时的 p95"。

    ⚠️ 默认给 1 的话，"忘了加 `--concurrency`"会静悄悄退化成串行测量，
    而输出格式与并发时**一模一样**。
    """
    tree = ast.parse(_src())
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    main_body = ast.unparse(funcs["main"])
    assert 'default=4' in main_body or "default=8" in main_body, (
        "默认并发不是 ≥4 —— 忘了加参数会静悄悄退化成串行"
    )


# ============================================================
# 2. 无活数据必须拒绝（否则结论完全错且不报错）
# ============================================================


def test_refusal_decision_is_made_by_the_real_point_count():
    """🔑 拒绝与否**由真实点数决定**（纯函数，可直接验）。

    🔴 第一版那个判据查的是"文件里出现过 `actual` / `SystemExit`"，
    于是变异把 `vis = drift["kb_visual"]` 换成硬编码
    `{"expected": 0, "actual": 0}` 时**照样全绿** —— 而那正是最坏的情况：
    空库上跑出空召回，而整份报告（加载耗时、内存、延迟）**全都正常**，
    看报告的人会以为"视觉腿没召回能力"。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("_mvl", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.refuse_reason(8, 8) is None, "有活数据却拒绝跑了"
    assert mod.refuse_reason(1, 1) is None, "只有 1 个点也该能跑（数据少但有）"
    reason = mod.refuse_reason(0, 0)
    assert reason and "seed_photos" in reason, reason
    # 报错里要带上**期望 vs 实有** —— 那是判断"是没数据还是数据全丢"的唯一依据
    assert "期望" in reason and "实有" in reason, reason


def test_refuses_to_run_without_live_visual_data():
    """🔑 `kb_visual` 为空时**明确拒绝**，不是跑出空结果。

    🔴 这是最要紧的一条：空库上跑出来的召回是"0 命中"，
    而整份输出（加载耗时、内存、延迟）**全都正常** ——
    看报告的人会以为"视觉腿没召回能力"，而真实原因是"没数据可召"。
    """
    src = _src()
    tree = ast.parse(src)
    pick_fn = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "pick_kb")
    pick = ast.unparse(pick_fn)

    # ① 真的查了 kb_visual 的**实有**点数（不是恒为 0 的占位）
    assert "check_drift" in pick, "没算漂移 ⇒ 无从知道有没有活数据"
    assert '"actual"' in pick or "vis['actual']" in pick or 'vis["actual"]' in pick, (
        "没取 kb_visual 的实际点数"
    )
    # 🔑 判据是"`pick_kb` 用了那个纯函数、并在它给出理由时退出"（AST）——
    #   而不是"文件里出现过 SystemExit"（那在任何地方退出一次都能通过）。
    assert "refuse_reason(" in pick, (
        "`pick_kb` 没调用 `refuse_reason` ⇒ 拒绝与否不是由真实点数决定的"
    )
    exits = [
        n for n in ast.walk(pick_fn)
        if isinstance(n, ast.If)
        and "reason" in ast.unparse(n.test)
        and any(isinstance(x, ast.Raise) for x in ast.walk(n))
    ]
    assert exits, "拿到拒绝理由之后没有退出（会继续跑出空召回）"
    assert "seed_photos" in pick, "报错没告诉人先造活数据（D152）"


# ============================================================
# 3. 别在 API 进程里跑（那正是要测的决策）
# ============================================================


def test_says_do_not_run_it_in_the_api_process():
    """🔑 脚本必须写明"不要在 API 进程里跑"。

    ⚠️ §4.1 的原话是"API 进程不得加载 colSmol"。一个在 API 进程里跑起来的
    测量脚本会**当场违反**它自己要去验证的那条约束。
    """
    src = _src()
    assert "API 进程" in src, "脚本没提 API 进程"
    doc = ast.get_docstring(ast.parse(src)) or ""
    assert "不要" in doc and "API" in doc, "docstring 里没有那句警告"
