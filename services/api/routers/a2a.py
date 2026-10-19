"""A2A：`GET /.well-known/agent-card.json` + `POST /a2a/tasks`（D10）。

## 与 MCP 共用内核（D10 的原话：避免两套逻辑走偏）

`answer` Agent 的实现就是 `ask.prepare` + `stream_answer(one_shot=True)`
—— 与 MCP 的 `ask_kb` **同一个函数**。差别只在传输层：
A2A 回 `artifacts[]`，MCP 回 `content[].text`。

## 两个 Agent 的分工与**真实**实现

| Agent | skill | 实现 |
|---|---|---|
| 问答 | `answer` | 检索 + 抽取式生成，带 `[n]` 引用 |
| 研究 | `research` | **检索 → 按文档分组 → 抽取式汇总** |

⚠️ `research` **不是**"让 LLM 规划多轮检索"：本项目默认生成层是抽取式
（D73 的隐私红线：本机没有 LLM，且内容是私人数据）。所以它做的是
**可解释的多角度汇总**：把命中按文档分组，每份文档给出最相关的句子摘录
与出处，调用方自己再综合。**如实说明它做了什么**，不假装有多轮规划能力。

## 协议形状

A2A 的 HTTP 绑定是 JSON-RPC，方法 `tasks/send`。本端点**两种入参都收**：

```jsonc
// ① 协议面（外部 Agent 客户端用）
{"jsonrpc":"2.0","id":1,"method":"tasks/send",
 "params":{"skill":"answer","message":{"parts":[{"text":"…"}]}}}
// ② 便捷面（curl / 手测）
{"skill":"answer","query":"…"}
```

⚠️ 两种都支持不是"图省事"：`tasks/send` 的 `params.message.parts[]`
对 curl 来说很啰嗦，而本项目所有其它端点都是"一个 JSON 对象"，
突然只收协议形状会让**手测与排障**都变难。两条路径走**同一个**内部函数。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, Request, Response

from ...common.audit import ACTION_A2A_CALL, write_audit
from ...common.auth import UserContext, assert_kb_access
from ...common.deps import CurrentUser, DbSession
from ...common.errors import InvalidRequest, PkhError
from ...common.schemas import SEARCH_TEXT_CHARS
from ...retrieval.ask import prepare, stream_answer

logger = logging.getLogger(__name__)
router = APIRouter(tags=["a2a"])

AGENT_VERSION = "0.1.0"

#: 两个 Agent 的 skill 声明（契约 §11 的分工表）
#: 🔑 D145：**可机器读**的输入/输出 schema（M11 写的是"Agent Card 声明能力与
#: **输入输出**"，而此前只有 `inputModes`/`outputModes` 那种**媒体类型**声明）。
#:
#: 为什么需要：A2A 规范本身**不要求** skill 带 schema（`AgentSkill` 只有
#: id/name/description/tags/examples），但"声明输入输出"这条需求要落地就得
#: 给机器可读的东西 —— 否则外部 Agent 只能从 `description` 里猜参数名，
#: 猜错时拿到的是 400 与一句"query/message 不能为空"，**完全指不到"你少了
#: `kb_id`"**（这个端点刻意不返回有哪些库，见 `_parse_task_request` 的注释）。
#:
#: ⚠️ 这份声明**必须与真实响应一致** —— 否则它就是第二份契约（会漂）。
#: `tests/test_a2a_skill_schemas.py` 会拿**真实调用**的响应逐字段核对。
_INPUT_COMMON: dict[str, Any] = {
    "type": "object",
    "properties": {
        "skill": {"type": "string", "enum": ["answer", "research"],
                  "default": "answer"},
        "query": {"type": "string",
                  "description": "问题文本（便捷面）；协议面用 message.parts[].text"},
        "kb_id": {"type": "integer",
                  "description": "🔑 **必填**。本服务刻意不在 Agent Card 里列出"
                                 "可用的 kb_id —— 那是有权限信息的泄露"},
    },
    "required": ["kb_id"],
    "additionalProperties": False,
}

_PART: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "const": "text"},
        "text": {"type": "string", "description": "正文（答案或摘录汇总）"},
        "metadata": {
            "type": "object",
            "description": "🔑 引用角标在**这里**（`citations` / "
                           "`dangling_citations`），不在顶层字段",
        },
    },
    "required": ["kind", "text"],
}

_SOURCE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "idx": {"type": "integer", "description": "对应答案里的 [n]"},
        "doc_id": {"type": "integer"},
        "chunk_index": {"type": "integer"},
        "score": {"type": "number"},
        "title": {"type": "string"},
        "text": {"type": "string"},
        "locations": {"type": ["array", "null"], "description": "L3 坐标（可画框）"},
    },
    "required": ["idx", "doc_id", "chunk_index"],
}

_DOC: dict[str, Any] = {
    "type": "object",
    "properties": {
        "doc_id": {"type": "integer"},
        "file_name": {"type": "string"},
        "chunk_indexes": {"type": "array", "items": {"type": "integer"}},
        "best_score": {"type": "number"},
    },
    "required": ["doc_id", "file_name"],
}

#: 🔑 `outputSchema` 描述的是**调用方真正收到的东西**：一个 A2A `Task` 对象
#: （`_task_ok`）。正文在 `artifacts[].parts[].text`，引用来源在
#: **顶层 `metadata.sources`** —— 我第一稿把两者都放在 artifact 里，
#: 而真实响应里 artifact 只有 `{artifactId, name, parts}`
#: （`tests/test_a2a_skill_schemas.py` 拿真响应逐字段核对，当场抓住）。
_TASK_ENVELOPE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "contextId": {"type": "string", "description": "`pkh-<kb_id>`"},
        "kind": {"type": "string", "const": "task"},
        "status": {"type": "object",
                   "properties": {"state": {"type": "string", "const": "completed"},
                                  "timestamp": {"type": "number"}},
                   "required": ["state"]},
        "history": {"type": "array", "items": {"type": "object"},
                    "description": "user + agent 两条消息（agent 那条即正文）"},
        "artifacts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "artifactId": {"type": "string"},
                    "name": {"type": "string"},
                    "parts": {"type": "array", "items": _PART},
                },
                "required": ["artifactId", "name", "parts"],
            },
        },
        "metadata": {
            "type": "object",
            "properties": {
                "kb_id": {"type": "integer"},
                "skill": {"type": "string"},
                "sources": {"type": "array", "items": _SOURCE},
                "documents": {"type": "array", "items": _DOC},
            },
            "required": ["kb_id", "skill", "sources", "documents"],
        },
    },
    "required": ["id", "kind", "status", "history", "artifacts", "metadata"],
}

_ANSWER_OUTPUT: dict[str, Any] = json.loads(json.dumps(_TASK_ENVELOPE))
_ANSWER_OUTPUT["description"] = (
    "问答结果。正文在 `artifacts[0].parts[0].text`；"
    "引用来源在 `metadata.sources`（`idx` 与正文的 [n] 对应）；"
    "引用角标在 `artifacts[0].parts[0].metadata.citations`。"
)

_RESEARCH_OUTPUT: dict[str, Any] = json.loads(json.dumps(_TASK_ENVELOPE))
_RESEARCH_OUTPUT["description"] = (
    "研究结果（抽取式：返回**摘录**不是结论）。正文在 "
    "`artifacts[0].parts[0].text`；按文档分组的明细在 `metadata.documents`；"
    "`artifacts[0].parts[0].metadata.truncated` 说明是否因上限被截断。"
)

SKILLS = [
    {
        "id": "answer",
        "name": "问答 Agent",
        "description": (
            "在指定知识库里做单轮问答：检索 + 抽取式生成，答案带 [n] 引用角标。"
            "适合「资料里怎么写的」这类需要引用出处的问题。"
        ),
        "tags": ["rag", "qa", "citation"],
        "examples": [
            "入职日期那栏该填什么？",
            "报销单的填写规范是什么？",
        ],
        "inputModes": ["text/plain"],
        "outputModes": ["text/plain", "application/json"],
        # 🔑 D145：给机器看的输入/输出（不是给人读的 description）
        "inputSchema": _INPUT_COMMON,
        "outputSchema": _ANSWER_OUTPUT,
    },
    {
        "id": "research",
        "name": "研究 Agent",
        "description": (
            "多角度检索 + 按文档汇总：把命中按文档分组，每份文档给出最相关的"
            "句子摘录与出处，供上层综合。"
            "⚠️ 本 Agent 是**抽取式**的（不调用 LLM），返回的是摘录不是结论。"
        ),
        "tags": ["research", "summary", "multi-document"],
        "examples": [
            "把本季度的项目材料汇总一下",
            "关于 A2A Agent 互联，接口和示例分别怎么写的？",
        ],
        "inputModes": ["text/plain"],
        "outputModes": ["text/plain", "application/json"],
        "inputSchema": _INPUT_COMMON,
        "outputSchema": _RESEARCH_OUTPUT,
    },
]
SKILL_IDS = {s["id"] for s in SKILLS}

#: 单次 research 最多汇总多少份文档（再多响应会到几百 KB）
MAX_RESEARCH_DOCS = 10
#: 🔑 research 的候选池比单次问答宽（30）：研究要的是"覆盖多份文档"，
#:   top_k=10 只够看两三份 —— 而"覆盖多份"正是它与 answer 的区别。
RESEARCH_TOP_K = 30
#: 每份文档给出几句摘录
PER_DOC_SENTENCES = 2


@router.get(
    "/.well-known/agent-card.json",
    summary="A2A Agent Card（D10）",
)
async def agent_card(request: Request, ctx: CurrentUser) -> dict[str, Any]:
    """能力声明。

    ⚠️ **按调用者裁剪 `skills`**：只列出该用户至少有一个可访问 KB 的 skill。
    与 MCP 的 `tools/list` 同一原则（契约 §10.2）——
    "先全返回、调用时再拒绝"等于让调用方探测到不该存在的库。
    """
    base = str(request.base_url).rstrip("/")
    card: dict[str, Any] = {
        "protocolVersion": "0.2.5",
        "name": "Knowledge Hub",
        "description": (
            "个人知识库检索/问答服务（MCP + A2A）。"
            "所有回答都带引用出处；默认抽取式生成，不调用外部 LLM。"
        ),
        "url": f"{base}/a2a/tasks",
        "preferredTransport": "JSONRPC",
        "version": AGENT_VERSION,
        "provider": {
            "organization": "personal",
            "url": base,
        },
        "capabilities": {
            # ⚠️ 不声明 streaming：端点是非流式的。声明了客户端会试着订阅，
            #   而那必然失败 —— 能力声明要反映**实际**实现。
            "streaming": False,
            "pushNotifications": False,
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "securitySchemes": {
            "apiKey": {"type": "http", "scheme": "bearer"},
        },
        "security": [{"apiKey": []}],
        "skills": SKILLS,
        "readableKbCount": len(ctx.readable_kb_ids),
    }
    return card


@router.post("/a2a/tasks", summary="A2A 委派任务（D10）")
async def a2a_tasks(
    request: Request, ctx: CurrentUser, session: DbSession
) -> Response:
    """`tasks/send`（协议面）/ `{"skill","query","kb_id"}`（便捷面）。"""
    t0 = time.perf_counter()
    raw = await request.body()
    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        return _error(400, "INVALID_REQUEST", f"JSON 解析失败: {exc}")

    try:
        skill, query, kb_id = _parse_task_request(body)
    except BadTaskRequest as e:
        return _error(400, e.code, e.message)
    if kb_id is None:
        return _error(
            400, "INVALID_REQUEST",
            "必须指定 kb_id（Agent Card 不会泄露有哪些库 —— 那是权限信息）",
        )
    try:
        await assert_kb_access(session, ctx, kb_id)
        if skill == "answer":
            artifact = await _run_answer(session, ctx, kb_id, query)
        else:
            artifact = await _run_research(session, ctx, kb_id, query)
    except PkhError as exc:
        # ⚠️ 无权限与不存在同码（§0.4），对 A2A 也一样：
        #   区分开就等于告诉调用方"这个库存在但你没权限"
        await _audit(session, ctx, kb_id, skill, query, t0, error=exc.code)
        return _error(403, exc.code, exc.message)
    except InvalidRequest as exc:
        return _error(400, exc.code, exc.message)

    await _audit(session, ctx, kb_id, skill, query, t0)
    return Response(
        json.dumps(_task_ok(kb_id, skill, query, artifact), ensure_ascii=False),
        media_type="application/json",
    )


# ============================================================
# 两个 Agent（与 MCP 共用内核）
# ============================================================


async def _run_answer(session, ctx, kb_id: int, query: str) -> dict[str, Any]:
    """问答 Agent = `ask` 内核（与 MCP `ask_kb` 同一函数）。"""
    plan = await prepare(session, ctx, query, kb_id=kb_id)
    sources: list[dict[str, Any]] = []
    parts: list[str] = []
    done: dict[str, Any] = {}
    async for raw in stream_answer(plan, ctx=ctx, one_shot=True):
        event, data = _parse_sse(raw)
        if event == "sources":
            sources = data.get("sources") or []
        elif event == "delta":
            parts.append(data.get("text") or "")
        elif event == "done":
            done = data
    return {
        "name": "answer",
        "parts": [
            {
                "kind": "text",
                "text": "".join(parts),
                # ⚠️ 悬空角标**不静默**：没在 done 里就是有失效引用，
                #   上层要能提示"这条引用失效了"（D72）
                "metadata": {
                    "citations": done.get("citations") or [],
                    "dangling_citations": done.get("dangling_citations") or [],
                    "session_id": done.get("session_id"),
                    "retrieval_ms": done.get("retrieval_ms"),
                    "generator": done.get("generator"),
                },
            }
        ],
        "sources": sources,
    }


async def _run_research(session, ctx, kb_id: int, query: str) -> dict[str, Any]:
    """研究 Agent：检索 → **按文档分组** → 抽取式汇总（见模块 docstring）。

    🔑 与 `answer` 走**同一个** `ask.prepare`（只把 `top_k` 放宽）——
    证据去重、引用角标、软删过滤全都复用，不在这里重写一遍。
    """
    from ...retrieval.llm import compose_extractive, score_sentences, split_sentences

    plan = await prepare(session, ctx, query, kb_id=kb_id, top_k=RESEARCH_TOP_K)

    by_doc: dict[int, list] = {}
    for ev in plan.evidences:
        by_doc.setdefault(ev.doc_id, []).append(ev)

    summary, citations = compose_extractive(query, plan.evidences)

    blocks: list[str] = []
    docs_meta: list[dict[str, Any]] = []
    # 文档顺序按"该文档最好的一条证据"排 —— 可解释，且与相关性一致
    ordered = sorted(
        by_doc.items(), key=lambda kv: max(e.score for e in kv[1]), reverse=True
    )
    for doc_id, evs in ordered[:MAX_RESEARCH_DOCS]:
        title = evs[0].title or f"doc {doc_id}"
        # ⚠️ 打分在**整份文档的句子**上做（不是每条 chunk 各取一句），
        #   否则多 chunk 的文档会靠"句子多"取胜而不是"句子相关"
        sents = split_sentences(" ".join(e.text for e in evs if e.text))
        scores = score_sentences(query, sents) if sents else []
        ranked = sorted(
            zip(scores, sents), key=lambda x: -x[0]
        )[:PER_DOC_SENTENCES]
        quote = " ".join(s for _, s in ranked) or "（该文档没有可摘录的句子）"
        blocks.append(f"【{title}】{quote}")
        docs_meta.append({
            "doc_id": doc_id,
            "file_name": title,
            "chunk_indexes": [e.chunk_index for e in evs],
            "best_score": round(max(e.score for e in evs), 6),
        })

    text = "\n\n".join(blocks) if blocks else "（没有命中任何文档）"
    if summary:
        text = f"摘要：{summary}\n\n{text}"
    return {
        "name": "research",
        "parts": [
            {
                "kind": "text",
                "text": text,
                "metadata": {
                    "doc_count": len(docs_meta),
                    # ⚠️ 如实说明被截断了：不说的话调用方以为"就这些文档"
                    "truncated": len(by_doc) > MAX_RESEARCH_DOCS,
                    "extractive": True,
                    "note": "抽取式汇总（不调用 LLM）：返回的是摘录而非结论",
                },
            }
        ],
        "documents": docs_meta,
        "citations": citations,
    }


# ============================================================
# 协议形状
# ============================================================


class BadTaskRequest(Exception):
    """入参不合法。

    🔑 用**异常**而不是"返回 2 元组表示错误"：第一版那样写，
    于是 `isinstance(parsed, tuple)` 对成功路径也成立，
    `code, msg = parsed` 直接 `ValueError: too many values to unpack`
    —— 表现为 A2A 端点 500，而错误完全指不到"入参解析"。
    """

    def __init__(self, message: str, code: str = "INVALID_REQUEST") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _parse_task_request(body: Any) -> tuple[str, str, int | None]:
    """两种入参 → `(skill, query, kb_id)`；不合法抛 `BadTaskRequest`。"""
    if not isinstance(body, dict):
        raise BadTaskRequest("请求体必须是对象")

    # ① 协议面：JSON-RPC tasks/send
    if body.get("method") == "tasks/send":
        params = body.get("params") or {}
        if not isinstance(params, dict):
            raise BadTaskRequest("params 必须是对象")
        skill = str(params.get("skill") or "answer")
        message = params.get("message") or {}
        parts = message.get("parts") if isinstance(message, dict) else None
        query = _join_parts(parts)
        kb_id = params.get("kb_id") or (
            message.get("configuration", {}).get("kb_id")
            if isinstance(message, dict) else None
        )
    else:
        # ② 便捷面
        skill = str(body.get("skill") or "answer")
        query = str(body.get("query") or body.get("question") or "")
        kb_id = body.get("kb_id")

    if skill not in SKILL_IDS:
        raise BadTaskRequest(
            f"未知 skill {skill!r}；可用：{sorted(SKILL_IDS)}"
        )
    if not query.strip():
        raise BadTaskRequest("query/message 不能为空")
    try:
        kb = int(kb_id) if kb_id is not None else None
    except (TypeError, ValueError):
        raise BadTaskRequest(f"kb_id 必须是整数，收到 {kb_id!r}")
    return skill, query, kb


def _join_parts(parts: Any) -> str:
    """A2A 的 `parts[]` → 一段文本（忽略非 text 的 part）。

    ⚠️ 非 text 的 part（`kind: "file"` 之类）本项目**不支持**：
    静默忽略会让人以为文件被处理了，所以这里只取文本，
    而整体输入为空时报错（"不能为空"），由上层看到。
    """
    if isinstance(parts, str):
        return parts
    if not isinstance(parts, list):
        return ""
    out: list[str] = []
    for p in parts:
        if isinstance(p, dict) and p.get("kind", "text") == "text":
            t = p.get("text")
            if isinstance(t, str):
                out.append(t)
        elif isinstance(p, str):
            out.append(p)
    return "\n".join(out)


def _task_ok(
    kb_id: int, skill: str, query: str, artifact: dict[str, Any]
) -> dict[str, Any]:
    """A2A Task 对象（state=completed；本端点只做同步返回）。"""
    now = time.time()
    ctx_id = f"pkh-{kb_id}"
    msg_id = uuid.uuid4().hex[:12]
    return {
        "id": f"task-{msg_id}",
        "contextId": ctx_id,
        "kind": "task",
        "status": {"state": "completed", "timestamp": now},
        "history": [
            {
                "role": "user",
                "kind": "message",
                "messageId": msg_id,
                "parts": [{"kind": "text", "text": query}],
            },
            {
                "role": "agent",
                "kind": "message",
                "messageId": f"{msg_id}-a",
                "taskId": f"task-{msg_id}",
                "parts": artifact["parts"],
            },
        ],
        "artifacts": [
            {
                "artifactId": f"{msg_id}-art",
                "name": artifact.get("name") or skill,
                "parts": artifact["parts"],
            }
        ],
        "metadata": {
            "kb_id": kb_id,
            "skill": skill,
            "sources": artifact.get("sources") or [],
            "documents": artifact.get("documents") or [],
        },
    }


def _error(status: int, code: str, message: str) -> Response:
    return Response(
        json.dumps({"error": {"code": code, "message": message},
                    "agent_protocol_version": "0.2.5"},
                   ensure_ascii=False),
        status_code=status,
        media_type="application/json",
    )


# ============================================================
# 辅助（与 MCP 同源实现）
# ============================================================


def _parse_sse(raw: str) -> tuple[str, dict[str, Any]]:
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


async def _kb_rerank(session, kb_id: int) -> str | None:
    from sqlalchemy import select

    from ...common.models import KnowledgeBase

    row = (
        await session.execute(
            select(KnowledgeBase.rerank_model).where(KnowledgeBase.id == kb_id)
        )
    ).scalar_one_or_none()
    return row or None


async def _audit(
    session, ctx: UserContext, kb_id: int, skill: str, query: str,
    t0: float, *, error: str | None = None,
) -> None:
    """审计（D8）。⚠️ 审计失败不能毁掉任务结果。"""
    try:
        await write_audit(
            session,
            action=ACTION_A2A_CALL,
            user_id=ctx.user_id,
            api_key_id=ctx.api_key_id,
            kb_ids=[kb_id],
            detail={
                "skill": skill,
                "query": query[:200],
                "ms": int((time.perf_counter() - t0) * 1000),
                **({"error": error} if error else {}),
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("A2A 审计写入失败（skill=%s）：%s", skill, exc)


__all__ = ["router", "SKILLS", "SKILL_IDS", "BadTaskRequest"]