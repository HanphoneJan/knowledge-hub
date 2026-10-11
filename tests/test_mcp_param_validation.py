"""🔴 D144：MCP `tools/call` 的**参数校验**（此前缺参数会变成裸 Python 异常）。

## 缺口是怎么确认的

实测 `POST /mcp`（2026-10-05，M11 审计）：

| 调用 | 修之前 | 修之后 |
|---|---|---|
| `search_kb` **缺** `kb_id` | `-32603` · `KeyError: 'kb_id'` | `-32602` · `search_kb: 缺少必填参数 kb_id` |
| `kb_id` 传 `"不是数字"` | `-32603` · `ValueError: invalid literal for int() …` | `-32602` · `search_kb: kb_id 必须是整数，收到 '不是数字'` |
| `top_k=9999`（schema 写 maximum=50）| 静默**夹取**成 50 | `-32602` · `top_k 不能大于 50（收到 9999）` |

两个问题：

1. 🔴 **错误码错**：JSON-RPC 2.0 §5.1.2 规定"参数不合法"是 **-32602**
   （`Invalid params`），-32603 是"服务端内部错误"。把调用方的错报成内错，
   客户端会以为要重试或报 bug，而不是"改参数"。
2. 🔴 **message 是裸 Python 异常**：既不告诉调用方要改什么，
   也泄露了内部实现（`KeyError` 说明服务端用字典下标取参）。

⚠️ 与"业务失败"的界线（这一条容易搞混，`tools.py` 里原本就有注释提醒）：
**参数不合法 = 协议错（-32602）**；**请求合法但结果失败**（库不存在 / 无权限）
= `isError: true` 的**结果**。混起来的话，模型会把"我少传一个参数"
误读成"这个库不存在"，于是换一个不存在的 `kb_id` 反复试。

## 越界为什么不静默夹取

HTTP 侧 `POST /kb/{id}/search` 的 `top_k` 是**夹取**的（给人调用，报错不友好）；
但 MCP 侧的调用方**多半是个 LLM**，schema 已经把上限告诉它了 ——
悄悄改成 50 会让"我要 80 条"变成"我拿到 50 条"而**无人察觉**，
于是它在下游基于 50 条做判断。⇒ 报错，让它自己决定。
"""

from __future__ import annotations

import pytest

from services.mcp.protocol import ProtocolError
from services.mcp.tools import BY_NAME, INVALID_PARAMS, Tool, validate_args


@pytest.fixture(scope="module", autouse=True)
def _own_the_async_engine():
    """🔑 进出都丢掉 async 引擎单例（本文件的请求会真的建连接池）。

    ⚠️ **只丢单例不够**：真正的坑是 `TestClient` —— 它**每次调用**都开一个新
    event loop，于是第一个请求把 asyncpg 连接池建在 loop A 上，
    第二个请求在 loop B 上用它 ⇒
    `got Future … attached to a different loop`（§5.122 记过这个形状）。

    ⇒ 两条一起做：① 下面 `client` fixture 用 `with TestClient(...)`
    （**进入上下文**才会让整个块共用一个 portal/loop）；
    ② 进入与退出都 `dispose_engines()`，别把池泄漏给后面的测试文件。
    """
    from services.common.db import dispose_engines

    dispose_engines()
    yield
    dispose_engines()


# ============================================================
# 1. 校验器本身
# ============================================================


def test_missing_required_is_reported_with_the_tool_name_hint():
    problems = validate_args(BY_NAME["search_kb"], {"query": "x"})
    assert problems == ["缺少必填参数 kb_id"], problems


def test_all_missing_are_reported_at_once():
    """🔑 一次报**全部**问题 —— 逐个报错会让 LLM 试五次。"""
    problems = validate_args(BY_NAME["search_kb"], {})
    assert len(problems) == 2, problems
    assert any("kb_id" in p for p in problems)
    assert any("query" in p for p in problems)


def test_non_integer_is_rejected_with_the_received_value():
    problems = validate_args(BY_NAME["search_kb"], {"kb_id": "不是数字", "query": "x"})
    assert problems and "必须是整数" in problems[0], problems
    assert "不是数字" in problems[0], "报错里要带**收到的值**，否则调用方无从判断"


def test_bool_is_not_accepted_as_integer():
    """🔑 `bool` 是 `int` 的子类：`kb_id=True` 若不显式排除，会被当成 `kb_id=1`。"""
    problems = validate_args(BY_NAME["search_kb"], {"kb_id": True, "query": "x"})
    assert problems and "必须是整数" in problems[0], problems


def test_numeric_string_is_accepted():
    """🔑 数字**字符串**要接受：很多 MCP 客户端从 JSON 里把整数读成字符串。"""
    assert validate_args(BY_NAME["search_kb"],
                         {"kb_id": "7", "query": "x", "top_k": "3"}) == []


def test_out_of_range_is_rejected_not_silently_clamped():
    """🔑 越界**报错**：调用方多半是 LLM，悄悄夹取会让它拿着 50 条当 80 条用。"""
    problems = validate_args(BY_NAME["search_kb"],
                             {"kb_id": 1, "query": "x", "top_k": 9999})
    assert problems and "不能大于 50" in problems[0], problems
    low = validate_args(BY_NAME["search_kb"], {"kb_id": 1, "query": "x", "top_k": 0})
    assert low and "不能小于 1" in low[0], low


def test_enum_is_enforced():
    problems = validate_args(BY_NAME["search_kb"],
                             {"kb_id": 1, "query": "x", "mode": "zzz"})
    assert problems and "只能是" in problems[0], problems


def test_unknown_argument_is_rejected():
    """🔑 schema 写着 `additionalProperties: false` ⇒ 未知参数也要报
    （拼错 `kb_lid` 的话，不报就会一路走到"库不存在"）。"""
    problems = validate_args(BY_NAME["search_kb"],
                             {"kb_id": 1, "query": "x", "kb_lid": 2})
    assert problems and "未知参数 kb_lid" in problems[0], problems


def test_arguments_must_be_an_object():
    problems = validate_args(BY_NAME["list_kb"], ["not", "a", "dict"])
    assert problems and "必须是对象" in problems[0], problems


def test_valid_arguments_pass_cleanly_for_every_tool():
    """🔑 **每个**工具的合法调用都零问题（别把正常路径也拦了）。"""
    good = {
        "list_kb": {},
        "search_kb": {"kb_id": 1, "query": "x"},
        "ask_kb": {"kb_id": 1, "question": "q", "session_id": 5, "top_k": 3},
        "get_document": {"kb_id": 1, "doc_id": 2, "limit": 5},
    }
    assert set(good) == set(BY_NAME), set(BY_NAME) - set(good)
    for name, args in good.items():
        assert validate_args(BY_NAME[name], args) == [], (name, args)


def test_error_code_is_invalid_params():
    """🔑 JSON-RPC 2.0 §5.1.2：参数不合法是 **-32602**，不是 -32603。"""
    assert INVALID_PARAMS == -32602


# ============================================================
# 2. 端点层（真 HTTP）
# ============================================================


@pytest.fixture(scope="module")
def client():
    """一个**贯穿整个模块**的 TestClient（共用 portal/loop，见上面那条）。"""
    from fastapi.testclient import TestClient

    from services.api.main import app

    with TestClient(app, client=("127.0.0.1", 12345)) as c:
        yield c


@pytest.fixture(scope="module")
def _mcp_key(purge_users):
    """真注册一个账号拿 Key（MCP 走真实的鉴权链路）。"""
    import time

    import httpx

    c = httpx.Client(base_url="http://127.0.0.1:8010", trust_env=False, timeout=30)
    # 🔴 **这个 fixture 要一个真在跑的 API**（它用真注册链路换 Key，不是 TestClient）。
    #   所以：连不上就 `skip`，**不能**让它抛 ConnectError ——
    #   抛出去会把同模块里那一堆纯单测（`validate_args` 一族）一起连坐成 ERROR，
    #   症状是"整套挂了"，而真正没跑的只有 3 个集成用例。
    try:
        c.get("/health", timeout=3)
    except httpx.HTTPError as exc:
        pytest.skip(f"需要一个在跑的 API（http://127.0.0.1:8010）：{exc!r}")
    mail = f"mcp-d144-{time.time():.0f}@t.local"
    r = c.post("/api/v1/auth/register",
               json={"email": mail, "password": "pw12345678", "display_name": "x"})
    if r.status_code != 201:
        pytest.skip(f"注册失败（API 没起？）：HTTP {r.status_code}")
    key = r.json()["api_key"]
    yield key
    # 🔑 清场**必须**重建 BM25（`conftest.purge_users`）：裸
    #   `DELETE FROM users` 会级联删掉 chunks 而不动 df ⇒ 下一次 pytest
    #   假红在 `test_corpus_stats_matches_live_chunks`（§5.109、D146）。
    purge_users([mail])


@pytest.mark.parametrize("args,expect_in", [
    ({"query": "x"}, "缺少必填参数 kb_id"),
    ({"kb_id": "不是数字", "query": "x"}, "必须是整数"),
    ({"kb_id": True, "query": "x"}, "必须是整数"),
    ({"kb_id": 1, "query": "x", "top_k": 9999}, "不能大于 50"),
    ({"kb_id": 1, "query": "x", "mode": "zzz"}, "只能是"),
    ({"kb_id": 1, "query": "x", "kb_lid": 2}, "未知参数 kb_lid"),
])
def test_endpoint_returns_invalid_params_with_actionable_message(
        client, _mcp_key, args, expect_in):
    """🔑 端点真的返回 `-32602` + **可操作**的消息（含工具名与收到的值）。"""
    c = client
    r = c.post("/mcp", headers={"Authorization": f"Bearer {_mcp_key}"},
               json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "search_kb", "arguments": args}})
    assert r.status_code == 200, r.text[:200]
    err = r.json()["error"]
    assert err["code"] == INVALID_PARAMS, err
    assert "search_kb" in err["message"], err["message"]
    assert expect_in in err["message"], err["message"]
    # 🔑 绝不能是裸 Python 异常（那是 D144 之前的症状）
    for leak in ("KeyError", "ValueError", "Traceback", "TypeError"):
        assert leak not in err["message"], err["message"]


def test_business_failure_stays_is_error_not_protocol_error(client, _mcp_key):
    """🔑 **界线**：库不存在是**业务失败**（`isError: true`），不是 -32602。

    ⚠️ 这条与上面那条互为对照：混起来的话，模型会把"我少传一个参数"
    误读成"这个库不存在"，于是换一个不存在的 kb_id 反复试。
    """
    c = client
    r = c.post("/mcp", headers={"Authorization": f"Bearer {_mcp_key}"},
               json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "get_document",
                                "arguments": {"kb_id": 999999, "doc_id": 1}}})
    body = r.json()
    assert "error" not in body, body
    assert body["result"]["isError"] is True, body
    assert "KB_NOT_FOUND" in str(body["result"]), body


def test_unknown_tool_still_method_not_found(client, _mcp_key):
    """🔑 未知工具仍是 -32601（别被新的参数校验抢走）。"""
    c = client
    r = c.post("/mcp", headers={"Authorization": f"Bearer {_mcp_key}"},
               json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "rm_rf", "arguments": {}}})
    assert r.json()["error"]["code"] == -32601, r.json()