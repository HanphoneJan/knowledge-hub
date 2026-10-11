"""visual 腿：colSmol-500M multi-vector MaxSim（`kb_visual`）。

## 编码在哪跑（两处），Qdrant 查询永远在本地

| 步骤 | 在哪 | 为什么 |
|---|---|---|
| ① 编码查询文本 | 本进程（`enable_visual` 为真）**或**远端视觉服务 | §4.1：本进程是 API 时**绝不能**碰 colSmol |
| ② `build_qdrant_filter` | **始终本地** | 租户过滤唯一入口之一（§0.2） |
| ③ MaxSim 查询 | **始终本地** | 同上 |

三个进程各自能做什么：

| 进程 | 视觉腿 |
|---|---|
| `services/api` | 只调 ①②③ 的②③，编码走**远端 HTTP**（D158） |
| `services/worker` | ① **本地**（编码图片，15s/图）· ③ 写侧 |
| `services/visual_service` | ① **本地**（编码查询，119~128ms/条）|

🔑 **②③ 留在 API 侧是架构性的**：`build_qdrant_filter` 是唯一的租户过滤
入口之一，复制一份到新进程就多了一份 ⇒ 两份过滤逻辑迟早漂移，
而漂移的方向是"**越权看到别人的照片**"（§0.6，唯一一次真实越权的形状）。

## 🔴 视觉腿是**增强项**：挂了就降级，绝不 500

远端不可达 / 503 / 超时 ⇒ `skipped`，检索照常返回 dense+sparse+trgm。
`skipped` 的文案要**可操作**（说清怎么修）—— 只写"失败"会让人查错方向
（§5.93/§5.94 那一族）。

⚠️ 模块 docstring 里曾写着"API 进程恒为跳过 / API 进程恒为两路+trgm" ——
   D158 之后**不再成立**，照着它排障会跑去 API 进程里找一个不存在的开关。

## 三条腿 → 四条腿时的实测数据（`reports/retrieval_eval.json`）

| 类别 | visual R@10 |
|---|---|
| photo | **1.00** |
| visual/扫描页 | **1.00** |
| blog / chat / doc | 0.00（它只索引 47 个点，天然召不回文本类 query） |

所以 overall 的 0.195 **没有意义**，只有分类别数字可比 —— 别拿它做取舍。

## 这一腿是 PROGRESS §4 那条结论的落点

> photo 类 R@5=0.00 是真实缺陷：查"第一次旅游的照片"时 RRF 前 5 名全是文本结果。
> **下一步应做 query 类型路由**（识别出"找照片/找扫描件"意图后提高对应腿权重
> 或直接过滤 modality），而不是继续调 RRF 的 k 值。

`retrieval/routing.py`（阶段 3）就是干这个的。本模块的 payload 里保留
`source_kind`，供它按 modality 过滤 —— 别在 fusion 里写死。
"""

from __future__ import annotations

import time
from typing import Any

import anyio.to_thread

from ..common.auth import UserContext, build_qdrant_filter
from ..common.qdrant import COLL_VISUAL, VISUAL_VECTOR_NAME, get_client
from .types import LEG_VISUAL, NS_VISUAL, LegHit, LegResult, rrf_key


async def visual_available() -> bool:
    """本进程**能不能**跑视觉腿（`available_legs()` 用它如实汇报）。

    🔑 两个来源，任一为真即可：
    1. **本地** `pool.visual_enabled()` —— worker 进程（写侧）与
       视觉查询服务自己。它在 API 进程恒 `False`（§4.1）。
    2. **远端** `visual_service_url` 可达且模型就绪 —— D158 之后的读侧。

    ⚠️ collection 为空**不是**判据：空只说明"没数据"，能力可用性是另一回事，
    两者必须分开报（否则"这个库没有照片"与"这台机器装不了 colSmol"
    会长得一模一样）。

    🔑 变成 `async` 是因为远端探测要发 HTTP。别把它变回同步 ——
    那就只能在事件循环里 `run_sync` 发网络请求，会**堵住整个事件循环**。
    """
    from .pool import visual_enabled, visual_service_health

    if visual_enabled():
        return True
    health = await visual_service_health()
    return bool(health.get("reachable"))


def _skipped(leg: str, t0: float, reason: str) -> LegResult:
    """一条 `skipped` 结果。**所有降级路径都走它** —— 别在别处再拼一次 dict。

    🔑 单一出口不是为了好看：`LegResult` 的 `skipped` 语义是
    "`None` = 正常跑了但没命中"（`types.py`），而 `elapsed_ms` 与 `leg`
    两处拼错**不会报错**，只会让 `/search/debug` 显示得对不上。
    """
    return LegResult(
        leg=leg, elapsed_ms=int((time.perf_counter() - t0) * 1000), skipped=reason
    )


def _unavailable_reason(health: dict[str, Any]) -> str:
    """远端不可用的说明。**必须能照着修** —— 说"失败"的文案会让人查错方向。

    ⚠️ 刻意**不提**"colSmol 未启用"：D158 之后 API 进程不加载 colSmol 是
    **正常状态**（§4.1），把它说成原因会让下一个人去 API 进程里找问题，
    而那里根本没有开关（曾写着"API 进程恒为两路+trgm"，接通后就不成立了）。
    """
    from .pool import remote_hint

    reason = health.get("reason") or "视觉服务不可用"
    return f"视觉腿跳过：{reason}。{remote_hint()}"


def _query_sync(vecs: list[list[float]], flt: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """⚠️ multi-vector 必须走 `/points/query`（不能用 `/points/search`）。

    Qdrant 1.15 实测（PROGRESS §5.9）：

    | 请求形式 | 结果 |
    |---|---|
    | `/points/search` + `{"vector": [[...]], "using": "text"}` | ❌ 400 NamedVectorStruct |
    | `/points/search` + `{"vector": {"name":"text","vector":[[...]]}}` | ❌ 400 同上 |
    | `/points/search` + `{"vector":{"name":"text","vector":[单个128维]}}` | ⚠️ 200 但退化成单向量，**不是** late-interaction |
    | **`/points/query` + `{"query": [[...]], "using": "text"}`** | ✅ 200 真 MaxSim |

    写错不会降级报错，只会"退化成单向量静默给出错误的相似度" —— 那是最糟的失败方式。
    """
    r = get_client().post(
        f"/collections/{COLL_VISUAL}/points/query",
        json={
            "query": vecs,
            "using": VISUAL_VECTOR_NAME,
            "limit": limit,
            "with_payload": True,
            "with_vector": False,
            "filter": flt,   # 租户过滤唯一入口（CONVENTIONS §0.2）
        },
    )
    r.raise_for_status()
    return r.json()["result"]["points"]


async def search_visual(
    ctx: UserContext,
    query: str,
    *,
    kb_id: int | None = None,
    kb_ids: list[int] | None = None,
    limit: int = 30,
) -> LegResult:
    """视觉腿召回：**编码**与 **Qdrant 查询 + 租户过滤**分开。

    | 步骤 | 在哪 | 为什么 |
    |---|---|---|
    | ① 编码查询文本 | 本进程（`enable_visual` 为真）**或**远端视觉服务 | §4.1：本进程是 API 时绝不能碰 colSmol |
    | ② `build_qdrant_filter` | **始终本地** | 租户过滤唯一入口之一（§0.2） |
    | ③ MaxSim 查询 | **始终本地** | 同上 |

    🔑 **②③ 留在本地是架构性的**：把它们搬进新进程就多了一份过滤逻辑，
    而两份过滤逻辑迟早漂移，漂移的方向是"越权看到别人的照片"（§0.6）。

    🔴 **任何一步失败都降级成 `skipped`，绝不抛出去** ——
    视觉腿是增强项，它挂掉不能让检索整体挂掉（D158 的核心约束）。
    `skipped` 的文案必须**可操作**：说清怎么修，而不是"失败"两个字。
    """
    t0 = time.perf_counter()

    # ---- ① 编码 ----
    from .pool import (
        VisualServiceError,
        encode_vdr_query,
        encode_vdr_query_remote,
        remote_hint,
        visual_enabled,
        visual_service_health,
    )

    if visual_enabled():
        # 本进程装了 colSmol（worker / 视觉服务自身）
        try:
            vecs = await anyio.to_thread.run_sync(encode_vdr_query, query)
        except Exception as exc:  # noqa: BLE001
            return _skipped(LEG_VISUAL, t0, f"本地视觉编码失败：{exc}")
    else:
        # ⚠️ **先探活再编码**，而不是直接编码：
        #   服务没起时 `/encode` 会**连接失败**（connect 收紧到 1s），
        #   每次检索都白等那 1s。探活结果有 5s 缓存（`pool.visual_service_health`），
        #   且"没起"这个答案**本来就比"连不上"更有诊断价值**。
        health = await visual_service_health()
        if not health.get("reachable"):
            return _skipped(LEG_VISUAL, t0, _unavailable_reason(health))
        try:
            vecs = await encode_vdr_query_remote(query)
        except VisualServiceError as exc:
            # ⚠️ 已带修复指引的降级。**不要**改成 `except Exception` ——
            #   那样会把"带指引"的文案退化成 "visual 腿失败: ConnectError"。
            return _skipped(LEG_VISUAL, t0, f"视觉腿跳过：{exc.reason}。{exc.hint}")
        except Exception as exc:  # noqa: BLE001
            return _skipped(
                LEG_VISUAL, t0,
                f"视觉编码失败：{type(exc).__name__}: {exc}。{remote_hint()}",
            )

    # ---- ②③ 租户过滤 + Qdrant MaxSim（**始终本地**）----
    # 🔑 过滤**先**算成一个具名变量，而不是内联进查询参数：
    #   内联时"查回来的点有没有带过滤"这件事在源码里看不出来（测试只能
    #   去扫调用实参），而它恰恰是最不能看不出来的那个（§0.6）。
    flt = build_qdrant_filter(ctx, kb_id, kb_ids=kb_ids)
    try:
        points = await anyio.to_thread.run_sync(_query_sync, vecs, flt, limit)
    except Exception as exc:  # noqa: BLE001
        return _skipped(LEG_VISUAL, t0, f"kb_visual 查询失败：{exc}")

    hits: list[LegHit] = []
    for rank, p in enumerate(points, start=1):
        payload = p.get("payload") or {}
        pid = int(p["id"])
        hits.append(
            LegHit(
                # ⚠️ 命名空间前缀必须与文本腿不同（PROGRESS §5.10）：
                # `kb_dense` 的 chunk 点与 `kb_visual` 的页级点 ID 空间独立，
                # 不加前缀会让"文本 chunk 3"和"扫描页 3"在 RRF 里互相抵消。
                key=rrf_key(NS_VISUAL, pid),
                point_id=pid,
                doc_id=int(payload.get("doc_id") or 0),
                chunk_index=int(payload.get("page") or -1),
                rank=rank,
                score=float(p.get("score") or 0.0),
                leg=LEG_VISUAL,
                payload=payload,
            )
        )
    return LegResult(
        leg=LEG_VISUAL, hits=hits, elapsed_ms=int((time.perf_counter() - t0) * 1000)
    )