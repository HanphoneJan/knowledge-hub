"""MCP 的四个工具（D9）—— 与 HTTP 路由**共用同一套内核**（D10）。

| 工具 | 参数 | 内核 |
|---|---|---|
| `list_kb` | — | `kb` 路由的可见库查询 |
| `search_kb` | `kb_id`, `query`, `top_k`, `mode` | `retrieval.engine.search` |
| `ask_kb` | `kb_id`, `question`, `top_k`, `session_id` | `retrieval.ask.prepare` + `stream_answer(one_shot=True)` |
| `get_document` | `kb_id`, `doc_id`, `limit` | `documents` 路由的详情 + chunks |

## 🔑 权限裁剪在**两个**地方做（契约 §10.2）

1. `tools/list` 的 `kb_id` 参数 schema 用 **enum 列出有权限的库** ——
   "先返回全部工具、调用时再拒绝"等于让调用方探测到不该存在的 KB。
2. `tools/call` 时仍然走 `assert_kb_access` ——
   schema 的 enum 是**给调用方看的提示**，不是访问控制。
   只靠第 1 条的话，改一次客户端的入参就能越权。

## 为什么 `ask_kb` 复用 SSE 而不是另写一条非流式路径

`ask.stream_answer(one_shot=True)` 就是为 MCP/A2A 准备的：所有增量合并成
**一个** `delta` 帧，其余帧（`sources` / `done`）照常。
少一条响应格式 → 三方（MCP / A2A / 浏览器 SSE）共用一条代码路径
（CONVENTIONS §0.3）。这里把它解析回结构化 JSON 给调用方。

## 工具结果一律是 JSON 字符串塞进 `content[0].text`

见 `protocol.json_content` 的说明：MCP 客户端**必须**支持 `text` 内容类型，
自造类型会被当"未知类型"丢掉 —— 表现为"调用成功但没有内容"。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from ..common.auth import UserContext, assert_kb_access
from ..common.errors import InvalidRequest, PkhError, ResourceNotFound
from ..common.schemas import SEARCH_TEXT_CHARS
from .protocol import ProtocolError, json_content

logger = logging.getLogger(__name__)

#: `top_k` 的上限。⚠️ 必须有：外部 AI 会传 `top_k=10000`，
#: 而 `MAX_CANDIDATES=100` 已经是单腿候选的上限（再大只是白跑）。
MAX_TOP_K = 50
DEFAULT_TOP_K = 10


@dataclass(frozen=True)
class Tool:
    """一个工具的声明（`tools/list` 直接用它）。"""

    name: str
    description: str
    #: 参数 schema 的**静态部分**；`kb_id` 的 enum 在裁剪时注入
    schema: dict[str, Any]


#: 🔑 JSON-RPC 的"参数不合法"错误码（JSON-RPC 2.0 §5.1.2 / MCP 规范）。
#:   之前缺参数会一路抛成 `KeyError` → 顶层 `except Exception` → **-32603 内错**，
#:   而且 message 是 `KeyError: 'kb_id'` 这种**裸 Python 异常**：
#:   调用方（多半是个 LLM）既不知道该改什么，也拿到了内部实现细节。
INVALID_PARAMS = -32602


def validate_args(tool: Tool, args: Any) -> list[str]:
    """按工具自己的 schema 校验参数，返回**人类可读的问题列表**。

    🔑 **只用 schema 里已经声明的东西**（`required` / `type` / `enum` /
    `minimum` / `maximum` / `additionalProperties`），**不引入 jsonschema 依赖**
    —— 那 5 条覆盖了外部客户端真正会犯的错，而多一个依赖就多一份
    "版本升级后行为变了"的运维风险（项目里 httpx 传输层已经手写过一次，
    同样的理由，`MEMORY.md` 也记着"传输层手写"这条决策）。

    ⚠️ 为什么**参数错**是协议错、而**业务失败**是 `isError: true`：
    前者是"请求本身不合法"（JSON-RPC 层面），后者是"请求合法但结果失败"
    （库不存在 / 无权限）。混在一起的话，模型会把"我少传一个参数"
    误读成"这个库不存在"，于是换一个不存在的 kb_id 反复试（`tools.py` 的注释里写过）。

    @return 问题列表；**空列表表示通过**（不抛，调用方决定报不报）
    """
    if not isinstance(args, dict):
        return [f"arguments 必须是对象，收到 {type(args).__name__}"]
    props: dict[str, Any] = tool.schema.get("properties") or {}
    problems: list[str] = []

    for key in tool.schema.get("required") or ():
        if args.get(key) is None:
            problems.append(f"缺少必填参数 {key}")

    if tool.schema.get("additionalProperties") is False:
        for key in args:
            if key not in props:
                problems.append(
                    f"未知参数 {key}；可用：{sorted(props)}"
                )

    for key, raw in args.items():
        spec = props.get(key)
        if not isinstance(spec, dict):
            continue
        want = spec.get("type")
        if want == "integer":
            # ⚠️ `bool` 是 `int` 的子类：Python 侧要显式排除，
            #   否则 `kb_id=True` 会被当成 kb_id=1 静默通过。
            if isinstance(raw, bool) or not isinstance(raw, (int, str)):
                problems.append(f"{key} 必须是整数，收到 {raw!r}")
                continue
            try:
                num = int(raw)
            except ValueError:
                problems.append(f"{key} 必须是整数，收到 {raw!r}")
                continue
            lo, hi = spec.get("minimum"), spec.get("maximum")
            if isinstance(lo, int) and num < lo:
                problems.append(f"{key} 不能小于 {lo}（收到 {num}）")
            if isinstance(hi, int) and num > hi:
                # 🔑 越界**报错而不是静默夹取**：schema 已经告诉调用方上限是多少，
                #   悄悄改成 50 会让"我要 80 条"变成"我拿到 50 条"而无人察觉。
                #   （HTTP 侧 `/search` 的 `top_k` 仍是夹取，因为那里是给人调用的。）
                problems.append(f"{key} 不能大于 {hi}（收到 {num}）")
        elif want == "string" and not isinstance(raw, str):
            problems.append(f"{key} 必须是字符串，收到 {type(raw).__name__}")
        enum = spec.get("enum")
        if enum and raw not in enum:
            problems.append(f"{key} 只能是 {enum} 之一，收到 {raw!r}")
    return problems


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="list_kb",
        description=(
            "列出我有权限访问的知识库（KB）。"
            "search_kb / ask_kb / get_document 的 kb_id 只能取这里返回的值。"
        ),
        schema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    Tool(
        name="search_kb",
        description=(
            "在指定知识库里做多路检索（向量 + BM25 + 子串），"
            "返回带正文与引用坐标的命中列表。适合'在资料里找某段话'。"
        ),
        schema={
            "type": "object",
            "properties": {
                "kb_id": {"type": "integer", "description": "知识库 id"},
                "query": {"type": "string", "description": "检索词或问题"},
                "top_k": {"type": "integer", "minimum": 1,
                          "maximum": MAX_TOP_K, "default": DEFAULT_TOP_K},
                "mode": {"type": "string", "enum": ["hybrid", "semantic", "exact"],
                         "default": "hybrid"},
            },
            "required": ["kb_id", "query"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="ask_kb",
        description=(
            "在指定知识库里做 RAG 问答：检索 + 生成带 [n] 引用角的答案。"
            "适合'资料里怎么写的'这种需要综合与引用的提问。"
        ),
        schema={
            "type": "object",
            "properties": {
                "kb_id": {"type": "integer", "description": "知识库 id"},
                "question": {"type": "string", "description": "问题"},
                "top_k": {"type": "integer", "minimum": 1,
                          "maximum": MAX_TOP_K, "default": DEFAULT_TOP_K},
                "session_id": {
                    "type": "integer",
                    "description": "继续多轮追问时传上一次的会话 id（D7）",
                },
            },
            "required": ["kb_id", "question"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="get_document",
        description=(
            "取一份文档的元信息（文件名、大小、状态）与前 N 个 chunk 的正文。"
            "适合'这份文档具体写了什么'。"
        ),
        schema={
            "type": "object",
            "properties": {
                "kb_id": {"type": "integer", "description": "知识库 id"},
                "doc_id": {"type": "integer", "description": "文档 id"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50,
                          "default": 5},
            },
            "required": ["kb_id", "doc_id"],
            "additionalProperties": False,
        },
    ),
)

BY_NAME = {t.name: t for t in TOOLS}


def tools_list(ctx: UserContext) -> list[dict[str, Any]]:
    """`tools/list` 的结果（**按权限裁剪**，契约 §10.2）。"""
    kb_ids = sorted(ctx.readable_kb_ids)
    out: list[dict[str, Any]] = []
    for t in TOOLS:
        schema = json.loads(json.dumps(t.schema))     # 深拷贝，别改到常量
        props = schema.get("properties") or {}
        if "kb_id" in props:
            if not kb_ids:
                # ⚠️ 一个库都没有 → **不暴露这三个工具**：
                #   留着 `kb_id` 的 enum 是空数组的话，调用方只能得到
                #   "每次调用都失败"，那不如告诉它"你现在没有可用的库"
                #   （`list_kb` 仍然返回，列表为空）
                continue
            props["kb_id"]["enum"] = kb_ids
            props["kb_id"]["description"] = (
                f"知识库 id；当前可用：{kb_ids}"
            )
        out.append(
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": schema,
            }
        )
    return out


# ============================================================
# 工具实现
# ============================================================


async def call_tool(
    name: str, args: dict[str, Any], *, session, ctx: UserContext
) -> dict[str, Any]:
    """调一个工具。**业务失败返回 `isError=true`**，不抛（见 protocol 说明）。"""
    tool = BY_NAME.get(name)
    fn = _IMPL.get(name)
    if fn is None or tool is None:
        raise ProtocolError(
            -32601, f"未知工具 {name!r}；可用：{sorted(_IMPL)}"
        )
    problems = validate_args(tool, args)
    if problems:
        # 🔴 D144：参数不合法是**协议错**（-32602），不是"库不存在"那种业务错。
        #   之前这里不校验，缺参数会一路抛成 `KeyError` → 顶层 except → -32603，
        #   message 还是裸的 `KeyError: 'kb_id'`：调用方既不知道要改什么，
        #   也拿到了内部实现细节。
        raise ProtocolError(INVALID_PARAMS, f"{name}: " + "；".join(problems))
    try:
        return await fn(args, session=session, ctx=ctx)
    except PkhError as exc:
        # ⚠️ 库不存在 / 无权限 → **业务错误**（isError），不是协议错误：
        #   模型看到的是"这个库不存在"，而不是一个看不懂的 -32602。
        return json_content(
            {"error": exc.code, "message": exc.message}, is_error=True
        )
    except InvalidRequest as exc:
        return json_content(
            {"error": exc.code, "message": exc.message}, is_error=True
        )


async def _t_list_kb(args, *, session, ctx) -> dict[str, Any]:
    from sqlalchemy import select

    from ..common.models import KnowledgeBase

    rows = (
        await session.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.id.in_(sorted(ctx.readable_kb_ids)),
                KnowledgeBase.deleted_at.is_(None),
            ).order_by(KnowledgeBase.id)
        )
    ).scalars().all()
    return json_content({
        "kbs": [
            {
                "id": k.id, "name": k.name, "source_kind": k.source_kind,
                "visibility": k.visibility,
                "description": k.description,
            }
            for k in rows
        ],
        "count": len(rows),
    })


async def _t_search_kb(args, *, session, ctx) -> dict[str, Any]:
    from ..retrieval import engine

    kb_id = int(args["kb_id"])
    await assert_kb_access(session, ctx, kb_id)
    top_k = _clamp_top_k(args.get("top_k"))
    outcome = await engine.search(
        session, ctx, str(args["query"]),
        kb_id=kb_id, mode=args.get("mode") or "hybrid", top_k=top_k,
        rerank_model=await _kb_rerank(session, kb_id),
    )
    return json_content({
        "query": outcome.query,
        "total": outcome.total,
        "elapsed_ms": outcome.total_ms,
        # ⚠️ 路由/精排的结论一起给：外部 AI 看到"为什么这么排"才能
        #   在下一次提问里改策略（这也是 §12 debug 端点的用途）
        "route": (
            None if outcome.route is None
            else {"intent": outcome.route.intent, "why": outcome.route.why}
        ),
        "rerank": (
            None if outcome.rerank is None
            else {"applied": outcome.rerank.applied,
                  "skipped": outcome.rerank.skipped}
        ),
        "items": [
            {
                "doc_id": h.doc_id, "file_name": h.payload.get("file_name"),
                "chunk_index": h.chunk_index,
                "kind": engine.citation_kind(h.kind, visual="visual" in h.legs),
                "text": h.text[:SEARCH_TEXT_CHARS],
                "score": round(h.score, 6),
                "locations": h.locations,
                "thumb_url": h.payload.get("thumb_url"),
            }
            for h in outcome.items
        ],
    })


async def _t_ask_kb(args, *, session, ctx) -> dict[str, Any]:
    """问答：复用 SSE 内核，`one_shot=True`（见模块 docstring）。"""
    from ..retrieval.ask import prepare, stream_answer

    kb_id = int(args["kb_id"])
    await assert_kb_access(session, ctx, kb_id)
    sid = args.get("session_id")
    plan = await prepare(
        session, ctx, str(args["question"]), kb_id=kb_id,
        session_id=int(sid) if isinstance(sid, (int, str)) and sid else None,
        top_k=_clamp_top_k(args.get("top_k")),
    )

    sources: list[dict[str, Any]] = []
    answer_parts: list[str] = []
    done: dict[str, Any] = {}
    async for raw in stream_answer(plan, ctx=ctx, one_shot=True):
        event, data = _parse_sse(raw)
        if event == "sources":
            sources = data.get("sources") or []
        elif event == "delta":
            answer_parts.append(data.get("text") or "")
        elif event == "done":
            done = data
    return json_content({
        "answer": "".join(answer_parts),
        "citations": done.get("citations") or [],
        # ⚠️ 悬空角标要回传：静默丢掉的话模型会以为 [3] 有来源，
        #   用户点过去发现是空的（这是 D72 的同一条）
        "dangling_citations": done.get("dangling_citations") or [],
        "session_id": done.get("session_id"),
        "retrieval_ms": done.get("retrieval_ms"),
        "elapsed_ms": done.get("elapsed_ms"),
        "generator": done.get("generator"),
        "sources": sources,
    })


async def _t_get_document(args, *, session, ctx) -> dict[str, Any]:
    from sqlalchemy import select

    from ..common.models import Chunk, Document

    kb_id = int(args["kb_id"])
    await assert_kb_access(session, ctx, kb_id)
    doc_id = int(args["doc_id"])
    doc = (
        await session.execute(
            select(Document).where(
                Document.id == doc_id,
                Document.kb_id == kb_id,
                Document.is_current.is_(True),
                Document.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if doc is None:
        # ⚠️ 与"库不存在"同一种表达（不区分是哪个不存在）：
        #   区分开就等于告诉调用方"这个 doc_id 在别的库里存在"
        return json_content(
            {"error": "DOC_NOT_FOUND", "message": f"文档 {doc_id} 不存在"},
            is_error=True,
        )

    limit = max(1, min(int(args.get("limit") or 5), 50))
    chunks = (
        await session.execute(
            select(Chunk).where(Chunk.doc_id == doc_id)
            .order_by(Chunk.chunk_index).limit(limit)
        )
    ).scalars().all()
    return json_content({
        "doc_id": doc.id, "kb_id": kb_id, "file_name": doc.file_name,
        "mime_type": doc.mime_type, "size_bytes": doc.size_bytes,
        "status": doc.status, "status_msg": doc.status_msg,
        "chunk_count": doc.chunk_count, "page_count": doc.page_count,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
        "chunks": [
            {
                "chunk_index": c.chunk_index, "kind": c.kind,
                "content_norm": c.content_norm[:SEARCH_TEXT_CHARS],
                "locations": c.locations,
            }
            for c in chunks
        ],
        "truncated": doc.chunk_count > len(chunks),
    })


_IMPL = {
    "list_kb": _t_list_kb,
    "search_kb": _t_search_kb,
    "ask_kb": _t_ask_kb,
    "get_document": _t_get_document,
}


# ============================================================
# 辅助
# ============================================================


def _clamp_top_k(v) -> int:
    """`top_k` 收敛到 `[1, MAX_TOP_K]`。

    ⚠️ **不报错而是收敛**：外部 AI 会传 1000，而超限报错会让整个对话失败。
    收敛 + 警告文案对模型更友好（它下一轮就会自己调小）。
    """
    try:
        n = int(v) if v is not None else DEFAULT_TOP_K
    except (TypeError, ValueError):
        n = DEFAULT_TOP_K
    return max(1, min(n, MAX_TOP_K))


async def _kb_rerank(session, kb_id: int) -> str | None:
    """该库配的 Reranker（与 HTTP 路径同源，见 `documents._kb_rerank`）。"""
    from sqlalchemy import select

    from ..common.models import KnowledgeBase

    row = (
        await session.execute(
            select(KnowledgeBase.rerank_model).where(KnowledgeBase.id == kb_id)
        )
    ).scalar_one_or_none()
    return row or None


def _parse_sse(raw: str) -> tuple[str, dict[str, Any]]:
    """`ask._sse` 产出的单帧 → `(event, data)`。"""
    event = ""
    data: dict[str, Any] = {}
    for line in raw.splitlines():
        if line.startswith("event: "):
            event = line[7:].strip()
        elif line.startswith("data: "):
            try:
                data = json.loads(line[6:])
            except json.JSONDecodeError:
                data = {}
    return event, data


__all__ = ["BY_NAME", "MAX_TOP_K", "TOOLS", "Tool", "call_tool", "tools_list"]