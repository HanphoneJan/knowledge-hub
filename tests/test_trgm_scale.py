"""🔑 D180：trgm 腿的规模闸（语句超时）。

## 实测（本机，~55 字/行的合成语料，`word_similarity` 召回条件）

| 行数 | 耗时 |
|---|---|
| 1 万 | ≈ 0.18 s |
| 10 万 | ≈ 1.4 s |
| 100 万 | ≈ **13.9 s** |

远超 D46 的 500ms 预算。GiST trgm 索引也救不了（函数在 WHERE 里，规划器仍走
Seq Scan）。⇒ 给这一腿封顶：超过 `trgm_statement_timeout_ms` 就降级成 `skipped`
（如实说明），文本召回仍由 dense / sparse 承担。

## 这一组钉

1. 配置存在且默认封顶（非 0）。
2. `search_trgm` 真的设了 `SET LOCAL statement_timeout`。
3. 超时 → skipped 里带 **D180** 的可操作说明（不是人口普查式的 "失败"）。
"""
from __future__ import annotations

import asyncio

from services.common.auth import UserContext
from services.retrieval.trgm import search_trgm

ROOT_CTX = UserContext(
    user_id=1, display_name="u", email="u@example.com", api_key_id=1,
    scopes=frozenset({"kb:read"}), workspace_ids=frozenset({1}),
    readable_kb_ids=frozenset({1}),
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Result:
    def all(self):
        return []


class _FakeSession:
    def __init__(self, exc: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.exc = exc

    async def execute(self, stmt, *a, **k):  # noqa: ANN001, ANN002, ANN003
        self.calls.append(str(stmt))
        if self.exc is not None:
            raise self.exc
        return _Result()


def test_config_defaults_to_a_cap():
    from services.common.config import Settings

    s = Settings(_env_file=None)
    assert s.trgm_statement_timeout_ms > 0, (
        "trgm 腿没有封顶 —— 库一大每次检索都会被它的 seq scan 拖到秒级（D180）"
    )


def test_search_trgm_sets_statement_timeout():
    s = _FakeSession()
    res = _run(search_trgm(s, ROOT_CTX, "入职日期那栏填什么"))
    assert any("statement_timeout" in c for c in s.calls), (
        "trgm 腿没有设语句超时 —— 大库会拖死检索（D180）"
    )
    assert res.skipped is None  # 正常路径：空结果，不是 skipped


def test_search_trgm_degrades_on_timeout_with_actionable_reason():
    exc = TimeoutError("canceling statement due to statement timeout")
    res = _run(search_trgm(_FakeSession(exc=exc), ROOT_CTX, "入职日期那栏填什么"))
    assert res.skipped, "超时必须降级成 skipped（不是抛出去）"
    assert "D180" in res.skipped, f"降级文案没有指到 D180：{res.skipped}"
    assert "trgm" in res.skipped.lower()
