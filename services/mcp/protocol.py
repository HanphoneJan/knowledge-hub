"""MCP 的 JSON-RPC / Streamable HTTP 协议层（**纯逻辑**，不碰 DB/网络）。

## 为什么手写而不是用官方 SDK

`uv add mcp` 在这台机器上拉不动（经 fclash 代理取 PyPI wheel 元数据
`tls handshake eof`，重试 3 次都失败；HF 模型能拉，PyPI 这条路不通）。
而 MCP 的 tool-calling 面需要的协议子集很小：

| 方法 | 方向 | 我们要做的 |
|---|---|---|
| `initialize` | c→s | 版本协商 + 回 `{protocolVersion, capabilities, serverInfo}` |
| `notifications/initialized` | c→s | 通知（**不回响应**，回响应是协议错误） |
| `tools/list` | c→s | 回工具表 |
| `tools/call` | c→s | 调工具 → 回 `content[]` + `isError` |

剩下的是 JSON-RPC 2.0 的信封（`jsonrpc`/`id`/`result`/`error`）与错误码映射。
所以这个模块只做信封与分发，**工具实现在 `tools.py`**，
两者都复用与 HTTP 路由同一套检索/问答内核（D10）。

## 三条协议细节（写错了外部客户端就接不上）

1. **通知没有 `id`，也不能回响应**：带 `id` 的 `notifications/*` 是协议错误。
   回一个 response 会让客户端的请求-响应配对错位。
2. **错误用 JSON-RPC `error`（带 code），不用 HTTP 状态**：
   工具执行失败是 `result.isError=true`（业务错误，可被模型读懂），
   而协议/鉴权错误才走 `error`（如 `-32601` 方法未知、`-32602` 参数非法）。
   ⚠️ 这条搞反的话，模型看到的是协议错误而不是"这个库不存在"。
3. **`tools/list` 的工具要按权限裁剪**（契约 §10.2）：
   `kb_id` 参数的 schema 用 **enum 列出有权限的库** ——
   不能"先返回全部工具、调用时再拒绝"，那等于让调用方探测到不该看到的 KB。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

#: 我们支持的协议版本（按新到旧）。`initialize` 时与客户端协商：
#: 客户端报的是其中之一就用它的，否则用我们最新的那个。
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26")

#: JSON-RPC 标准错误码（只列我们真会返回的）
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class ProtocolError(Exception):
    """协议层错误 → JSON-RPC `error` 响应。

    ⚠️ **不要**用它表示"业务失败"（例如"库不存在"）：
    那些要用 `result.isError=true`（见模块 docstring 第 2 条）——
    模型能读懂前者，读不懂后者。
    """

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_error(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            out["data"] = self.data
        return out


@dataclass
class Request:
    """一条 JSON-RPC 请求（或通知）。"""

    method: str
    params: dict[str, Any] = field(default_factory=dict)
    id: Any = None
    is_notification: bool = False


@dataclass
class Response:
    """一条 JSON-RPC 响应。通知没有响应（`body=None`）。"""

    body: dict[str, Any] | None
    #: HTTP 状态码。⚠️ 协议错误也回 **200**（JSON-RPC 的错误在 body 里），
    #: 只有"根本不是 JSON-RPC"（解析失败）才用 400 ——
    #: 否则客户端库会把 HTTP 错误当成传输故障，拿不到 error.code。
    http_status: int = 200


def negotiate_version(client: str | None) -> str:
    """协议版本协商：客户端报的我们支持就**用它的**，否则用最新的。"""
    if client in SUPPORTED_PROTOCOL_VERSIONS:
        return client
    return SUPPORTED_PROTOCOL_VERSIONS[0]


def parse_request(raw: bytes | str) -> list[Request]:
    """解析请求体。**批量**请求也要支持（协议允许数组）。

    ⚠️ 单个请求也可能是 `{"jsonrpc": ...}` 而不是数组 —— 两种都要收。
    """
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError(PARSE_ERROR, f"JSON 解析失败: {exc}") from exc

    items = data if isinstance(data, list) else [data]
    if not items:
        raise ProtocolError(INVALID_REQUEST, "空的请求（既不是对象也不是非空数组）")
    return [_one(i) for i in items]


def _one(item: Any) -> Request:
    if not isinstance(item, dict):
        raise ProtocolError(INVALID_REQUEST, "请求项必须是对象")
    if item.get("jsonrpc") != "2.0":
        raise ProtocolError(
            INVALID_REQUEST, f"jsonrpc 必须是 '2.0'，收到 {item.get('jsonrpc')!r}"
        )
    method = item.get("method")
    if not isinstance(method, str) or not method:
        raise ProtocolError(INVALID_REQUEST, "缺少 method")
    # ⚠️ 不能用 `or {}`：那样 `params: []`（空数组，falsy）会被当成
    #   "没传 params"而放过，而 `params: [1]` 才报错 ——
    #   同一类错误两种表现，客户端会以为自己没写对格式。
    params = item.get("params")
    if params is None:
        params = {}
    elif not isinstance(params, dict):
        raise ProtocolError(
            INVALID_PARAMS, f"params 必须是对象，收到 {type(params).__name__}"
        )
    # 🔑 有 id 才是请求；没有 id 就是通知（**不能回响应**）
    has_id = "id" in item and item["id"] is not None
    return Request(
        method=method, params=params, id=item.get("id"),
        is_notification=method.startswith("notifications/") or not has_id,
    )


def ok(request: Request, result: Any) -> Response | None:
    """成功响应。**通知返回 None**（不构造 body）。"""
    if request.is_notification:
        return None
    return Response({"jsonrpc": "2.0", "id": request.id, "result": result})


def err(request: Request, error: ProtocolError) -> Response | None:
    """协议错误响应。通知上的错误**丢弃**（无处回）。"""
    if request.is_notification:
        return None
    return Response(
        {"jsonrpc": "2.0", "id": request.id, "error": error.to_error()}
    )


def text_content(text: str, *, is_error: bool = False) -> dict[str, Any]:
    """工具结果 → MCP `content` 项。

    ⚠️ **一律用 `text` 内容**：MCP 的内容类型里 `text` 是所有客户端都必须
    支持的一种（`image`/`resource` 都可以不支持）。
    结构化数据用 JSON 字符串塞进 `text`，而不是自造 content 类型 ——
    自造的会被客户端当"未知类型"丢掉，表现为"工具调用成功但没有内容"。
    """
    return {
        "content": [{"type": "text", "text": text}],
        "isError": bool(is_error),
    }


def json_content(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    """结构化结果 → `content[0].text` 是 pretty JSON。

    ⚠️ `ensure_ascii=False`：中文结果转成 `\\uXXXX` 会让模型读起来费劲
    （它按 token 计费，也更容易在引用里写错字）。
    """
    return text_content(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        is_error=is_error,
    )


def error_content(message: str) -> dict[str, Any]:
    """业务失败 → `result.isError=true`（不是 JSON-RPC error，见模块 docstring）。"""
    return text_content(message, is_error=True)


__all__ = [
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "PARSE_ERROR",
    "ProtocolError",
    "Request",
    "Response",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "err",
    "error_content",
    "json_content",
    "negotiate_version",
    "ok",
    "parse_request",
    "text_content",
]