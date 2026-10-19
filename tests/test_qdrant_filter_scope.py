"""守卫：Qdrant 的租户过滤**只允许 `kb_id`**（防对齐报告 §4 的潜在陷阱成真）。

## 背景（这是"现在不是 bug、但一改就成 bug"的那一类）

`services/common/qdrant.py::PAYLOAD_INDEXES` 给 `visibility` / `workspace_id` /
`source_kind` 建了索引，payload 也带着它们 —— 但 `auth.build_qdrant_filter`
**只按 `kb_id` 过滤**（三档可见性在 PG 侧已折叠成 `ctx.readable_kb_ids`）。

payload 里的 `visibility` 是**摄取时写死**的（`ingest/chunker.py`），
改库可见性**不回写** ⇒ 一旦有人把过滤改成按 `visibility` 匹配，
陈旧值会**立刻**造成越权（改私密后仍被召回）。最小复现见
`reports/repro_kb_visibility.py`（当前 11/11，证明"现在不就范"）。

## 两条判据

1. **动态**：`build_qdrant_filter` 在**所有分支**（单库/跨库/空集/超管/无权限）
   产出的条件，`key` 只能是 `kb_id`，且整串里不得出现 ACL 字段名。
2. **静态**：`services/` 下**任何** API 过滤字典（`{"key": …}`）都不得用
   ACL 字段做 key。这是"防将来"的那一半 —— 有人新写一处就会红。

⚠️ 静态扫描带**正控制**（`test_scanner_sees_keys_and_positive_control`）：
先断言扫描器**确实扫到了** `kb_id`/`doc_id`，否则一个坏掉的扫描器
（永远返回空集）会让这条守卫"看起来全绿"（§5.149b 的等价变异教训）。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: 这些是 ACL/可见性字段。它们可以进 payload（审计/展示），
#: **绝不**能进过滤条件 —— 见模块 docstring。
ACL_FIELDS = {"visibility", "workspace_id", "source_kind", "owner_id"}
#: 允许出现在过滤里的 key。
ALLOWED_FILTER_KEYS = {"kb_id", "doc_id"}


# ============================================================
# 1. 动态：build_qdrant_filter 的所有分支
# ============================================================


def _ctx(**kw):
    from services.common.auth import UserContext

    base = dict(
        user_id=1, display_name="u", email="u@example.com", api_key_id=1,
        scopes=frozenset({"kb:read"}), workspace_ids=frozenset({7}),
        readable_kb_ids=frozenset({11, 12}),
    )
    base.update(kw)
    return UserContext(**base)


def _filter_keys(flt: dict) -> set[str]:
    keys: set[str] = set()
    for clause in ("must", "should", "must_not"):
        for cond in flt.get(clause, []) or []:
            if isinstance(cond, dict) and "key" in cond:
                keys.add(str(cond["key"]))
    return keys


CASES = [
    ("单库-有权限", _ctx(), {"kb_id": 11}),
    ("全库（无 kb_id）", _ctx(), {}),
    ("跨库-含权限", _ctx(), {"kb_ids": [12]}),
    ("跨库-全无权限", _ctx(), {"kb_ids": [99]}),
    ("单库-无权限", _ctx(), {"kb_id": 999}),
    ("空可读集合", _ctx(readable_kb_ids=frozenset()), {}),
    ("超管", _ctx(is_superadmin=True), {}),
]


@pytest.mark.parametrize("name,ctx,kw", CASES, ids=[c[0] for c in CASES])
def test_build_qdrant_filter_only_emits_kb_id(name, ctx, kw):
    from services.common.auth import build_qdrant_filter

    flt = build_qdrant_filter(ctx, kw.get("kb_id"), kb_ids=kw.get("kb_ids"))
    keys = _filter_keys(flt)
    assert keys <= {"kb_id"}, f"[{name}] 过滤里出现了非 kb_id 的条件：{keys}（{flt}）"
    blob = str(flt)
    for field in ACL_FIELDS:
        assert field not in blob, (
            f"[{name}] 过滤串里出现了 ACL 字段 {field!r} —— "
            "它会读到陈旧的 payload（越权），见 reports/repro_kb_visibility.py"
        )


# ============================================================
# 2. 静态：services/ 下不许有 {"key": <ACL 字段>} 的字面量
# ============================================================


def _iter_service_files():
    for p in sorted((ROOT / "services").rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        yield p


def _filter_key_literals(tree: ast.AST) -> list[str]:
    """收集 `{"key": "<字面量>"}` 里的那个字面量（如 `"kb_id"`）。"""
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        pairs = {
            k.value: v
            for k, v in zip(node.keys, node.values)
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
        kv = pairs.get("key")
        if isinstance(kv, ast.Constant) and isinstance(kv.value, str):
            out.append(kv.value)
    return out


def test_scanner_sees_keys_and_positive_control():
    """正控制：扫描器**确实**扫到了 `kb_id`/`doc_id`，否则守卫是空的。"""
    seen: set[str] = set()
    for path in _iter_service_files():
        seen.update(_filter_key_literals(ast.parse(path.read_text(encoding="utf-8"))))
    assert seen, "静态扫描器一个过滤 key 都没扫到 —— 判据失效（§5.149b）"
    assert {"kb_id", "doc_id"} <= seen, (
        f"正控制失败：没扫到预期的 kb_id/doc_id，实际扫到 {seen}"
    )


def test_no_service_filter_uses_an_acl_field():
    """🔑 `services/` 里任何过滤字典都不得用 ACL 字段做 key。

    **变异验证**：在任一服务文件里加一处 `{"key": "visibility", …}` ⇒ 本测试红。
    """
    offenders: list[str] = []
    for path in _iter_service_files():
        for key in _filter_key_literals(ast.parse(path.read_text(encoding="utf-8"))):
            if key in ACL_FIELDS:
                offenders.append(f"{path.relative_to(ROOT)} → {key!r}")
    assert not offenders, (
        "过滤条件用到了 ACL 字段（不可用陈旧 payload 判可见性）：\n  "
        + "\n  ".join(offenders)
    )
