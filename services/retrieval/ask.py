"""RAG 问答编排（`POST /kb/{id}/ask`，SSE）。

流程：检索 → 组织证据 → 流式生成 → 引用校验 → 落库。

## ⚠️ SSE 与依赖生命周期的硬约束

**FastAPI ≥0.106 在 `yield` 依赖上把退出码放在"发送响应之前"执行**
（本机 fastapi 0.142.2 / starlette 1.7.0 正是这个行为）。
所以 `DbSession` 依赖在**响应体开始流式之前**就已经 commit + 关闭了。

因此本模块的铁律：

1. 检索 + 取生成器 —— **全部在开流之前**用请求级 session 完成
2. SSE 生成器里**只允许**做 LLM 的 HTTP 往返，**不许碰任何 DB session**
3. 问答记录（user + assistant 两条消息）的落库在流结束后**新开**
   一个 `session_scope()` —— 两条一起写，中途失败就都不写，
   免得留下"有提问没有回答"的孤儿会话

这不是风格问题：第 2 条违反的话，`session.execute()` 返回的 coroutine
永远不会被 await，消息就静默丢了 —— 和 PROGRESS §5.28 第 3 例
（`enqueue_async` 忘了 await，任务表 0 行、worker 干等）**完全同一种
"不报错、不告警、功能就是不工作"**。

## D46 的性能目标如何落到架构上

首 token < 2 秒，**检索段必须 < 500ms**。所以顺序是：

```
sources 事件（检索完成就发，~200~400ms）
   ↓ 前端此时已经能画出引用卡片
delta*  （生成段，本机抽取式是毫秒级；换成 LLM 是 3~8 tok/s）
   ↓ SSE 逐字掩盖慢速
done     （含 citations 与校验结果）
```

`sources` 先于任何 `delta` 是**契约要求**，前端据此区分"还在检索"与"在生成"。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..common.auth import UserContext
from ..common.config import get_settings
from ..common.db import session_scope
from ..common.models import ChatMessage, ChatSession, KnowledgeBase
from .engine import SearchOutcome, search
from .llm import (
    Evidence,
    ExtractiveGenerator,
    Generator,
    Turn,
    expand_query,
    get_generator,
    load_llm_config,
    parse_citations,
    window_history,
)
from .types import LEG_VISUAL, FusedHit, citation_kind, thumb_url_for

logger = logging.getLogger(__name__)

#: 证据正文最多送进生成的字符数。多了 token 成本上升、中间段还被模型忽略。
MAX_EVIDENCE_CHARS = 400

#: Parent-Child（D165）：命中子块时改用**父块**做上下文，上限比子块宽。
MAX_PARENT_CHARS = 1200


def _evidence_text(hit: FusedHit) -> str:
    """证据正文：Parent-Child 库里优先用**父块上下文**。

    文章 §Parent-Child 的折中：向量索引存小子块（区分相近主题），
    命中后取父段落做生成上下文。`payload["parent_text"]` 就是那个父段落
    （摄取时按章节/页挂上）。没有它的库（默认策略）行为完全不变。
    """
    payload = hit.payload if isinstance(hit.payload, dict) else {}
    parent = payload.get("parent_text")
    if parent:
        return str(parent)[:MAX_PARENT_CHARS]
    return (hit.text or "")[:MAX_EVIDENCE_CHARS]


@dataclass
class AskPlan:
    """开流之前算好的一切。SSE 生成器只读它，不做计算。"""

    question: str
    #: 单库会话的库；多库会话为 `None`（范围见 `kb_ids`）。
    kb_id: int | None
    session_id: int | None
    outcome: SearchOutcome
    evidences: list[Evidence]
    generator: Generator
    #: 🔑 对话历史（D141）：已按窗口裁剪，按时间顺序。
    #: 抽取式生成器**不用**它（`llm.ExtractiveGenerator` 里有理由），
    #: OpenAI 兼容生成器把它当真实的 user/assistant 消息送进模型。
    history: list[Turn] = field(default_factory=list)
    #: 🔑 **实际送去检索的 query**。与 `question` 可能不同 ——
    #: 指代式追问会被并上上一轮问句（`llm.expand_query`）。
    #: 分开存是为了审计能如实记两个（"用户问了什么"与"我们检索了什么"）。
    retrieval_query: str = ""
    #: D163：这次问答的检索范围。`None` 且 `kb_id` 有值 = 单库；
    #: `None` 且 `kb_id` 也为 `None` = 全部有权限的库。
    kb_ids: list[int] | None = None


def to_evidences(hits: list[FusedHit]) -> list[Evidence]:
    """融合结果 → 证据列表。`idx` 从 1 开始，就是答案里的 `[n]` 角标。

    ⚠️ **必须去重**：同一条腿可能命中同一个 chunk（RRF 会把它们合并成一条，
    但如果调用方传了多个 mode 或做了二次融合仍可能重复）。
    重复的证据会让 `[1]` 和 `[2]` 指向同一段文字 —— 角标失去意义。

    🔑 **同文档的相邻片段保持原文顺序**（文章 §上下文排序）：RRF/Rerank 的顺序
    是"相关性"序，同一篇文档的相邻块可能被打散；把它们按 `chunk_index` 升序排，
    模型读到的才是连贯的一段，而不是跳着读。**文档之间的相对顺序不变**
    （按各文档**首次出现**的位置）。

    🔑 **不会出现版本冲突**：检索只返回 `is_current`（`engine._drop_deleted` 挡掉了
    非当前版本），所以"同一主题的多个版本混进上下文"在当前结构下不可能发生 ——
    这条是结构保证，不是靠事后标注。
    """
    uniq: list[FusedHit] = []
    seen: set[tuple[int, int]] = set()
    for h in hits:
        ident = (h.doc_id, h.chunk_index)
        if ident in seen:
            continue
        seen.add(ident)
        uniq.append(h)

    # 按文档分组、组内按 chunk_index 升序；文档顺序取首次出现
    doc_order: list[int] = []
    by_doc: dict[int, list[FusedHit]] = {}
    for h in uniq:
        if h.doc_id not in by_doc:
            by_doc[h.doc_id] = []
            doc_order.append(h.doc_id)
        by_doc[h.doc_id].append(h)
    ordered = [
        h
        for did in doc_order
        for h in sorted(by_doc[did], key=lambda x: x.chunk_index)
    ]

    out: list[Evidence] = []
    for i, h in enumerate(ordered, start=1):
        out.append(
            Evidence(
                idx=i,   # 重排/去重后重排，否则角标会跳号
                doc_id=h.doc_id,
                chunk_index=h.chunk_index,
                title=h.payload.get("file_name") or f"doc {h.doc_id}",
                text=_evidence_text(h),
                score=h.score,
                kind=citation_kind(h.kind, visual=LEG_VISUAL in h.legs),
                locations=h.locations,
                # D36：与 /search 用**同一份**实现（types.thumb_url_for）
                thumb_url=thumb_url_for(h),
                legs=list(h.legs),
                ranks=dict(h.ranks),
            )
        )
    return out


async def _kb_rerank_model(session, kb_id: int) -> str | None:
    """该库配的 Reranker 模型；没配返回 None（= 不启用，行为与今天一致）。"""
    row = (
        await session.execute(
            select(KnowledgeBase.rerank_model).where(KnowledgeBase.id == kb_id)
        )
    ).scalar_one_or_none()
    return row or None

async def prepare(
    session: AsyncSession,
    ctx: UserContext,
    question: str,
    *,
    kb_id: int | None = None,
    kb_ids: list[int] | None = None,
    session_id: int | None = None,
    top_k: int = 8,
    provider: str | None = None,
) -> AskPlan:
    """检索 + 组装证据 + 取生成器。**在开流之前调完。**

    @param kb_id 单库问答。
    @param kb_ids D163 多库问答的检索范围（`None` = 全部有权限的库）。
        给了 `kb_ids` 时按**跨库**检索，并按 D88 **不启用精排**
        （跨库排序取决于"哪些库碰巧配了"）。
    """
    # 阶段 3：Reranker 按库启用。⚠️ 问答这条路径**必须**也接上 ——
    #   用户问"入职日期那栏填什么"正是精排最有价值的场景，
    #   只给 `/search` 开精排等于把好处留给了调试页面。
    settings = get_settings()
    history = await load_history(
        session, session_id,
        max_turns=settings.ask_history_turns,
        max_chars=settings.ask_history_chars,
    )
    # 🔑 指代式追问：把上一轮问句并进 query（与生成器无关，抽取式也受益）
    query = (
        expand_query(question, history, max_chars=settings.ask_anaphora_max_chars)
        if settings.ask_anaphora_expand else question
    )

    if kb_ids is not None:
        # 跨库（含单库的 [id] 形式）：不启用精排（D88）
        outcome = await search(session, ctx, query, kb_ids=kb_ids, top_k=top_k)
    else:
        outcome = await search(
            session, ctx, query, kb_id=kb_id, top_k=top_k,
            rerank_model=(await _kb_rerank_model(session, kb_id)) if kb_id is not None else None,
        )
    return AskPlan(
        question=question,
        kb_id=kb_id,
        kb_ids=kb_ids,
        session_id=session_id,
        outcome=outcome,
        evidences=to_evidences(outcome.items),
        generator=get_generator(
            provider,
            # 🔑 D170：从库里读外部 LLM 配置（页面可配）。显式给了 provider
            # 就用请求级覆盖，不看库配置。
            config=None if provider is not None else await load_llm_config(session),
        ),
        history=history,
        retrieval_query=query,
    )


async def load_history(
    session: AsyncSession, session_id: int | None, *, max_turns: int, max_chars: int
) -> list[Turn]:
    """读这个会话的历史消息并**按窗口裁剪**（D141）。

    🔑 **只读 `session_id` 指定的那一个会话** —— 跨会话读历史等于把
    另一个话题的上下文混进来（而会话归属已由 `ask` 路由的
    `_assert_session` 校验过：必须属于当前用户 + 当前 KB）。

    ⚠️ 取的是**最近**若干条再裁剪，而不是"全取回来再裁" ——
    会话可能有几百轮，全取会把每轮问答的内存占用拖成 O(会话长度)。
    ⚠️ 多取一倍的量：裁剪是**按整轮丢弃**的，取奇数条会砍掉半轮。
    """
    if session_id is None or max_turns <= 0 or max_chars <= 0:
        return []
    rows = await session.execute(
        select(ChatMessage.role, ChatMessage.content)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.id.desc())
        .limit(max_turns * 2)
    )
    turns = [Turn(role=r, content=c) for r, c in rows]
    turns.reverse()                      # 回到时间顺序
    return window_history(turns, max_turns=max_turns, max_chars=max_chars)


# ============================================================
# SSE
# ============================================================


def _sse(event: str, data: dict) -> str:
    """SSE 帧。⚠️ `data` 里不能有裸换行（`text_raw` 是多行文本）——
    JSON 序列化天然把它变成 `\\n`，这就是为什么不能手拼字符串。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _evidence_payload(ev: Evidence) -> dict:
    return {
        "idx": ev.idx,
        "kind": ev.kind,
        "score": round(ev.score, 6),
        "title": ev.title,
        "doc_id": ev.doc_id,
        "chunk_index": ev.chunk_index,
        "date": ev.date,
        "text": ev.text,
        "locations": ev.locations,
        "thumb_url": ev.thumb_url,
    }


def _gen(plan: AskPlan):
    """生成器调用点：统一带上历史（D141）。

    🔑 抽出来是因为 `stream_answer` 里有两条分支（one_shot / 逐帧），
    早期版本只改了其中一条 ⇒ 流式与非流式的行为不一致
    （那种 bug 的症状是"同一个问题，curl 与网页给的答案不同"）。
    """
    return plan.generator.stream(
        plan.question, plan.evidences, plan.history
    )


async def stream_answer(
    plan: AskPlan, *, ctx: UserContext, one_shot: bool = False
) -> AsyncIterator[str]:
    """产出 SSE 帧序列：`sources` → `delta`* → `done`。

    ⚠️ 本函数**只**做 LLM HTTP 往返与文本拼接，**不碰 DB**（见模块 docstring）。

    @param one_shot True 时把所有增量合并成一个 `delta` 帧。
        `stream=false` 的调用方（MCP / A2A / curl）不关心逐字输出，
        只要完整答案。**仍然返回 SSE 而不是改成 JSON** ——
        少一条响应格式，三方共用一条代码路径（CONVENTIONS §0.3）。
    """
    t0 = time.perf_counter()

    # ① sources 先发 —— 检索段一结束就发，前端立刻能画引用卡片
    yield _sse(
        "sources",
        {
            "sources": [_evidence_payload(e) for e in plan.evidences],
            "retrieval_ms": plan.outcome.total_ms,
        },
    )

    # ② delta
    chunks: list[str] = []
    #: 外部生成器失败时的降级说明（如实回传，不把"降级"伪装成"正常"）。
    degraded: str | None = None
    # D197 拒答：证据质量 = none 且生成器**不是**抽取式 ⇒ 直接拒答，不调 LLM。
    # 抽取式自己会在"摘不出句子"时拒答（保持其既有文案与行为不变）。
    _q = getattr(plan.outcome, "quality", None)
    if _q is not None and _q.level == "none" and plan.generator.name != "extractive":
        refusal = (
            "没有检索到可用于回答的证据。请换个说法，或用「精确搜索」直接找原文片段。"
        )
        chunks = [refusal]
        yield _sse("delta", {"text": refusal})
    else:
        try:
            if one_shot:
                parts = [p async for p in _gen(plan) if p]
                if parts:
                    chunks = parts
                    yield _sse("delta", {"text": "".join(parts)})
            else:
                async for piece in _gen(plan):
                    if not piece:
                        continue
                    chunks.append(piece)
                    yield _sse("delta", {"text": piece})
        except Exception as exc:  # noqa: BLE001
            # 🔑 外部生成器（openai_compat）的 HTTP 失败**不该变成 500** ——
            #   与"视觉腿挂了检索仍 200、只降级 skipped"是同一条设计纪律。
            #   抽取式是默认生成层：用它把同一批证据再讲一遍，答案仍可核验。
            if plan.generator.name == "extractive":
                raise
            logger.warning(
                "生成器 %s 失败，降级为抽取式：%s", plan.generator.name, exc
            )
            degraded = f"{type(exc).__name__}: {exc}"[:200]
            if not chunks:
                # 还没有任何增量流出 ⇒ 干净地重跑一次抽取式。
                async for piece in ExtractiveGenerator().stream(
                    plan.question, plan.evidences, plan.history
                ):
                    if not piece:
                        continue
                    chunks.append(piece)
                    yield _sse("delta", {"text": piece})
                if not chunks:
                    fallback = (
                        "外部生成器不可用，且证据里没有可摘录的原文片段。"
                        "请检查「设置 → 外部大模型」的配置，或改用「精确搜索」。"
                    )
                    chunks = [fallback]
                    yield _sse("delta", {"text": fallback})
            # 已经流出部分 LLM 文本时不再追加（避免两份答案拼在一起）；
            # `done.degraded` 会如实告诉调用方"这一轮降级了"。

    answer = "".join(chunks)

    # ③ 引用校验 + 落库 + done
    citations, dangling = parse_citations(answer, len(plan.evidences))
    sid = await _persist(plan, ctx, answer, citations)
    yield _sse(
        "done",
        {
            "citations": citations,
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "retrieval_ms": plan.outcome.total_ms,
            "generator": plan.generator.name,
            # 外部生成器失败并降级为抽取式时的原因（未降级为 null）。
            # ⚠️ 不是错误码：这一轮**仍然成功**，只是答案来自抽取式。
            "degraded": degraded,
            # 悬空角标应为空。有的话前端要能提示"这条引用失效了"，
            # 静默吞掉等于让用户点到一个空来源。
            "dangling_citations": dangling,
            # 会话 id 回传：本次新建的话 `plan.session_id` 是 None，
            # 前端要靠这个才能接着多轮问（D7）
            "session_id": sid,
            # 🔑 D141：这一轮带进生成的上下文轮数（0 = 没带）。
            #   前端据此显示"参考了 N 轮上文"，测试据此断言多轮真的接上了。
            "history_turns": len(plan.history),
            # D197：证据质量（拒答能力的信号）—— 前端可据此提示"证据不足"
            "quality": (
                {"level": _q.level, "reason": _q.reason, "max_legs": _q.max_legs}
                if _q is not None
                else None
            ),
            "legs": {
                leg: {"n": len(r.hits), "ms": r.elapsed_ms, "skipped": r.skipped}
                for leg, r in plan.outcome.legs.items()
            },
        },
    )


async def _persist(
    plan: AskPlan, ctx: UserContext, answer: str, citations: list[int]
) -> int:
    """把这一轮问答写进 `chat_sessions` / `chat_messages`（D7 多轮）。返回会话 id。

    ⚠️ 新开 session：请求级 `DbSession` 此时已经 teardown（模块 docstring）。
    """
    async with session_scope() as session:
        sid = plan.session_id
        if sid is None:
            sess = ChatSession(
                kb_id=plan.kb_id,
                kb_ids=plan.kb_ids,
                user_id=ctx.user_id,
                # 用问题做会话标题（前端可改，契约 §7 有 PATCH /sessions/{id}）
                title=(plan.question or "").strip().replace("\n", " ")[:80] or None,
            )
            session.add(sess)
            await session.flush()
            sid = sess.id
        session.add_all(
            [
                ChatMessage(session_id=sid, role="user", content=plan.question),
                ChatMessage(
                    session_id=sid,
                    role="assistant",
                    content=answer,
                    citations=citations or None,
                ),
            ]
        )
    return int(sid)