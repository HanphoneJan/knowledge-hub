"""Qdrant 客户端与 collection 管理。

collection 布局见 `DESIGN-DATA-MODEL.md` 第 3 节。
三个 collection 的点 ID 完全一致（稳定哈希，见 `point_id()`），
检索时并行查再 RRF 融合。
"""

from __future__ import annotations

import hashlib
from typing import Any

import httpx

from .config import get_settings

# ---------------- collection 名（D7 / D22）----------------

COLL_DENSE = "kb_dense"  # 512d unnamed，bge-small-zh-v1.5
COLL_SPARSE = "kb_sparse"  # 稀疏，jieba + BM25
COLL_VISUAL = "kb_visual"  # 命名向量 "text"，multi-vector 128d colSmol MaxSim

ALL_COLLECTIONS = (COLL_DENSE, COLL_SPARSE, COLL_VISUAL)

#: colSmol-500M 的输出维度。改了要重建视觉索引。
VDR_DIM = 128
#: 命名向量名。必须与建 collection 时一致，否则写入报 400。
VISUAL_VECTOR_NAME = "text"
SPARSE_VECTOR_NAME = "bm25"

# ---------------- 稳定点 ID ----------------


def point_id(kb_id: int, doc_id: int, chunk_index: int) -> int:
    """稳定可复算的点 ID。

    为什么要哈希而不是自增序号：
    1. 自增序号在删除后不复用，跨文档/跨 KB 的 ID 会撞车
    2. 哈希后可从 `(kb_id, doc_id, chunk_index)` 直接算出来，
       **不需要先查 DB 就能定位 Qdrant 里的点** —— 这是"删索引再重建"安全的前提

    blake2b-64，实测 10 万 chunk 零冲突（2^63 空间，期望碰撞数 ≈ 5e-11）。
    右移一位是为兼容 Qdrant 的 uint64，避开最高位。

    注：不带 namespace 前缀，因为三个 collection 靠同一 ID 对齐（见上）。
    """
    raw = f"{kb_id}\x00{doc_id}\x00{chunk_index}".encode()
    return int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big") >> 1


def point_id_for_page(kb_id: int, doc_id: int, page_no: int) -> int:
    """视觉索引的页级 ID。与 `point_id` 同算法但 chunk_index 传负数，避开撞车。"""
    return point_id(kb_id, doc_id, -(page_no + 1))


# ---------------- 客户端 ----------------

_client: httpx.Client | None = None


def get_client() -> httpx.Client:
    """共享的 HTTP 客户端。

    必须 `trust_env=False` —— 本机有 fclash 代理在 127.0.0.1:7890，
    httpx 默认读 HTTP_PROXY 环境变量，会把内网请求也送去代理，导致连不上。
    """
    global _client
    if _client is None:
        _client = httpx.Client(
            base_url=get_settings().qdrant_url,
            # read/write 给 60s：批量 upsert 一个大文档的几千个点会慢，
            # 但查询很快，所以单独把 connect 收紧到 5s 快速失败。
            timeout=httpx.Timeout(60.0, connect=5.0),
            trust_env=False,
        )
    return _client


def close_client() -> None:
    global _client
    if _client is not None:
        _client.close()
    _client = None


async def check_qdrant() -> dict[str, Any]:
    """健康检查用。异常一律吞掉返回 —— 健康检查要能看到故障本身。"""
    try:
        r = get_client().get("/collections")
        r.raise_for_status()
        names = [c["name"] for c in r.json()["result"]["collections"]]
        return {"ok": True, "collections": names}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


# ---------------- collection 定义 ----------------

# payload 索引：给"会被查询命中的字段"建索引。
#
# 🔑 **只有 `kb_id` 参与租户过滤** —— `auth.build_qdrant_filter` 只按它过滤
#    （三档可见性在 PG 侧已折叠成 `ctx.readable_kb_ids`，D8/§0.2）。
#    `doc_id` 用于删除 / 计数（`remove_from_indexes` / `visual_point_count`）。
#
# ⚠️ `workspace_id` / `source_kind` / `visibility` **不参与任何过滤**，
#    它们随结果回传，供审计与前端展示。⚠️ 尤其 `visibility` 是**摄取时写死**的
#    （`ingest/chunker.py`），改库可见性**不会**回写 ⇒ 一旦有人按它过滤，
#    陈旧值会**立刻**变成越权（对齐报告 §4）。守卫：`tests/test_qdrant_filter_scope.py`。
PAYLOAD_INDEXES: tuple[tuple[str, str], ...] = (
    ("kb_id", "keyword"),
    ("workspace_id", "keyword"),
    ("doc_id", "integer"),
    ("source_kind", "keyword"),
    ("visibility", "keyword"),
)


def ensure_collections(recreate: bool = False) -> dict[str, str]:
    """建好三个 collection + payload 索引。可重复执行（幂等）。"""
    c = get_client()
    created: dict[str, str] = {}

    existing = {
        col["name"]
        for col in c.get("/collections").json()["result"]["collections"]
    }

    for name in ALL_COLLECTIONS:
        if name in existing and recreate:
            c.delete(f"/collections/{name}")
            existing.discard(name)
        if name in existing:
            created[name] = "exists"
            continue

        if name == COLL_VISUAL:
            # colSmol 输出 multi-vector（late-interaction / MaxSim）。
            # 实测：Qdrant 不支持 unnamed + named multi-vector 混用，
            # 必须整块独立成 collection。
            # 命名向量名沿用 "text"（与旧 mem_visual 一致，见 PROGRESS.md 5.2）
            body: dict[str, Any] = {
                "vectors": {
                    "text": {
                        "size": VDR_DIM,
                        "distance": "Cosine",
                        "multivector_config": {"comparator": "max_sim"},
                    }
                }
            }
        elif name == COLL_SPARSE:
            # 注意：稀疏向量是**顶层 sparse_vectors 键**，不是 vectors 里的项。
            # 写成 {"vectors": {"bm25": {}}} 会被 Qdrant 当成 dense 命名向量拒绝。
            body = {"sparse_vectors": {SPARSE_VECTOR_NAME: {}}}
        else:
            body = {
                "vectors": {
                    "size": get_settings().text_embed_dim,
                    "distance": "Cosine",
                }
            }

        r = c.put(f"/collections/{name}", json=body)
        r.raise_for_status()
        created[name] = "created"

    # payload 索引（三个 collection 都要）
    # `wait=true` 让建索引变成同步操作 —— 否则 `up` 返回时索引还没建完，
    # 紧接着发查询会走全表扫描。
    for name in ALL_COLLECTIONS:
        for field, schema in PAYLOAD_INDEXES:
            r = c.put(
                f"/collections/{name}/index?wait=true",
                json={"field_name": field, "field_schema": schema},
            )
            r.raise_for_status()

    return created


def payload_schema(name: str) -> dict[str, Any]:
    """读某个 collection 的 payload 索引现状。

    ⚠️ 实测踩坑：`payload_schema` 在响应的**顶层**，不在 `config` 里面。
    写成 `result["config"]["payload_schema"]` 会一直拿到 null，
    看起来像"索引没建成"。
    """
    r = get_client().get(f"/collections/{name}")
    r.raise_for_status()
    return r.json()["result"].get("payload_schema") or {}


# ---------------- 写入辅助 ----------------


def sparse_vector(weights: dict[int, float]) -> dict[str, list[float]]:
    """稀疏向量分量 → REST 可接受的 JSON（**不含** collection 里的向量名）。

    ⚠️ 实测踩坑（写入与查询的格式不一样，写文档时容易混）：

    | 场景 | 可用格式 |
    |------|---------|
    | 写入 points | `{"bm25": {"indices": [...], "values": [...]}}` |
    | 查询 query   | `{"name": "bm25", "indices": [...], "values": [...]}` |

    另外这两种都**不行**：
    - `{"bm25": {1: 0.5}}` → 400 Format error（`VectorStruct` 是 untagged
      enum，`{索引:权重}` 对象会被当成命名 dense 向量解析）
    - `{"bm25": [[1, 0.5]]}` → 400 multi/regular conversion failed

    旧代码走 qdrant-client 的 `SparseVector` 类型所以没踩到，直接调 REST 会踩。
    """
    items = sorted(weights.items())
    return {
        "indices": [i for i, _ in items],
        "values": [w for _, w in items],
    }


def sparse_query(weights: dict[int, float]) -> dict[str, Any]:
    """稀疏**查询**向量（格式与写入不同，见 `sparse_vector` 的坑表）。"""
    return {"name": SPARSE_VECTOR_NAME, **sparse_vector(weights)}


def upsert_points(
    collection: str,
    points: list[dict[str, Any]],
    *,
    wait: bool = True,
    chunk_size: int = 512,
) -> int:
    """批量写入点。

    @param chunk_size 单请求最大点数。一个 2000 页文档切出上万 chunk 时，
        一次 POST 会被拒（payload 过大），必须分批。
    """
    if not points:
        return 0
    # 防御：写入稀疏点时自动包上向量名，调用方不必自己拼
    if collection == COLL_SPARSE:
        points = [
            {
                **p,
                "vector": (
                    {SPARSE_VECTOR_NAME: v}
                    if isinstance(v, dict) and "indices" in v
                    else v
                ),
            }
            for p in points
            for v in [p["vector"]]
        ]
    c = get_client()
    written = 0
    for i in range(0, len(points), chunk_size):
        batch = points[i : i + chunk_size]
        r = c.put(
            f"/collections/{collection}/points?wait={'true' if wait else 'false'}",
            json={"points": batch},
        )
        r.raise_for_status()
        written += len(batch)
    return written


def delete_by_filter(collection: str, flt: dict[str, Any]) -> None:
    """按 filter 删点。

    用于文档删除（D26）：Qdrant 里的点必须跟着文档走，不能留孤儿点 ——
    否则检索会命中已删除内容（PG 二次过滤能挡住，但白占内存和召回名额）。
    """
    c = get_client()
    r = c.post(
        f"/collections/{collection}/points/delete?wait=true",
        json={"filter": flt},
    )
    r.raise_for_status()
