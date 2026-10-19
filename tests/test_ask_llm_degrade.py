"""外部生成器失败 → 降级为抽取式（不 500）。

背景：接 `openai_compat` 之后，网关 401/超时/5xx 是**正常运维事件**。
它不该让一次问答变成 Internal Server Error —— 与"视觉腿挂了检索仍 200、
只如实标 skipped"是同一条纪律。降级后的答案来自抽取式（同一批证据），
仍然可核验；`done.degraded` 会带上原因，不把降级伪装成正常。

⚠️ 这里**不发任何 HTTP**：把生成器换成一个必然抛错的假对象，
`_persist` 也换掉，所以不碰 DB —— 可以进 CI 的免基础设施集合。
"""

from __future__ import annotations

import asyncio

import pytest

from services.retrieval import ask
from services.retrieval.engine import SearchOutcome
from services.retrieval.llm import Evidence


class _BoomGenerator:
    """模拟外部 LLM：name 不是 extractive，第一次 stream 就抛错。"""

    name = "openai_compat"

    async def stream(self, question, evidences, history=()):  # noqa: ANN001
        raise RuntimeError("401 Unauthorized: bad api key")
        yield  # pragma: no cover — 让它成为 async generator


async def _no_persist(*args, **kwargs):  # noqa: ANN002, ANN003
    return 1


async def _collect(plan: ask.AskPlan) -> str:
    return "".join(
        [frame async for frame in ask.stream_answer(plan, ctx=None, one_shot=True)]
    )


def _plan(generator) -> ask.AskPlan:  # noqa: ANN001
    return ask.AskPlan(
        question="苹果是什么颜色",
        kb_id=1,
        session_id=None,
        outcome=SearchOutcome(query="苹果是什么颜色", mode="hybrid", total_ms=1),
        evidences=[
            Evidence(idx=1, doc_id=1, chunk_index=0, text="苹果是红色的。")
        ],
        generator=generator,
        retrieval_query="苹果是什么颜色",
    )


def test_external_generator_failure_degrades_to_extractive(monkeypatch):
    """不抛 500；delta 里出现抽取式摘出的原句；done 如实标 degraded。"""
    monkeypatch.setattr(ask, "_persist", _no_persist)

    frames = asyncio.run(_collect(_plan(_BoomGenerator())))

    assert "event: sources" in frames, "sources 必须先发（契约要求）"
    assert "event: delta" in frames, "降级后仍要有答案增量"
    assert "苹果" in frames, "应摘出证据里的原句"
    assert "event: done" in frames, "无论是否降级都必须收尾"
    assert '"degraded"' in frames and "401 Unauthorized" in frames, (
        "降级原因要如实回传，不能静默"
    )


def test_extractive_generator_failure_is_not_swallowed(monkeypatch):
    """抽取式自己失败是真 bug（不是外部依赖问题）—— 必须继续抛，别被吞掉。"""
    monkeypatch.setattr(ask, "_persist", _no_persist)

    class _BrokenExtractive:
        name = "extractive"

        async def stream(self, question, evidences, history=()):  # noqa: ANN001
            raise ValueError("内部实现出错")
            yield  # pragma: no cover

    with pytest.raises(ValueError, match="内部实现出错"):
        asyncio.run(_collect(_plan(_BrokenExtractive())))
