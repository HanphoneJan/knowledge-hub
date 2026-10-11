"""🔑 MCP 协议层（`services/mcp/protocol.py`）的单测。

协议层的错**不会在代码里暴露** —— 表现为"外部客户端接不上"：
HTTP 状态不对、通知多了一个响应、`error` 与 `isError` 用反了。
所以这里逐条钉住规范里那几个"写错了就接不上"的点。
"""

from __future__ import annotations

import json

import pytest

from services.mcp.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    ProtocolError,
    err,
    error_content,
    json_content,
    negotiate_version,
    ok,
    parse_request,
    text_content,
)


# ============================================================
# 1. 版本协商
# ============================================================


def test_negotiate_supported_version():
    assert negotiate_version("2025-06-18") == "2025-06-18"
    assert negotiate_version("2025-03-26") == "2025-03-26"


@pytest.mark.parametrize("v", [None, "", "1999-01-01", "2026-99-99", 42])
def test_negotiate_falls_back_to_latest(v):
    """🔑 不认识的版本**回退到最新**，而不是报错。

    报错会让所有"比新"的客户端都接不上，而版本号本来就是协商用的。
    """
    assert negotiate_version(v) == "2025-06-18"


# ============================================================
# 2. 请求解析
# ============================================================


def test_parse_single_request():
    reqs = parse_request('{"jsonrpc":"2.0","id":1,"method":"tools/list"}')
    assert len(reqs) == 1
    assert reqs[0].method == "tools/list" and reqs[0].id == 1
    assert reqs[0].is_notification is False


def test_parse_batch():
    """协议允许批量；只支持单条的话第二个请求会被静默丢掉。"""
    reqs = parse_request(json.dumps([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
    ]))
    assert [r.method for r in reqs] == ["tools/list", "ping"]


def test_notification_has_no_id():
    """🔑 通知没有 id，且**不能回响应**。

    回一个响应会让客户端的请求-响应配对错位（它会等一个永远不来的回答）。
    """
    reqs = parse_request('{"jsonrpc":"2.0","method":"notifications/initialized"}')
    assert reqs[0].is_notification is True
    assert ok(reqs[0], {}) is None, "通知不该有响应体"
    assert err(reqs[0], ProtocolError(INVALID_REQUEST, "x")) is None


def test_method_without_id_is_treated_as_notification():
    """没有 `id` 的请求**按通知处理**（规范如此），即使它叫 `tools/list`。"""
    reqs = parse_request('{"jsonrpc":"2.0","method":"tools/list"}')
    assert reqs[0].is_notification is True


def test_rejects_wrong_jsonrpc_version():
    with pytest.raises(ProtocolError) as ei:
        parse_request('{"jsonrpc":"1.0","id":1,"method":"ping"}')
    assert ei.value.code == INVALID_REQUEST


def test_rejects_bad_json():
    with pytest.raises(ProtocolError) as ei:
        parse_request("{不是 json")
    assert ei.value.code == PARSE_ERROR


@pytest.mark.parametrize(
    "body,code",
    [
        ("[]", INVALID_REQUEST),                    # 空数组
        ('{"jsonrpc":"2.0"}', INVALID_REQUEST),      # 缺 method
        ('{"method":"ping"}', INVALID_REQUEST),      # 缺 jsonrpc
        ('{"jsonrpc":"2.0","method":"ping","params":[]}', INVALID_PARAMS),
        ('"just a string"', INVALID_REQUEST),
    ],
)
def test_malformed_shapes(body, code):
    with pytest.raises(ProtocolError) as ei:
        parse_request(body)
    assert ei.value.code == code


def test_accepts_bytes_and_str():
    assert parse_request(b'{"jsonrpc":"2.0","id":1,"method":"ping"}')[0].method == "ping"
    assert parse_request('{"jsonrpc":"2.0","id":1,"method":"ping"}')[0].method == "ping"


# ============================================================
# 3. 响应信封
# ============================================================


def test_ok_response_shape():
    req = parse_request('{"jsonrpc":"2.0","id":7,"method":"ping"}')[0]
    body = ok(req, {"a": 1}).body
    assert body == {"jsonrpc": "2.0", "id": 7, "result": {"a": 1}}


def test_error_response_shape():
    req = parse_request('{"jsonrpc":"2.0","id":"abc","method":"x"}')[0]
    body = err(req, ProtocolError(METHOD_NOT_FOUND, "未知方法", {"m": "x"})).body
    assert body["id"] == "abc"          # id 原样回（字符串 id 很常见）
    assert body["error"]["code"] == METHOD_NOT_FOUND
    assert body["error"]["data"] == {"m": "x"}
    assert "result" not in body


def test_error_without_data_omits_it():
    req = parse_request('{"jsonrpc":"2.0","id":1,"method":"x"}')[0]
    assert "data" not in err(req, ProtocolError(INTERNAL_ERROR, "boom")).body["error"]


def test_id_zero_is_a_valid_id():
    """⚠️ `id: 0` 是合法 id —— 用 `if not id` 判"有没有 id"会把 0 当成通知。"""
    req = parse_request('{"jsonrpc":"2.0","id":0,"method":"ping"}')[0]
    assert req.is_notification is False and req.id == 0


def test_null_id_is_notification():
    req = parse_request('{"jsonrpc":"2.0","id":null,"method":"ping"}')[0]
    assert req.is_notification is True


# ============================================================
# 4. 工具结果形状
# ============================================================


def test_text_content_is_always_text_type():
    """🔑 内容类型必须用 `text` —— 客户端**必须**支持它。

    自造类型（image/resource 之外的）会被当"未知类型"丢掉，
    症状是"工具调用成功但没有内容"。
    """
    c = text_content("hello")
    assert c["content"][0]["type"] == "text"
    assert c["isError"] is False


def test_json_content_keeps_chinese_readable():
    """⚠️ 不要 `ensure_ascii=True`：中文变成 `\\uXXXX` 会让模型读起来费劲
    （按 token 计费，也更容易在引用里写错字）。"""
    c = json_content({"msg": "考察情况那栏填什么"})
    assert "\\u8003" not in c["content"][0]["text"]
    assert "考察情况那栏填什么" in c["content"][0]["text"]


def test_json_content_round_trips():
    payload = {"items": [{"doc_id": 1, "text": "多行\n文本"}], "total": 1}
    got = json.loads(json_content(payload)["content"][0]["text"])
    assert got == payload


def test_error_content_is_result_level_not_rpc_error():
    """🔑 业务失败用 `result.isError=true`（模型读得懂），
    而不是 JSON-RPC `error`（那是协议层给开发者看的）。"""
    c = error_content("这个库不存在")
    assert c["isError"] is True
    assert "error" not in c
    assert c["content"][0]["text"] == "这个库不存在"


# ============================================================
# 5. 协议常量与文档一致
# ============================================================


def test_error_codes_are_the_standard_ones():
    """标准码写错的话，客户端的自动重试/报错分支会走错。"""
    assert (PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND,
            INVALID_PARAMS, INTERNAL_ERROR) == (
        -32700, -32600, -32601, -32602, -32603)


def test_protocol_versions_are_declared():
    from services.mcp.protocol import SUPPORTED_PROTOCOL_VERSIONS

    assert SUPPORTED_PROTOCOL_VERSIONS[0] == "2025-06-18"
    assert "2025-03-26" in SUPPORTED_PROTOCOL_VERSIONS


# ============================================================
# 6. 四个工具的声明与裁剪（契约 §10.1/§10.2）
# ============================================================


def _ctx(*kb_ids: int):
    from services.common.auth import UserContext

    return UserContext(
        user_id=1, display_name="u", email="u@t.local", api_key_id=None,
        readable_kb_ids=frozenset(kb_ids),
    )


def test_tools_list_has_the_four_contract_tools():
    from services.mcp.tools import tools_list

    names = sorted(t["name"] for t in tools_list(_ctx(1)))
    assert names == ["ask_kb", "get_document", "list_kb", "search_kb"]


def test_tools_are_constant_across_calls():
    """🔑 `tools/list` 必须**深拷贝** schema 后再注入 enum。

    直接改 `TOOLS[i].schema` 的话，第一次调用的调用方能通过参数
    枚举探测到后续调用的权限（跨请求的信息泄漏）。
    """
    from services.mcp.tools import tools_list

    a = tools_list(_ctx(1))
    b = tools_list(_ctx(2, 3))
    enum_a = next(t for t in a if t["name"] == "search_kb")["inputSchema"][
        "properties"]["kb_id"]["enum"]
    enum_b = next(t for t in b if t["name"] == "search_kb")["inputSchema"][
        "properties"]["kb_id"]["enum"]
    assert enum_a == [1] and enum_b == [2, 3]


def test_no_readable_kb_hides_kb_tools():
    """一个库都没有 → **不暴露**三个带 kb_id 的工具（只留 list_kb）。

    留着空 enum 的后果是调用方每次调用都失败，
    那不如直接告诉它"你现在没有可用的库"。
    """
    from services.mcp.tools import tools_list

    names = [t["name"] for t in tools_list(_ctx())]
    assert names == ["list_kb"]


def test_list_kb_has_no_kb_param():
    from services.mcp.tools import tools_list

    t = next(x for x in tools_list(_ctx(1)) if x["name"] == "list_kb")
    assert "kb_id" not in t["inputSchema"]["properties"]


def test_every_tool_has_description():
    """外部 AI 靠 description 选工具；空描述 = 工具基本不会被选中。"""
    from services.mcp.tools import tools_list

    assert all(len(t["description"].strip()) > 10 for t in tools_list(_ctx(1)))


def test_top_k_has_a_maximum():
    """⚠️ 必须有上界：外部 AI 会传 `top_k=10000`，
    而 `MAX_CANDIDATES=100` 已经是单腿候选的上限（再大只是白跑）。"""
    from services.mcp.tools import BY_NAME, MAX_TOP_K

    props = BY_NAME["search_kb"].schema["properties"]["top_k"]
    assert props["maximum"] == MAX_TOP_K
    assert props["maximum"] <= 100


def test_top_k_is_clamped_not_rejected():
    """收敛而不是报错 —— 报错会让整个外部对话失败。"""
    from services.mcp.tools import MAX_TOP_K, _clamp_top_k

    assert _clamp_top_k(10000) == MAX_TOP_K
    assert _clamp_top_k(0) == 1
    assert _clamp_top_k(None) > 0
    assert _clamp_top_k("abc") > 0
    assert _clamp_top_k(-5) == 1