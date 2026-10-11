"""🔴 D141：**多轮上下文**（M7 承诺"会话历史持久化，**上下文窗口管理**"）。

## 缺口是怎么确认的（不是读代码猜的）

- 前端 `AskPage.tsx` 每次追问都带 `session_id`（`done` 帧回传后存进 state），
  界面上看就是**一段多轮对话**
- 后端 `_assert_session` 还会校验"这个会话属于当前用户 + 当前 KB"
- 而 `retrieval/ask.py::prepare()` **完全不查 `chat_messages`** ——
  历史只在 `_persist()` 里**写**，从来不**读**
- ⇒ 每一轮问答互相不知道对方存在。`chat_messages` 表长得像多轮，实现不是

## 这一组钉的性质

1. 🔑 **窗口按整轮丢弃**、超预算丢**最旧**的（不截断单条消息的中段：
   半句话进 prompt 比不进去更糟）
2. 🔑 `max_turns=0` / `max_chars<=0` = **关掉多轮**，不是"无限"
3. 🔑 **指代式追问才扩展 query**，三条门槛同时满足（有历史 + 够短 + 含指代词），
   且**审计里如实记两个 query**（用户问了什么 / 我们检索了什么）
4. 🔑 历史按**真实 user/assistant 消息**进 OpenAI 兼容 payload（不是拼成一段文本）
5. 🔑 **只读指定会话**的历史 —— 跨会话读等于把别的话题混进来
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from services.retrieval.llm import (
    ANAPHORA_MARKERS,
    Evidence,
    OpenAICompatGenerator,
    Turn,
    expand_query,
    window_history,
)

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

#: 一个模块级 loop（CONVENTIONS.md §9：asyncpg 连接池绑定创建它的那个 loop）
_LOOP = asyncio.new_event_loop()


async def _dispose() -> None:
    from services.common.db import get_async_engine

    await get_async_engine().dispose()


@pytest.fixture(scope="module", autouse=True)
def _own_the_async_engine():
    """🔑 进出都销毁 async 引擎单例（CONVENTIONS.md §9）。"""
    from services.common.db import dispose_engines

    dispose_engines()
    yield
    asyncio.new_event_loop().run_until_complete(_dispose())
    dispose_engines()


def _run_with_session(coro_factory):  # noqa: ANN001, ANN202
    """在一个 async session 里跑一个用到 session 的协程。

    ⚠️ 必须**每次新建** session：跨 loop 复用 session 会在 `commit` 时报
    `attached to a different loop`（CONVENTIONS.md §9 记过三次的坑）。
    """
    from services.common.db import session_scope

    async def _main():
        async with session_scope() as s:
            return await coro_factory(s)

    return _LOOP.run_until_complete(_main())


def _hist() -> list[Turn]:
    return [
        Turn("user", "考察情况那栏怎么填"),
        Turn("assistant", "通常写时间与单位。[1]"),
        Turn("user", "还有别的要求吗"),
        Turn("assistant", "一般还要写明口径。[1]"),
    ]


# ============================================================
# 1. 窗口
# ============================================================


def test_window_keeps_the_newest_turns_in_time_order():
    """🔑 取最近 N 条，且回到**时间顺序**（倒序发给模型是错的）。"""
    out = window_history(_hist(), max_turns=2, max_chars=10_000)
    assert [t.content for t in out] == [
        "还有别的要求吗", "一般还要写明口径。[1]",
    ]


def test_window_drops_oldest_when_over_char_budget():
    """🔑 超字符预算时丢**最旧**的，不是丢最新的。

    ⚠️ 这里只放得下**最后一轮**，所以答案是它 —— 不是"最旧的"那条。
    """
    last = "一般还要写明口径。[1]"
    out = window_history(_hist(), max_turns=99, max_chars=len(last))
    assert [t.content for t in out] == [last], out


def test_window_never_truncates_inside_a_turn():
    """🔑 **整轮**处理：超预算的那条被**整条跳过**，不是截半句。

    🔑 而且是"跳过继续"而不是"就此打住"：上面那条 100 字的回答装不下，
    但下面那句"问"装得下 ⇒ 追问时**仍然拿得到上一轮的问句**
    （这正是 `expand_query` 需要的）。若改成 `break`，
    "上一轮回答很长"就会让这一轮彻底失去上文。
    """
    long_turn = Turn("assistant", "啊" * 100)
    out = window_history([Turn("user", "问"), long_turn],
                         max_turns=5, max_chars=10)
    assert out == [Turn("user", "问")], [t.content for t in out]
    assert all(t.content != "啊" * 100 for t in out)


def test_zero_budget_disables_history():
    """🔑 `0` 是**关掉**，不是"无限" —— 这两种解释的差别是 token 账单。"""
    assert window_history(_hist(), max_turns=0, max_chars=10_000) == []
    assert window_history(_hist(), max_turns=5, max_chars=0) == []
    assert window_history(_hist(), max_turns=-1, max_chars=10) == []


def test_window_of_empty_history_is_empty():
    assert window_history([], max_turns=4, max_chars=100) == []


# ============================================================
# 2. 指代式 query 扩展（三条门槛）
# ============================================================


def test_anaphoric_short_followup_gets_previous_question():
    """🔑 核心场景："那栏填什么" 单看检索不到任何东西。

    🔑 并进去的是**最近那一轮的问句**（`_hist()` 里最后一个 user 是
    "还有别的要求吗"），不是最早那条 —— 上文的"上文"才是相关的那个。
    """
    out = expand_query("那栏填什么", _hist(), max_chars=24)
    assert "还有别的要求吗" in out, out
    assert "考察情况那栏怎么填" not in out, "取了最早那一轮而不是最近的"
    assert out.startswith("那栏填什么"), "原问题必须在最前面（模型先看到它）"


def test_no_expansion_without_history():
    """🔑 没有上文就没有"上文"可并 —— 第一轮绝不能被加料。"""
    q = "那栏填什么"
    assert expand_query(q, [], max_chars=24) == q


def test_no_expansion_when_question_is_long_enough():
    """🔑 问题够长 = 用户把话说全了，不需要加料。"""
    q = "那栏" + "啊" * 40
    assert expand_query(q, _hist(), max_chars=24) == q


def test_no_expansion_without_anaphora_marker():
    """🔑 自足的问题不该被污染（错误扩展会把不相关上文塞进检索）。"""
    q = "考察情况怎么填"
    assert expand_query(q, _hist(), max_chars=24) == q


def test_expansion_uses_the_last_user_turn_not_the_answer():
    """🔑 并进去的是**上一轮的问句**，不是上一轮的回答 ——
    检索要的是"用户在问什么"，不是"模型答了什么"。"""
    out = expand_query("还有呢", [Turn("user", "第一个问题"), Turn("assistant", "答案 A"),
                                  Turn("user", "第二个问题"), Turn("assistant", "答案 B")],
                       max_chars=24)
    assert "第二个问题" in out and "答案 B" not in out, out


def test_anaphora_markers_are_chinese_only_and_that_is_intentional():
    """⚠️ 库里**只有中文**指代词，而这是有意的：数据几乎全是中文。
    钉住这个事实，将来接英文语料时要记得扩列表（而不是以为已经覆盖了）。"""
    assert "它" in ANAPHORA_MARKERS
    assert not any(m.isascii() for m in ANAPHORA_MARKERS), ANAPHORA_MARKERS


# ============================================================
# 3. 生成器接线
# ============================================================


def test_openai_payload_carries_history_as_real_messages():
    """🔑 历史按**真实的 user/assistant 消息**进 payload，而不是拼成一段文本。

    ⚠️ role 必须原样传：把 assistant 说成 user 会让模型以为那是用户的断言。
    """
    g = OpenAICompatGenerator.__new__(OpenAICompatGenerator)
    g.model = "m"
    payload = g._payload(
        "那栏填什么", [Evidence(idx=1, doc_id=1, chunk_index=0, text="正文")],
        _hist(),
    )
    msgs = payload["messages"]
    assert msgs[0]["role"] == "system"
    assert [m["role"] for m in msgs[1:-1]] == ["user", "assistant", "user", "assistant"]
    assert msgs[-1]["role"] == "user" and "那栏填什么" in msgs[-1]["content"]
    # 证据仍然只在本轮那条消息里（不能混进历史）
    assert "正文" in msgs[-1]["content"]
    assert all("正文" not in m["content"] for m in msgs[1:-1])


def test_openai_payload_without_history_is_unchanged_shape():
    """🔑 没有历史时 payload 与 D141 之前**完全一样**（system + 一条 user）。"""
    g = OpenAICompatGenerator.__new__(OpenAICompatGenerator)
    g.model = "m"
    payload = g._payload("问题", [], [])
    assert [m["role"] for m in payload["messages"]] == ["system", "user"]


def test_both_generators_accept_the_history_argument():
    """🔑 接线：两个生成器的 `stream` 都收下 `history`（§5.112：抽了 helper 必须接线）。"""
    import inspect

    from services.retrieval.llm import ExtractiveGenerator

    for cls in (ExtractiveGenerator, OpenAICompatGenerator):
        params = list(inspect.signature(cls.stream).parameters)
        assert "history" in params, f"{cls.__name__}.stream 不收 history：{params}"
        assert inspect.signature(cls.stream).parameters["history"].default == (), (
            f"{cls.__name__}.stream 的 history 必须有默认值 —— "
            "否则老的无参调用会 TypeError"
        )


@pg
def test_history_is_read_only_for_the_given_session():
    """🔑 **只读 `session_id` 指定的那一个会话**。

    ⚠️ 跨会话读历史 = 把另一个话题的上下文混进来；而会话归属由路由层的
    `_assert_session` 校验（必须属于当前用户 + 当前 KB）。
    判据用**真库**：真插两个会话的历史，只指定其中一个。
    """
    from sqlalchemy import select

    from services.common.db import sync_session_scope
    from services.common.models import ChatMessage, ChatSession
    from services.retrieval.ask import load_history

    from services.common.models import User

    with sync_session_scope() as s:
        kb_id = s.execute(select(ChatSession.kb_id).limit(1)).scalar_one_or_none()
        uid = s.execute(select(User.id).limit(1)).scalar_one_or_none()
        if kb_id is None or uid is None:
            pytest.skip("库里还没有会话/用户（先跑一遍 eval/api_1d_check.py）")
        # ⚠️ `chat_sessions.user_id` 是 NOT NULL —— 不给的话插入直接炸，
        # 而那条报错完全指不到"你少填了一列"。
        a = ChatSession(kb_id=kb_id, user_id=uid, title="A")
        b = ChatSession(kb_id=kb_id, user_id=uid, title="B")
        s.add_all([a, b])
        s.flush()
        s.add_all([
            ChatMessage(session_id=a.id, role="user", content="属于A的问题"),
            ChatMessage(session_id=a.id, role="assistant", content="属于A的回答"),
            ChatMessage(session_id=b.id, role="user", content="属于B的问题"),
        ])
        s.flush()
        a_id, b_id = a.id, b.id
    try:
        out = _run_with_session(
            lambda s: load_history(s, a_id, max_turns=8, max_chars=4000)
        )
        assert [t.content for t in out] == ["属于A的问题", "属于A回答".replace("回答", "的回答")], out
        assert all("属于B" not in t.content for t in out), out
        # 关掉时连库都不碰
        assert _run_with_session(
            lambda s: load_history(s, a_id, max_turns=0, max_chars=4000)
        ) == []
        assert _run_with_session(
            lambda s: load_history(s, None, max_turns=8, max_chars=4000)
        ) == []
    finally:
        with sync_session_scope() as s:
            s.execute(ChatMessage.__table__.delete()
                      .where(ChatMessage.session_id.in_([a_id, b_id])))
            s.execute(ChatSession.__table__.delete()
                      .where(ChatSession.id.in_([a_id, b_id])))


# ============================================================
# 4. **接线**（§5.109/§5.112：判据必须落在调用点上）
# ============================================================
#
# ⚠️ 上面那些单元测试只覆盖 `window_history` / `_payload` 两个 helper。
# 第一版变异脚本立刻证明了它们的盲区：把 `prepare()` 里的
# `history = await load_history(...)` 换成 `history = []`、
# 把 `return window_history(...)` 换成 `return turns`、
# 把 `max_turns <= 0` 的守卫放宽、把 `stream_answer` 的历史参数去掉 ——
# **四条变异全部全绿**。⇒ 必须有**穿过 prepare → stream_answer** 的测试。


def _ctx_and_kb():  # noqa: ANN202
    """真库里取一个"能用的" 用户上下文 + 一个 KB（有会话就更好）。"""
    from sqlalchemy import select

    from services.common.auth import UserContext
    from services.common.db import sync_session_scope
    from services.common.models import KnowledgeBase, User

    with sync_session_scope() as s:
        user = s.execute(select(User).limit(1)).scalar_one_or_none()
        if user is None:
            pytest.skip("库里还没有用户（先跑一遍 eval/api_1b_check.py）")
        kb_ids = set(s.execute(
            select(KnowledgeBase.id).where(KnowledgeBase.deleted_at.is_(None))
        ).scalars().all())
        ws_ids = set()
        from services.common.models import WorkspaceMember

        ws_ids = set(s.execute(
            select(WorkspaceMember.workspace_id)
            .where(WorkspaceMember.user_id == user.id)
        ).scalars().all())
        readable = frozenset(kb_ids)
        ctx = UserContext(
            user_id=user.id, display_name=user.display_name, email=user.email,
            api_key_id=None, workspace_ids=frozenset(ws_ids), readable_kb_ids=readable,
        )
    if not readable:
        pytest.skip("库里没有库（先跑一遍 eval/api_1c_check.py）")
    return ctx, sorted(readable)[0]


def _make_session_with_history(kb_id: int, uid: int, texts: list[tuple[str, str]]):
    from services.common.db import sync_session_scope
    from services.common.models import ChatMessage, ChatSession

    with sync_session_scope() as s:
        sess = ChatSession(kb_id=kb_id, user_id=uid, title="D141 探针")
        s.add(sess)
        s.flush()
        s.add_all([ChatMessage(session_id=sess.id, role=r, content=c)
                   for r, c in texts])
        s.flush()
        return int(sess.id)


def _drop_session(sid: int) -> None:
    from services.common.db import sync_session_scope
    from services.common.models import ChatMessage, ChatSession

    with sync_session_scope() as s:
        s.execute(ChatMessage.__table__.delete()
                  .where(ChatMessage.session_id == sid))
        s.execute(ChatSession.__table__.delete().where(ChatSession.id == sid))


@pg
def test_prepare_puts_session_history_into_the_plan(monkeypatch):
    """🔑 **穿过 `prepare`**：会话里的历史真的进了 `plan.history`。

    ⚠️ 这条是为了抓住"`load_history` 存在但没人调"（第一版变异全绿的那种）。
    """
    from services.retrieval.ask import prepare

    from services.common.config import get_settings

    ctx, kb_id = _ctx_and_kb()
    # ⚠️ 放**四条**消息并把窗口设成 2 —— 只放两条的话默认窗口全都能装下，
    #   "有没有真的走 window"就测不出来了（第一版正是这样，变异全绿）。
    monkey = get_settings()
    old_turns = monkey.ask_history_turns
    monkey.ask_history_turns = 2
    sid = _make_session_with_history(kb_id, ctx.user_id, [
        ("user", "上文问题甲"), ("assistant", "上文回答甲"),
        ("user", "上文问题乙"), ("assistant", "上文回答乙"),
    ])
    try:
        plan = _run_with_session(
            lambda s: prepare(s, ctx, "现在问这个", kb_id=kb_id, session_id=sid, top_k=2)
        )
        # 最近的两条，且是时间顺序
        assert [t.content for t in plan.history] == ["上文问题乙", "上文回答乙"], plan.history
    finally:
        monkey.ask_history_turns = old_turns
        _drop_session(sid)


@pg
def test_prepare_honours_the_switch_at_the_call_site(monkeypatch):
    """🔑 `ask_history_turns=0` 时 `plan.history` 为空 —— 守卫在**调用点**生效。

    ⚠️ 判据钉的是 plan 上的字段，不是 helper 的行为：`window_history(…, 0)`
    返回空是 helper 的事，而这里要证明 **`prepare` 真的读了那个配置**。
    """
    from services.common.config import get_settings
    from services.retrieval.ask import prepare

    ctx, kb_id = _ctx_and_kb()
    sid = _make_session_with_history(kb_id, ctx.user_id, [("user", "上文问题")])
    monkeypatch.setattr(get_settings(), "ask_history_turns", 0)
    try:
        plan = _run_with_session(
            lambda s: prepare(s, ctx, "现在问这个", kb_id=kb_id, session_id=sid, top_k=2)
        )
        assert plan.history == [], plan.history
    finally:
        _drop_session(sid)


class _RecordingGenerator:
    """记录自己收到的 `history` 的假生成器。"""

    name = "recording"

    def __init__(self) -> None:
        self.seen: list | None = None

    async def stream(self, question, evidences, history=()):  # noqa: ANN001, ANN202
        self.seen = list(history)
        yield "答[1]\n"


@pg
@pytest.mark.parametrize("one_shot", [False, True], ids=["stream", "one_shot"])
def test_both_stream_branches_pass_history(one_shot: bool):
    """🔑 `stream_answer` 的**两条分支**都要带历史。

    🔴 第一版我把历史参数加在了 `if one_shot:` 那一支里 ——
    于是"同一个问题，curl（非流式）与网页（流式）拿到的上下文不同"，
    而这种分裂**几乎不可能被看出来**（两个答案都对，只是一个有上文）。
    ⇒ 参数化两个分支各钉一次。
    """
    from services.retrieval.ask import AskPlan, stream_answer

    ctx, kb_id = _ctx_and_kb()
    gen = _RecordingGenerator()
    plan = AskPlan(
        question="那栏填什么", kb_id=kb_id, session_id=None,
        outcome=type("O", (), {"total_ms": 1, "items": [], "legs": {}})(),
        evidences=[], generator=gen,
        history=[Turn("user", "上文问题"), Turn("assistant", "上文回答")],
    )
    frames = _LOOP.run_until_complete(_drain(stream_answer(plan, ctx=ctx,
                                                          one_shot=one_shot)))
    assert gen.seen is not None, "生成器根本没被调用"
    assert [t.content for t in gen.seen] == ["上文问题", "上文回答"], gen.seen
    assert any("done" in f for f in frames), frames


async def _drain(aiter):  # noqa: ANN001, ANN202
    return [f async for f in aiter]
