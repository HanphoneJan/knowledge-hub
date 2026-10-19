"""`POST /mcp` —— MCP Streamable HTTP 传输（D9）。

## 传输层的三个决定

1. **无状态**：不实现 `Mcp-Session-Id`。协议允许服务端无状态，
   而本项目的鉴权是 Bearer API Key（每次请求自带身份），
   没有会话状态的必要。省掉它就不用处理"会话过期/泄漏"这一类问题。
2. **响应是 `application/json`**：Streamable HTTP 允许服务端对单条请求
   直接回 JSON（不必包成 SSE 帧）。⚠️ 协议错误也回 **HTTP 200**
   （错误在 body 的 `error` 里）—— 客户端库会把 HTTP 4xx 当传输故障，
   那样就拿不到 `error.code` 了。只有"根本不是 JSON-RPC"才回 400。
3. **鉴权复用现有依赖**：`CurrentUser` 走 API Key（与其它端点同源），
   权限裁剪只读 `ctx.readable_kb_ids`（§0.5），不重算可见性。

## 审计（D8）

每次 `tools/call` 都记 `audit_log`：`api_key_id → user_id → kb_ids → query 摘要 → 耗时`。
⚠️ 只审计 `tools/call`：`tools/list` 不带 query 也没改数据，
记它只会让审计表被"列了一次工具"这类噪声淹掉。
"""

from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Request, Response

from ...common.audit import ACTION_MCP_CALL, write_audit
from ...common.auth import UserContext
from ...common.deps import CurrentUser, DbSession
from ...mcp.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    SUPPORTED_PROTOCOL_VERSIONS,
    ProtocolError,
    Response as JsonRpcResponse,
    err,
    negotiate_version,
    ok,
    parse_request,
)
from ...mcp.tools import call_tool, tools_list

logger = logging.getLogger(__name__)
router = APIRouter(tags=["mcp"])

SERVER_INFO = {
    "name": "knowledge-hub",
    "version": "0.1.0",
    "title": "Knowledge Hub MCP Server",
}

JSONRPC_VERSION = "2.0"


@router.post("/mcp", summary="MCP Streamable HTTP（D9）")
async def mcp_endpoint(
    request: Request,
    ctx: CurrentUser,
    session: DbSession,
) -> Response:
    """处理一条（或一批）JSON-RPC 消息。"""
    raw = await request.body()
    t0 = time.perf_counter()
    try:
        requests = parse_request(raw)
    except ProtocolError as e:
        # ⚠️ 只有"不是 JSON-RPC"才用 400；协议级错误走 200 + error body
        status = 400 if e.code in (-32700, -32600) else 200
        body = {
            "jsonrpc": JSONRPC_VERSION, "id": None, "error": e.to_error()
        }
        return Response(
            json.dumps(body, ensure_ascii=False), status_code=status,
            media_type="application/json",
        )

    out: list[dict] = []
    for req in requests:
        resp = await _dispatch(req, session=session, ctx=ctx, t0=t0)
        if resp is not None and resp.body is not None:
            out.append(resp.body)

    if not out:
        # 全是通知 → 204（协议规定通知没有响应体）
        return Response(status_code=204)

    # 批量：始终回数组（协议允许单条请求回对象、批量回数组；
    # 回数组最安全 —— 客户端能统一处理）
    payload = out if len(out) > 1 or len(requests) > 1 else out[0]
    return Response(
        json.dumps(payload, ensure_ascii=False),
        media_type="application/json",
        headers={"MCP-Protocol-Version": SUPPORTED_PROTOCOL_VERSIONS[0]},
    )


async def _dispatch(
    req, *, session, ctx: UserContext, t0: float
) -> JsonRpcResponse | None:
    """单个 JSON-RPC 消息的处理。"""
    try:
        if req.method == "initialize":
            return ok(req, {
                "protocolVersion": negotiate_version(
                    req.params.get("protocolVersion")
                ),
                "capabilities": {
                    # 只声明 tools：没有 resources/prompts/logging，
                    # 声明了客户端会尝试用，而那必然失败
                    "tools": {"listChanged": False},
                },
                "serverInfo": SERVER_INFO,
                "instructions": (
                    "先调 list_kb 拿到可用的 kb_id，再按需要选 "
                    "search_kb（找资料）/ ask_kb（带引用的综合回答）。"
                ),
            })

        if req.method in ("notifications/initialized", "initialized"):
            return None                     # 通知：不回响应

        if req.method == "ping":
            return ok(req, {})

        if req.method == "tools/list":
            return ok(req, {"tools": tools_list(ctx)})

        if req.method == "tools/call":
            return await _tools_call(req, session=session, ctx=ctx, t0=t0)

        raise ProtocolError(
            METHOD_NOT_FOUND,
            f"未知方法 {req.method!r}；本服务器只实现 "
            f"initialize / tools/list / tools/call",
        )
    except ProtocolError as e:
        return err(req, e)
    except Exception as e:  # noqa: BLE001 — 服务器不能因一个请求整体崩
        logger.exception("MCP 处理失败 %s", req.method)
        return err(req, ProtocolError(
            INTERNAL_ERROR, f"{type(e).__name__}: {e}"
        ))


async def _tools_call(req, *, session, ctx: UserContext, t0: float) -> JsonRpcResponse:
    """`tools/call`：调工具 + 审计（模块 docstring 第 3 条）。"""
    name = req.params.get("name")
    if not isinstance(name, str) or not name:
        raise ProtocolError(INVALID_PARAMS, "tools/call 缺 name")
    args = req.params.get("arguments") or {}
    if not isinstance(args, dict):
        raise ProtocolError(INVALID_PARAMS, "arguments 必须是对象")

    result = await call_tool(name, args, session=session, ctx=ctx)

    # 审计：从入参里取 kb_id / query（不是从结果里反推）
    kb_ids = [args["kb_id"]] if isinstance(args.get("kb_id"), int) else []
    if name == "list_kb":
        kb_ids = sorted(ctx.readable_kb_ids)[:20]
    try:
        await write_audit(
            session,
            action=ACTION_MCP_CALL,
            user_id=ctx.user_id,
            api_key_id=ctx.api_key_id,
            kb_ids=kb_ids,
            detail={
                "tool": name,
                # ⚠️ query 截断：审计表里存整段 prompt 会让表迅速膨胀
                "query": str(args.get("query") or args.get("question") or "")[:200],
                "ms": int((time.perf_counter() - t0) * 1000),
                "is_error": bool(result.get("isError")),
            },
        )
    except Exception as exc:  # noqa: BLE001 — 审计失败不能毁掉工具结果
        logger.warning("MCP 审计写入失败（tool=%s）：%s", name, exc)

    return ok(req, result)


@router.get("/mcp", summary="MCP 能力自述（便于人工排查）")
async def mcp_info(ctx: CurrentUser) -> dict:
    """返回本服务器实现的方法与工具（人看的，不是协议面）。

    ⚠️ **不是协议的一部分**：客户端不会调它。存在的理由是"服务出问题时
    能用 curl 确认到底实现了哪些方法"，而不用去翻日志。
    """
    return {
        "protocol_versions": list(SUPPORTED_PROTOCOL_VERSIONS),
        "methods": [
            "initialize", "notifications/initialized", "ping",
            "tools/list", "tools/call",
        ],
        "transport": "streamable-http (stateless)",
        "auth": "Authorization: Bearer <api_key>",
        "tools": [t["name"] for t in tools_list(ctx)],
        "readable_kb_ids": sorted(ctx.readable_kb_ids),
    }