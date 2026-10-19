"""元数据过滤（时间）与 payload 字段的判据（纯，进 `deploy/ci.sh`）。

文章 §Metadata：`created_at` / `updated_at` 应能用于**过滤**。这里钉三件事：
1. 无时间参数时**原样返回**（默认路径逐字不变）；
2. 有时间参数时往 `must` **追加一条**范围条件（与租户条件是 and，不是 or）；
3. payload 确实带了 `updated_at`（否则过滤会命中不到）。
"""

from __future__ import annotations

from services.common.qdrant import with_updated_range
from services.ingest.chunker import Chunk, build_payload


def _flt() -> dict:
    return {"must": [{"key": "kb_id", "match": {"value": 1}}]}


def test_noop_when_no_time_bounds():
    flt = _flt()
    assert with_updated_range(flt, None, None) is flt


def test_appends_range_condition_keeping_tenant():
    out = with_updated_range(_flt(), "2025-01-01T00:00:00+00:00", None)
    # 租户条件仍在第 0 位（没有把它挤掉 / 覆盖）
    assert out["must"][0] == {"key": "kb_id", "match": {"value": 1}}
    cond = out["must"][-1]
    assert cond["key"] == "updated_at"
    assert cond["range"] == {"gte": "2025-01-01T00:00:00+00:00"}


def test_both_bounds():
    out = with_updated_range(
        _flt(), "2025-01-01T00:00:00+00:00", "2025-12-31T00:00:00+00:00"
    )
    assert out["must"][-1]["range"] == {
        "gte": "2025-01-01T00:00:00+00:00",
        "lte": "2025-12-31T00:00:00+00:00",
    }


def test_does_not_mutate_input():
    flt = _flt()
    with_updated_range(flt, "2025-01-01T00:00:00+00:00", None)
    assert flt == {"must": [{"key": "kb_id", "match": {"value": 1}}]}


def _payload(**kw):
    return build_payload(
        Chunk(text="正文", kind="text"),
        chunk_index=0, kb_id=1, doc_id=2, workspace_id=3,
        visibility="private", source_kind="file", owner_id=4,
        file_name="a.md", mime_type="text/markdown", **kw,
    )


def test_payload_carries_updated_at():
    p = _payload(updated_at="2026-01-01T00:00:00+00:00")
    assert p["updated_at"] == "2026-01-01T00:00:00+00:00"


def test_payload_omits_updated_at_when_absent():
    assert "updated_at" not in _payload()
