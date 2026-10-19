"""MCP 服务端（D9）。

`protocol.py` 是协议信封（纯逻辑、可离线单测），`tools.py` 是四个工具
（复用与 HTTP 路由同一套检索/问答内核，D10）。传输层在
`services/api/routers/mcp.py`。
"""

from .protocol import (
    SUPPORTED_PROTOCOL_VERSIONS,
    ProtocolError,
    Response,
    Request,
    negotiate_version,
    parse_request,
)
from .tools import TOOLS, call_tool, tools_list

__all__ = [
    "SUPPORTED_PROTOCOL_VERSIONS",
    "TOOLS",
    "ProtocolError",
    "Request",
    "Response",
    "call_tool",
    "negotiate_version",
    "parse_request",
    "tools_list",
]
