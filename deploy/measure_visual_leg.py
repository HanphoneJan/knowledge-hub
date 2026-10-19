#!/usr/bin/env python3
"""量视觉腿（D50）在**自己机器**上的成本与收益 —— 独立进程，测完就退出。

## 为什么需要这个脚本

`CONVENTIONS.md` §4.1b 记着 2026-10-05 那次实测（加载 12.8~15.0s /
+2.64GB / 查询编码 119~128ms / 召回 4 条 top1 正确），并在结尾说：

> 谁要在自己的部署上重测这一条，请**连并发一起量**（≥4 个并发查询时的 p95）。

⚠️ 那次**没有**量并发 —— 而"吃满 12 核让 API 长时间无响应"（§4.1 的原理由）
恰恰是**并发下**才成立的说法，单次 128ms 说明不了它。
⇒ 本脚本补上并发那一维，让那句话有个可执行的量法。

## 它量什么

| 项 | 怎么量 | 为什么 |
|---|---|---|
| 模型加载 | `time.perf_counter()` 包 `enable_visual(True)` + `get_visual_model()` | 首次请求延迟 |
| 常驻内存 | `/proc/self/status` 的 `VmRSS`（Linux）| 2C4G 机器会不会 OOM |
| 单次查询编码 | `pool.encode_vdr_query` × N 条 | 视觉腿真正的新增延迟 |
| **并发下的 p50/p95** | `ThreadPoolExecutor` 同时发 M 个查询 | 🔴 **§4.1 的原理由只在这里才成立** |
| 召回 top-k | 直接查 `kb_visual`（multi-vector MaxSim）| 收益侧 |
| CPU 时间 | `resource.getrusage` | "吃满 12 核"的量化 |

## 用法

```bash
uv run python deploy/measure_visual_leg.py                    # 默认 4 并发 × 6 查询
uv run python deploy/measure_visual_leg.py --concurrency 8    # 想看更挤的情况
uv run python deploy/measure_visual_leg.py --kb-id 4249       # 指定库（默认自动找 keeper 库）
```

⚠️ **不要**在 API 进程里跑这个（那正是要测的决策）；它是**一次性脚本**，
跑完进程退出，不改任何配置。
⚠️ 需要**有活数据**（`kb_visual` 非空）才有意义 —— 没有就跑
`uv run python deploy/seed_photos.py`（D152）。空库跑出来的"召回"是空的，
而那份输出**看起来**和正常的一模一样。
"""

from __future__ import annotations

import argparse
import os
import resource
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_QUERIES = ("圆形", "红色的图", "有条纹的", "深蓝色", "棋盘格", "一个圆圈")


def rss_gb() -> float:
    """本进程 RSS（GB）。Linux 读 `/proc/self/status` 最省事。"""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024 / 1024
    except OSError:
        pass
    return 0.0


def cpu_seconds() -> float:
    """本进程累计 CPU 时间（user + sys）。"""
    ru = resource.getrusage(resource.RUSAGE_SELF)
    return ru.ru_utime + ru.ru_stime


def percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1)))))
    return xs[k]


def refuse_reason(visual_actual: int, visual_expected: int) -> str | None:
    """视觉腿**没有活数据**时该拒绝跑吗？返回拒绝理由，或 `None` 表示可以跑。

    🔑 抽成纯函数是为了能被单测：判据是"**这个决定由真实点数做出**"，
    而第一版把决定写在 `pick_kb` 里（那函数要连库、要 Key）⇒ 只能靠文本判，
    变异脚本把 `vis` 换成硬编码 `{"actual": 0}` 时**照样全绿**
    —— 而那正是最坏的情况：空库上跑出空召回，而报告看起来完全正常。
    """
    if visual_actual > 0:
        return None
    return (
        "**没有活视觉数据**，这次测出来的召回会是空的 —— "
        "先跑 `deploy/seed_photos.py`（D152）。\n"
        "⚠️ 不加这个检查的话，输出看起来和正常跑一模一样，"
        "而结论会完全错（这比报错难查得多）。"
        f"（期望 {visual_expected} · 实有 {visual_actual}）"
    )


def pick_kb(explicit: int | None) -> tuple[int, str]:
    """找那个有**活视觉点**的库（显式指定优先）。"""
    import httpx

    key = os.environ.get("PKH_KEEPER_KEY", "").strip()
    if not key:
        print("❌ 没有 `PKH_KEEPER_KEY` —— 先跑 `deploy/seed_photos.py` 造活数据（D152）",
              file=sys.stderr)
        raise SystemExit(1)
    api = os.environ.get("PKH_API_URL", "http://127.0.0.1:8010")
    with httpx.Client(base_url=api, trust_env=False, timeout=30.0) as c:
        kbs = c.get("/api/v1/kb", headers={"Authorization": f"Bearer {key}"}).json()
    if explicit is not None:
        hit = next((k for k in kbs if k["id"] == explicit), None)
        if hit is None:
            print(f"❌ 库 #{explicit} 不在这个 Key 的可见范围里", file=sys.stderr)
            raise SystemExit(1)
        return hit["id"], hit["name"]

    # ⚠️ `sync_session_scope` 在 `services.common.db`，**不在** `consistency` ——
    #   写错模块的报错是 `AttributeError: module ... has no attribute`,
    #   而症状看起来像"这个模块被删了"。
    from services.common import consistency
    from services.common.db import sync_session_scope

    with sync_session_scope() as s:
        drift = consistency.check_drift(s, consistency.current_kb_ids(s))
    vis = drift["kb_visual"]
    print(f"当前 kb_visual：期望 {vis['expected']} · 实有 {vis['actual']}")
    reason = refuse_reason(int(vis["actual"]), int(vis["expected"]))
    if reason:
        print(f"❌ {reason}", file=sys.stderr)
        raise SystemExit(1)
    keeper = next((k for k in kbs if k["name"] == "keeper 照片"), None)
    if keeper is None:
        print(f"⚠️ 这个 Key 看不到 keeper 照片库，用第一个 photo 库代替", file=sys.stderr)
        keeper = next((k for k in kbs if k.get("source_kind") == "photo"), kbs[0])
    return keeper["id"], keeper["name"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--concurrency", type=int, default=4,
                    help="并发查询数（§4.1b 说要 ≥4）")
    ap.add_argument("--kb-id", type=int, default=None)
    ap.add_argument("--queries", type=int, default=len(DEFAULT_QUERIES))
    args = ap.parse_args()

    from services.retrieval import pool

    queries = list(DEFAULT_QUERIES)[: max(1, args.queries)]
    kb_id, kb_name = pick_kb(args.kb_id)
    print(f"库：{kb_name} #{kb_id} · 并发 {args.concurrency} · {len(queries)} 条查询\n")

    rss0, cpu0 = rss_gb(), cpu_seconds()
    t0 = time.perf_counter()
    pool.enable_visual(True)
    pool.get_visual_model()
    load_s = time.perf_counter() - t0
    print(f"① 模型加载 {load_s:.1f}s · RSS {rss0:.2f} → **{rss_gb():.2f} GB**"
          f"（+{rss_gb() - rss0:.2f}）")

    print("\n② 单次查询编码（串行）")
    serial: list[float] = []
    for q in queries:
        t = time.perf_counter()
        vecs = pool.encode_vdr_query(q)
        serial.append((time.perf_counter() - t) * 1000)
        print(f"   {q!r:12} → {len(vecs)} 个 token 向量 · {serial[-1]:.0f}ms")
    print(f"   p50 {percentile(serial, 50):.0f}ms · p95 {percentile(serial, 95):.0f}ms")

    # 🔴 并发：§4.1 的原理由（吃满 12 核 → API 长时间无响应）只在这里才成立
    print(f"\n③ 并发 {args.concurrency} 路（同一批查询同时发）")
    cpu_before = cpu_seconds()
    wall0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool_exec:
        futs = [pool_exec.submit(pool.encode_vdr_query, q) for q in queries]
        conc = []
        for f in futs:
            t = time.perf_counter()
            f.result()
            conc.append((time.perf_counter() - t) * 1000)
    wall = time.perf_counter() - wall0
    cpu_used = cpu_seconds() - cpu_before
    print(f"   墙钟 {wall:.2f}s · 消耗 CPU {cpu_used:.2f}s ⇒ **并行度 {cpu_used / wall:.1f}x**")
    print(f"   单条 p50 {percentile(conc, 50):.0f}ms · p95 {percentile(conc, 95):.0f}ms")
    print(f"   ⚠️ 对照串行 p95 {percentile(serial, 95):.0f}ms —— "
          "这就是「同进程并发」的真实代价")

    print("\n④ 召回（视觉腿直接查 kb_visual，multi-vector MaxSim）")
    import anyio
    import functools

    from services.common.auth import UserContext
    from services.retrieval import visual

    ctx = UserContext(user_id=0, display_name="probe", email="probe@x",
                      api_key_id=None, readable_kb_ids=frozenset({kb_id}))
    top1_correct = 0
    #: fixture 的形状→期望的第一名（D152：8 张图的形状明显不同）
    expect = {"圆形": "circle", "红色的图": "circle", "有条纹的": "bars",
              "深蓝色": "triangle", "棋盘格": "checker", "一个圆圈": "circle"}
    for q in queries:
        res = anyio.run(functools.partial(
            visual.search_visual, ctx, q, kb_id=kb_id, limit=8))
        names = [str((h.payload or {}).get("file_name") or h.point_id)
                 for h in res.hits]
        hit1 = next((n for n in names if "keeper-" in n), "")
        shape = hit1.split("-")[-1].replace(".jpg", "")
        want = expect.get(q)
        ok = want is not None and shape == want
        top1_correct += int(ok)
        print(f"   {q!r:12} skipped={str(res.skipped)[:30]:32} "
              f"命中 {len(res.hits)} · top1={shape or '—'}"
              f"（期望 {want}）{'✅' if ok else ''}")
    print(f"   ⇒ top1 完全正确 {top1_correct}/{len(queries)}")

    print(f"\n⑤ 峰值 RSS {rss_gb():.2f} GB · 累计 CPU {cpu_seconds():.0f}s")
    print("⚠️ 这份数字只覆盖**本机、本数据集**。要推翻 CONVENTIONS §4.1，"
          "需要在自己的部署上重跑本脚本，并**连并发一起看**（§4.1b）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
