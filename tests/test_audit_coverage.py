"""🔴 D139：**每一次检索都必须记审计**（M8 写的是"每次检索/问答记录
`api_key → user → kb_id → query`"）。

## 这条要求的原文与实测的差距

`REQUIREMENTS.md` §3 的 M8 那一行写着"每次检索/问答记录"。2026-10-05
逐个端点实测审计行数（真库 `audit_log` 前后计数）：

| 端点 | 修之前 | 修之后 |
|---|---|---|
| `POST /kb/{id}/search` | 1 条 | 1 条 |
| `POST /search`（跨库） | 1 条 | 1 条 |
| 🔴 `POST /kb/{id}/search/debug` | **0 条** | 1 条 |
| 🔴 `GET /kb/{id}/ask/preview` | **0 条** | 1 条 |

两个漏的都跑的是同一个 `engine.search`（§0.3：检索唯一实现），而
`ask/preview` 还把**正文片段**（`text[:400]`）返回给调用方 ——
也就是说"谁在拿我的库探召回"在审计里查不到。

## 为什么要有这个"接线守卫"

审计是**横切关注点**：每个端点都得自己记得写一行 ⇒ 加了新端点一定会漏
（D139 就是这么漏的）。所以这里钉的不是"这两个端点现在写了"，
而是"**清单上的每个端点，其调用链里必须有 `write_audit` 且被 `await`**"。

⚠️ 两个判据细节，都是被自己的第一版打回来才写对的：

1. **要走传递闭包**。MCP 记在 `_tools_call` 里、A2A 记在 `_audit` 里，
   端点函数体本身一个都没有 ⇒ 只看函数体会把它们判成"没记审计"，
   那是**判据指错位置**（§5.138 那一族），不是缺陷。
2. **"被 await"要真的检查 await**。`ast` 里 `await f()` 的**父节点**是
   `Await`、子节点才是 `Call`；第一版写成
   `{id(c) for c in calls if isinstance(c, ast.Await)}` ——
   `calls` 里装的是 `Call`，`isinstance` 恒为 False，于是这条断言**恒红**。
   正确写法是从 `Await` 节点反向取 `id(await_node.value)`。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _pg_alive() -> bool:
    from sqlalchemy import text

    from services.common.db import sync_session_scope

    try:
        with sync_session_scope() as s:
            s.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


pg = pytest.mark.skipif(not _pg_alive(), reason="Postgres 不在线")

#: **必须**记审计的端点实现，及其**应该记的 action**
#: （`GET /audit?action=…` 是唯一的查询面 ⇒ action 记错等于"查不到"）。
MUST_AUDIT_ACTION: dict[tuple[str, str], str] = {
    ("services/api/routers/search.py", "search_kb"): "ACTION_SEARCH",
    ("services/api/routers/search.py", "search_cross"): "ACTION_SEARCH",
    ("services/api/routers/search.py", "search_debug"): "ACTION_SEARCH",
    ("services/api/routers/ask.py", "ask_kb"): "ACTION_ASK",
    # ⚠️ `ask_preview` 记成 `ACTION_SEARCH`：**它没有生成任何东西**，
    #   记成 ask 会让"生成过答案"这个统计虚高（D139）
    ("services/api/routers/ask.py", "ask_preview"): "ACTION_SEARCH",
    ("services/api/routers/mcp.py", "mcp_endpoint"): "ACTION_MCP_CALL",
    ("services/api/routers/a2a.py", "a2a_tasks"): "ACTION_A2A_CALL",
}

#: **必须**记审计的端点实现（`(文件, 函数名)`）。
#: 🔑 判据是"调用链里真的调了 `write_audit`"，而**不是**"文件里出现过这个词"
#:   —— 后者会让"注释里提一句"就通过（§5.65 的老坑）。
#: ⚠️ 这几个函数名是**实测**出来的：第一版凭印象猜了 `upload` / `handle_rpc`
#:   / `a2a_task`，守卫当场报"找不到函数" —— 这正是要"响亮失败而不是跳过"的理由。
MUST_AUDIT: list[tuple[str, str]] = [
    # 检索（返回命中 / 各腿 / 正文片段）
    ("services/api/routers/search.py", "search_kb"),
    ("services/api/routers/search.py", "search_cross"),
    ("services/api/routers/search.py", "search_debug"),      # D139 补的
    ("services/api/routers/ask.py", "ask_kb"),
    ("services/api/routers/ask.py", "ask_preview"),          # D139 补的
    # 导入（"谁往库里塞了东西"）与外部集成
    ("services/api/routers/imports.py", "import_chat"),
    ("services/api/routers/imports.py", "import_dir"),
    ("services/api/routers/mcp.py", "mcp_endpoint"),
    ("services/api/routers/a2a.py", "a2a_tasks"),
]

_ROUTER_RELS = (
    "services/api/routers/search.py",
    "services/api/routers/ask.py",
    "services/api/routers/mcp.py",
    "services/api/routers/a2a.py",
)


def _module_funcs(rel: str) -> dict:
    """模块里所有**顶层**函数/协程，按名字索引。"""
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    return {n.name: n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _must_have(rel: str, name: str) -> None:
    if name not in _module_funcs(rel):
        pytest.fail(f"{rel} 里找不到函数 {name} —— 改名了？清单要一起改，"
                    f"**别把这条悄悄删掉**")


def _is_write_audit(call: ast.Call) -> bool:
    return ((isinstance(call.func, ast.Name) and call.func.id == "write_audit")
            or (isinstance(call.func, ast.Attribute)
                and call.func.attr in ("write_audit", "write_audit_sync")))


def _write_audit_calls(node) -> list[ast.Call]:  # noqa: ANN001
    return [n for n in ast.walk(node)
            if isinstance(n, ast.Call) and _is_write_audit(n)]


def _reached(rel: str, name: str, _seen: set | None = None
             ) -> list[tuple[str, ast.Call, bool]]:
    """端点**（含它调到的模块内函数）**里的 `write_audit` 调用。

    @return `(所在函数名, 调用节点, 是否被 await)` 的列表
    """
    funcs = _module_funcs(rel)
    seen = _seen if _seen is not None else set()
    if name in seen or name not in funcs:
        return []
    seen.add(name)
    node = funcs[name]
    awaited_ids = {id(a.value) for a in ast.walk(node) if isinstance(a, ast.Await)}
    out = [(name, c, id(c) in awaited_ids) for c in _write_audit_calls(node)]
    for call in ast.walk(node):
        if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id in funcs and call.func.id != name):
            out += _reached(rel, call.func.id, seen)
    return out


@pytest.mark.parametrize("rel,name", MUST_AUDIT,
                         ids=[f"{Path(r).name}:{n}" for r, n in MUST_AUDIT])
def test_endpoint_audits(rel: str, name: str):
    """🔑 该端点的调用链里**恰好一处** `write_audit`。"""
    _must_have(rel, name)
    hits = _reached(rel, name)
    assert hits, f"{rel}::{name} 的调用链里没有 write_audit（审计是横切关注点，" \
                 f"加端点时忘了写就会这样）"
    assert len(hits) == 1, (
        f"{rel}::{name} 的调用链里有 {len(hits)} 处 write_audit"
        f"（{[h[0] for h in hits]}），应为 1 —— 多处意味着一次调用可能记两条"
    )


@pytest.mark.parametrize("rel,name", MUST_AUDIT,
                         ids=[f"{Path(r).name}:{n}" for r, n in MUST_AUDIT])
def test_audit_call_is_awaited(rel: str, name: str):
    """🔑 `write_audit` **必须被 await**。

    ⚠️ 这是异步函数：漏 `await` 会返回一个**没被执行的协程** ——
    审计**静默不写**、请求照样 200，与 §5.90「异步去重漏 `await`」
    是同一个形状的静默失败。
    """
    hits = _reached(rel, name)
    assert hits, "调用链里没有 write_audit"
    not_awaited = [h[0] for h in hits if not h[2]]
    assert not not_awaited, (
        f"{rel}::{name} 的 write_audit 没有 await（在 {not_awaited}）"
        " ⇒ 协程不会被执行，审计静默丢失而请求仍然 200"
    )


@pytest.mark.parametrize("rel,name", list(MUST_AUDIT_ACTION),
                         ids=[f"{Path(r).name}:{n}" for r, n in MUST_AUDIT_ACTION])
def test_audit_records_the_right_action(rel: str, name: str, ):
    """🔑 记的 `action` 要**对** —— `GET /audit?action=…` 是唯一的查询面。

    ⚠️ 记错 action 的后果与没记一样：**按 action 查就查不到**
    （`audit.py` 的 action 白名单只有那 6 个值，写错还会直接报错）。
    """
    want = MUST_AUDIT_ACTION[(rel, name)]
    hits = _reached(rel, name)
    assert hits, "调用链里没有 write_audit"
    found: list[str] = []
    for fname, call, _awaited in hits:
        for kw in call.keywords or []:
            if kw.arg == "action":
                found.append(ast.unparse(kw.value))
    assert found, f"{rel}::{name} 的 write_audit 没有写 action= 关键字"
    assert want in found, (
        f"{rel}::{name} 记的 action 是 {found}，应为 {want}"
        "（按 action 查审计时这条会查不到）"
    )


def test_no_retrieval_endpoint_outside_the_list():
    """🔑 反向守卫：所有**真的跑了检索**的端点函数都必须在清单里（或有审计）。

    ⚠️ 判据不是"文件里出现了 `search`"（会命中 helper 与注释），
    而是"这个函数（**或它调到的模块内函数**）里出现了 `search` 调用"。
    """
    listed = {n for _, n in MUST_AUDIT}
    missing: list[str] = []
    for rel in _ROUTER_RELS:
        funcs = _module_funcs(rel)
        for name, node in funcs.items():
            if name.startswith("_"):
                continue
            names = {
                c.func.id for c in ast.walk(node)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            }
            calls_search = bool(names & {"search", "search_routed", "engine"})
            if not calls_search:
                # 也看它调到的模块内函数（MCP/A2A 就是这么写的）
                calls_search = any(
                    isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                    and c.func.id in funcs
                    and bool({
                        x.func.id for x in ast.walk(funcs[c.func.id])
                        if isinstance(x, ast.Call) and isinstance(x.func, ast.Name)
                    } & {"search", "search_routed", "engine"})
                    for c in ast.walk(node)
                )
            if not calls_search:
                continue
            if name not in listed and not _reached(rel, name):
                missing.append(f"{Path(rel).name}::{name}")
    assert not missing, (
        "这些函数跑了检索却没记审计（M8：每次检索/问答记录）：" + ", ".join(missing)
    )


@pg
def test_audit_table_is_queryable_and_has_the_triple():
    """🔑 审计行里有**三元组**（api_key → user → kb）—— 那是 M8/D8 的核心。

    顺带证明"`audit_log` 真的可查"：一个空断言只能证明测试跑过了，
    而这里断言的是**结构**（三列都非空）。
    """
    from sqlalchemy import select

    from services.common.db import sync_session_scope
    from services.common.models import AuditLog

    with sync_session_scope() as s:
        # 🔑 查**最近带 api_key_id 的**行，而不是"最近 200 行里至少一条带 key"。
        #   后者依赖"最近刚跑过 eval"—— 而测试客户端（MCP/A2A 的 schema 测试）
        #   会往审计表写**不带 key** 的行，跑一轮 pytest 就把最近 200 行刷成
        #   全 NULL，于是这条守卫**假红**（§5.167 同一族的"状态依赖"）。
        rows = s.execute(
            select(AuditLog.api_key_id, AuditLog.user_id, AuditLog.kb_ids)
            .where(AuditLog.api_key_id.isnot(None))
            .order_by(AuditLog.id.desc()).limit(50)
        ).all()
    assert rows, (
        "审计表里没有任何带 api_key_id 的行 —— 端到端路径可能没在写"
        "（先跑一遍 eval/api_1d_check.py 造出真 API 调用）"
    )

    # 🔑 真实不变量是"**有 key 就必有 user**"，不是"每行都有 key"：
    # 批量评估脚本（`eval/retrieval_eval.py` 那类）以系统身份写行，
    # 库里 1409 条 search 审计里只有 28 条带 key —— 那是**合法**的。
    # 判据抓的是那种"半截记录"（有 key 没 user ⇒ 追不到人）。
    half = [r for r in rows if (r[0] is None) != (r[1] is None)]
    assert not half, f"有 api_key_id 却没有 user_id（或反过来）的半截记录：{half[:3]}"
    for api_key_id, _user_id, kb_ids in rows:
        assert isinstance(kb_ids, list), type(kb_ids)