"""可插拔生成层（D2：统一走 HTTP，模型可插拔，不绑定厂商）。

两个实现：

| provider | 说明 |
|---|---|
| `extractive`（**默认**） | 零依赖零外传。答案句子全部**摘自检索到的原文**，所以引用 100% 可核验 |
| `openai_compat` | 任何 OpenAI 协议的服务（deepseek / 通义 / 本地 vLLM / Ollama） |

## 为什么默认是抽取式，而不是"接一个大模型"

**隐私红线**：检索出的内容是聊天记录、简历、他人文档的片段。
把它们发给外部 API 等于把私人数据送出本机。所以外部 LLM **必须显式配置**
（`LLM_BASE_URL` + `LLM_MODEL` + `LLM_API_KEY`）才可用，
不配置时 `get_generator("openai_compat")` 直接抛错 —— 不是静默降级成抽取式，
那样用户会以为自己在用 LLM。

本机**无 GPU**，本地 LLM 只有 3~8 tok/s（D46），首 token 逼近 2 秒上限。
抽取式没有这个问题，且引用可核验。

## 抽取式的"生成"其实只是**选句**

它做三件事：切句 → 用 BM25 查询词给句子打分 → 每条证据最多贡献 2 句
（防止一条 chunk 刷屏）。所以答案里每一句都能在 `sources` 里找到原文，
不会出现"模型编了一句不存在的话"。这是抽取式唯一但足够的优势。

## 切句要认说话人标记

聊天文本普遍**没有标点**，用 `[昵称]` 前缀标记说话人
（PROGRESS §5.4）。只按句末标点切会切出
`…[张三] 你吃饭了吗[李四] …` 这种跨消息碎片。所以先按说话人标记切。
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..common.config import get_settings
from .bm25 import tokenize

logger = logging.getLogger(__name__)

#: 说话人标记。聊天 chunk 的天然语义单元。
SPEAKER_RE = re.compile(r"\[[^\]\[]{1,12}\]")
#: 句末标点（文档/博客正文靠它）
SENT_SPLIT = re.compile(r"(?<=[。！？!?；;])|(?<=\. )|\n")

#: 角标 `[n]`
CITATION_RE = re.compile(r"\[(\d+)\]")

MAX_SENTENCE_LEN = 160

#: 句子下限。⚠️ **不能设成 8**（第一版就是这么写的，实测丢人）：
#:
#: - 聊天消息普遍很短：`你吃饭了吗` 5 字、`第三句话收尾。` 7 字 —— 下限 8 会把它们
#:   **整条丢掉**，而聊天正是本项目最大的数据源（87,057 条）。
#:   写单测时才发现：切句结果是**空列表**。
#: - 下限的作用是滤掉 `嗯` `哦` 这类语气词，2 个字就够。
MIN_SENTENCE_LEN = 4


# ============================================================
# 外部 LLM 配置（D170）—— 从库读，支持自定义请求头
# ============================================================

#: 视作"OpenAI 兼容网关"的 provider 别名。**加网关别名只改这一处。**
OPENAI_PROVIDERS = frozenset(
    {"openai", "openai_compat", "openai-compatible", "deepseek"}
)

#: 🔑 D171：哪些功能可以用外部 LLM（细粒度开关的**唯一**清单）。
#: 加一个功能 = 加这里 + `LLMConfig` 一个字段 + `usable_for` 一条分支。
LLM_FEATURES: tuple[str, ...] = ("ask", "contextual")


@dataclass
class LLMConfig:
    """一次生成/上下文所用的 LLM 配置（**与存储解耦**）。

    来自 `llm_settings` 表（页面可配，D170）；库里没有行时回退到环境变量
    （向后兼容 —— 老部署只改 `.env` 也能用）。

    ## 生效判据只有一个地方算（`effective_provider`）

    `provider=openai_compat` **且** `enabled=True` **且** base_url/model/api_key
    齐全，才真的外传。任何一项不满足都退回抽取式 —— 但**不静默**：
    调用方拿到的是 `ExtractiveGenerator`，且 `usable()` 会如实说明差什么。
    """

    provider: str = "extractive"
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    #: 自定义请求头（与 `Authorization` 合并）—— 接入新网关不用改代码
    extra_headers: dict[str, str] = field(default_factory=dict)
    temperature: float = 0.3
    enabled: bool = False
    #: 🔑 D171：细粒度功能开关。"凭据齐不齐"由 `usable()` 答，
    #: "这个功能要不要外传"由这两个 + `usable_for()` 答。
    use_for_ask: bool = True
    use_for_contextual: bool = True

    def missing(self) -> list[str]:
        """外传还差什么（用于给用户一句能照着修的话）。"""
        if not self.enabled:
            return ["enabled=false（外部 LLM 未启用）"]
        out: list[str] = []
        if (self.provider or "").lower() not in OPENAI_PROVIDERS:
            out.append("provider 不是 openai_compat")
        if not self.base_url:
            out.append("base_url 为空")
        if not self.model:
            out.append("model 为空")
        if not self.api_key:
            out.append("api_key 为空")
        return out

    def usable(self) -> bool:
        return not self.missing()

    def feature_on(self, feature: str) -> bool:
        """这个功能的细粒度开关开没开（不判凭据）。"""
        return {
            "ask": self.use_for_ask,
            "contextual": self.use_for_contextual,
        }.get(feature, False)

    def usable_for(self, feature: str) -> bool:
        """这个功能能不能真的走外部 LLM = 凭据齐全 **且** 该功能开关打开。"""
        return self.usable() and self.feature_on(feature)

    def effective_provider(self) -> str:
        return "openai_compat" if self.usable() else "extractive"


def _env_config() -> LLMConfig:
    """环境变量兜底（库里没有行时用）。"""
    s = get_settings()
    name = (s.llm_provider or "extractive").lower()
    return LLMConfig(
        provider=name,
        base_url=s.llm_base_url or None,
        model=s.llm_model or None,
        api_key=s.llm_api_key or None,
        extra_headers={},
        temperature=0.3,
        # env 里写了 openai_compat 就视作已启用（老部署的行为不变）
        enabled=name in OPENAI_PROVIDERS,
        use_for_ask=True,
        use_for_contextual=True,
    )


def env_llm_config() -> LLMConfig:
    """环境变量兜底配置（公开版；`contextual` 等模块在没拿到库配置时用）。"""
    return _env_config()


def _row_config(row: Any) -> LLMConfig:
    return LLMConfig(
        provider=row.provider or "extractive",
        base_url=row.base_url,
        model=row.model,
        api_key=row.api_key,
        extra_headers=dict(row.extra_headers or {}),
        temperature=row.temperature if row.temperature is not None else 0.3,
        enabled=bool(row.enabled),
        # getattr：向后兼容"加这两列之前"构造的假行（单测）
        use_for_ask=bool(getattr(row, "use_for_ask", True)),
        use_for_contextual=bool(getattr(row, "use_for_contextual", True)),
    )


def load_llm_config_sync(session: Any) -> LLMConfig:
    """同步读（worker 用）。"""
    from ..common.models import LlmSettings

    row = session.get(LlmSettings, 1)
    return _row_config(row) if row is not None else _env_config()


async def load_llm_config(session: Any) -> LLMConfig:
    """异步读（API 用）。"""
    from ..common.models import LlmSettings

    row = await session.get(LlmSettings, 1)
    return _row_config(row) if row is not None else _env_config()


def mask_api_key(key: str | None) -> str | None:
    """`sk-abc…wxyz` 式掩码（首 5 + 尾 4）。够认得出、不够还原。"""
    if not key:
        return None
    if len(key) <= 12:
        return key[:2] + "…"
    return f"{key[:5]}…{key[-4:]}"


# ============================================================
# 证据（一条召回结果 = 一条可供引用的证据）
# ============================================================


@dataclass
class Evidence:
    """一条证据。`idx` 就是答案里的 `[n]` 角标，从 1 开始。"""

    idx: int
    doc_id: int
    chunk_index: int
    title: str = ""
    text: str = ""
    score: float = 0.0
    #: 契约的引用类型：text / page / image
    kind: str = "text"
    #: L3 坐标（D33）。前端点角标时据此画框。
    locations: list[dict[str, Any]] | None = None
    date: str = ""
    thumb_url: str | None = None
    #: 命中它的腿与排名，`/search/debug` 用
    legs: list[str] = field(default_factory=list)
    ranks: dict[str, int] = field(default_factory=dict)


# ============================================================
# 对话历史（D141）—— M7 承诺的"上下文窗口管理"
# ============================================================


@dataclass
class Turn:
    """历史里的一轮（`chat_messages` 的一行）。"""

    role: str          # "user" | "assistant"
    content: str


def window_history(
    turns: list[Turn], *, max_turns: int, max_chars: int
) -> list[Turn]:
    """从**最新往回**取历史，直到轮数或字符预算用完。

    🔑 **按整轮丢弃**，不截断单条消息的中段：半句话进 prompt 比不进去更糟
    （模型会拿一个残缺的问句去推理，答案莫名其妙）。
    ⇒ 预算不够时**丢最旧的**，保最新的 —— 邻近的上下文比久远的上下文有用。

    ⚠️ 为什么要"窗口"而不是"全带"：历史是**用户自己的数据**，
    全带出去会让 token 成本随轮次线性上升，而且久远的问答往往与本轮无关
    （模型被无关上下文带偏，答案质量反而下降）。
    ⚠️ `max_turns=0` 或 `max_chars<=0` 一律返回空 —— 这是"关掉多轮"的开关，
    不能被当成"无限"。
    """
    if max_turns <= 0 or max_chars <= 0:
        return []
    kept: list[Turn] = []
    used = 0
    for turn in reversed(turns):
        if len(kept) >= max_turns:
            break
        n = len(turn.content)
        if used + n > max_chars:
            # 🔑 **跳过**这一轮、继续看更旧的，而不是 `break`。
            #   差别只出现在"最新一轮超预算"时，而那恰好是**用户追问**的时刻：
            #   上一轮回答动辄几千字，`break` 会让这一轮**完全失去上文**
            #   （追问"那栏填什么"于是检索不到东西）。
            #   代价是可能出现"连续两条 user 消息"—— 那是合法的，
            #   而且我们取回历史的主要目的正是**上一轮的问句**（给 `expand_query`）。
            continue
        kept.append(turn)
        used += n
    kept.reverse()                     # 回到时间顺序
    return kept


#: 指代词（用于"这轮问题太短、很可能在指代上文"的判断）。
#:
#: 🔑 命中它**不等于**要扩展 —— 还要"有历史"+"问题够短"（见 `expand_query`）。
#: 刻意保守：宁可漏扩展（回答稍差）也不要错误扩展（把不相关的上文塞进检索，
#: 污染召回）。这些词都是**中文**的，英文的 `it/that` 不在库里 ——
#: 因为本项目的数据（博客、聊天、办公文档）几乎全是中文。
ANAPHORA_MARKERS = (
    "它", "他们", "这个", "那个", "这份", "那份", "上面", "刚才", "刚刚",
    "继续", "详细", "再说说", "还有呢", "那栏", "那家", "那段", "那篇",
    "这些", "那些", "前者", "后者", "其中", "其他",
)


def expand_query(
    question: str, history: list[Turn], *, max_chars: int
) -> str:
    """把这轮问题接上**上一轮的问句**（仅在"短 + 指代 + 有历史"时）。

    🔑 为什么需要它：多轮里最常见的真实追问是"那考察情况那栏填什么"——
    **单看这一句检索不到任何东西**（"那栏"在语料里不是词），
    而模型侧的历史帮不上检索（检索发生在生成之前）。
    ⇒ 把上一轮的问句并进检索 query，是**与生成器无关**的修法：
    抽取式（默认）与大模型都受益。

    ⚠️ 三条门槛同时满足才扩展，缺一不可：
      ① **有历史**（否则没有"上文"）
      ② 问题**够短**（超过 `max_chars` 说明用户把话说全了，不该加料）
      ③ **含指代词**（否则这一句是自足的）
    ⚠️ 返回的是**新字符串**，原问题不动（审计里两个都记，见 `ask.prepare`）。
    """
    if not history or max_chars <= 0 or len(question) > max_chars:
        return question
    if not any(m in question for m in ANAPHORA_MARKERS):
        return question
    prev_user = next(
        (t.content for t in reversed(history) if t.role == "user"), None
    )
    if not prev_user:
        return question
    return f"{question}（上文问的是：{prev_user}）"


# ============================================================
# 生成器接口
# ============================================================


class Generator(Protocol):
    name: str

    async def stream(
        self, question: str, evidences: list[Evidence],
        history: "list[Turn]" = (),
    ) -> AsyncIterator[str]:
        """产出答案文本的增量片段。**不要**一次 yield 整段 —— D46 要靠 SSE 逐字掩盖慢速。

        @param history 已按窗口裁剪好的对话历史（D141）。**默认空** ⇒
            老签名照旧能用（自定义生成器不会因为升级就崩）。
        """
        ...


# ============================================================
# 抽取式（默认）
# ============================================================


def split_sentences(
    text: str, *, min_len: int = MIN_SENTENCE_LEN, max_len: int = MAX_SENTENCE_LEN
) -> list[str]:
    """切句。三级策略，见模块 docstring。"""
    text = text or ""
    units = SPEAKER_RE.split(text) if SPEAKER_RE.search(text) else [text]
    out: list[str] = []
    for unit in units:
        for raw in SENT_SPLIT.split(unit.strip()):
            s = (raw or "").strip()
            if not s:
                continue
            if len(s) <= max_len:
                out.append(s)
            else:
                # 硬切只在句内，尽量对齐到最近的停顿字符 —— 把话劈两半引用体验很差
                for i in range(0, len(s), max_len):
                    piece = s[i : i + max_len].strip()
                    if len(piece) >= min_len:
                        out.append(piece)
    return [p for p in out if len(p) >= min_len]


def score_sentences(question: str, sentences: list[str]) -> list[float]:
    """句级相关性：查询词覆盖率（TF 饱和）+ 长度轻微惩罚。

    ⚠️ 用 `bm25.tokenize` 而不是自己切词 —— 日期归一化 token 必须一起进来
    （`d20250214`），否则"2025年2月14日"类的提问在句级打分上也会失效。
    """
    q_terms = {t for t in tokenize(question) if t}
    if not q_terms:
        return [0.0] * len(sentences)
    out = []
    for s in sentences:
        low = s.lower()
        hit = sum(1 for t in q_terms if t in low)
        # 除以 query 词数 → 0~1 的覆盖率
        base = hit / len(q_terms)
        # 超长句轻微降权：整段长文本容易霸榜
        out.append(base * (1.0 if len(s) <= MAX_SENTENCE_LEN else 0.7))
    return out


def compose_extractive(
    question: str, evidences: list[Evidence], *, max_sentences: int = 6
) -> tuple[str, list[int]]:
    """抽取式的"生成"。返回 `(答案, 用到的角标)`。

    每条证据最多贡献 2 句 —— 否则一条长 chunk 能把 6 句全占满，
    回答里全是同一个来源，等于没检索。
    """
    cands: list[tuple[float, int, str]] = []
    for ev in evidences:
        if not ev.text or ev.kind == "image":
            continue  # 图片没有文本可摘
        sents = split_sentences(ev.text)
        for sent, sc in zip(sents, score_sentences(question, sents), strict=False):
            if sc > 0:
                cands.append((sc, ev.idx, sent))

    picked: list[tuple[int, str]] = []
    per_src: dict[int, int] = {}
    for sc, idx, sent in sorted(cands, key=lambda x: (-x[0], x[1])):
        if per_src.get(idx, 0) >= 2:
            continue
        per_src[idx] = per_src.get(idx, 0) + 1
        picked.append((idx, sent))
        if len(picked) >= max_sentences:
            break

    if not picked:
        # 一句都选不出来（查询词与原文无字面重合，或召回了但都是图片）。
        # **如实说检索不到**，不要编 —— 契约的 system prompt 也是这个要求。
        if any(ev.kind == "image" for ev in evidences):
            return (
                "检索到一些图片内容，但没有可用于回答的文字证据。",
                [ev.idx for ev in evidences if ev.kind == "image"][:3],
            )
        return (
            "检索到的内容里没有能回答这个问题的证据。可以换个说法，"
            "或用「精确搜索」直接找原文片段。",
            [],
        )

    # 按来源 idx 排序输出，角标 [1][2] 递增可读
    picked.sort(key=lambda x: x[0])
    answer = "\n".join(f"- {sent} [{idx}]" for idx, sent in picked)
    return answer, sorted({idx for idx, _ in picked})


class ExtractiveGenerator:
    """默认生成器。零依赖、零外传、引用 100% 可核验。"""

    name = "extractive"

    async def stream(
        self, question: str, evidences: list[Evidence],
        history: "list[Turn]" = (),
    ):
        # ⚠️ **故意不用** `history`：抽取式是"从证据里挑句子"，
        # 没有语言模型可以条件在对话上；硬塞进去只会在 prompt 里多一堆
        # 无关文本。真正让多轮起作用的是**检索侧**的 `expand_query`
        # （把上一轮问句并进 query）—— 那一条与生成器无关，两种生成器都受益。
        # ⚠️ 所以"抽取式 + 多轮"的效果是"追问能检索到"，不是"追问会接着聊"。
        answer, _ = compose_extractive(question, evidences)
        # 逐句 yield，让 SSE 有真实的增量感（也顺带验证前端能处理多帧）。
        # 抽取式本身不慢，但**契约不能因为当前实现快就退化成一次性输出** ——
        # 前端只有一条 SSE 路径，换成 LLM 时不该改代码。
        for line in answer.splitlines(keepends=True):
            yield line


# ============================================================
# OpenAI 兼容（需显式配置）
# ============================================================

SYSTEM_PROMPT = (
    "你是个人知识库助理。只依据给定证据回答，每句话末尾标注来源编号如 [1]。\n"
    "证据不足就明说，不要编造，也不要用证据之外的知识。"
)


class OpenAICompatGenerator:
    """OpenAI 协议的大模型（D2 的"模型可插拔"）。

    ⚠️ 启用它意味着**检索出的私人原文会离开本机**。项目隐私红线要求
    显式配置才可用，不配置直接抛错而不是静默降级。

    🔑 D170：配置来自 `LLMConfig`（库里的 `llm_settings`，页面可配），
    不再直接读环境变量；`extra_headers` 与 `Authorization` 合并 ——
    接入要求特殊请求头的网关（如 OpenCode Zen）不用改代码。
    """

    name = "openai_compat"

    def __init__(self, config: LLMConfig | None = None) -> None:
        self.config = config or _env_config()
        missing = self.config.missing()
        if missing:
            raise RuntimeError(
                f"外部 LLM 配置不完整：{'；'.join(missing)}。"
                "启用外部大模型会把检索出的私人原文送出本机，与项目隐私红线冲突，"
                "因此必须显式配置。不配置时请用 provider=extractive（默认）。"
            )
        self.base_url = (self.config.base_url or "").rstrip("/")
        self.model = self.config.model or ""
        self.api_key = self.config.api_key or ""
        self.temperature = self.config.temperature

    def headers(self) -> dict[str, str]:
        """请求头：`Authorization` + 用户自定义头。

        ⚠️ 自定义头**放在后面**，可以覆盖 `Authorization`（有些网关用别的
        鉴权头名，比如 `x-api-key`）—— 但默认不覆盖。
        """
        h = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        for k, v in (self.config.extra_headers or {}).items():
            if k and v is not None:
                h[str(k)] = str(v)
        return h

    def _payload(
        self, question: str, evidences: list[Evidence],
        history: "list[Turn]" = (),
    ) -> dict[str, Any]:
        ctx = "\n\n".join(
            f"[{ev.idx}] ({ev.title})\n{ev.text}" for ev in evidences if ev.text
        )
        # 🔑 历史按**真实的 user/assistant 交替**放进去，而不是拼成一段文本 ——
        #   这才是"上下文窗口"对模型的意义（模型能看到"我上一轮问了什么、
        #   我上一轮答了什么"）。⚠️ role 必须原样传：把 assistant 说成 user
        #   会让模型以为那是用户的断言。
        messages: list[dict[str, str]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
        ]
        for turn in history:
            if turn.content:
                messages.append({"role": turn.role, "content": turn.content})
        messages.append(
            {"role": "user", "content": f"证据：\n{ctx}\n\n问题：{question}"}
        )
        return {
            "model": self.model,
            "messages": messages,
            # ⚠️ `getattr` 默认值是为了兼容 `__new__` 构造（单测只测 `_payload`）
            "temperature": getattr(self, "temperature", 0.3),
            "stream": True,
        }

    async def stream(
        self, question: str, evidences: list[Evidence],
        history: "list[Turn]" = (),
    ):
        import anyio.to_thread
        import httpx

        def _iter_lines():
            # ⚠️ trust_env=False：本机有 fclash（127.0.0.1:7890），
            # httpx 默认读 HTTP_PROXY 会把请求送进代理（CONVENTIONS §2.5）
            with httpx.Client(timeout=120, trust_env=False) as c:
                with c.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    json=self._payload(question, evidences, history),
                    headers=self.headers(),
                ) as resp:
                    resp.raise_for_status()
                    for line in resp.iter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        body = line[5:].strip()
                        if body == "[DONE]":
                            break
                        import json

                        try:
                            delta = json.loads(body)["choices"][0]["delta"]
                        except (KeyError, IndexError, ValueError):
                            continue
                        if piece := delta.get("content"):
                            yield piece

        agen = _iter_lines()
        while True:
            try:
                piece = await anyio.to_thread.run_sync(lambda: next(agen, None))
            except StopIteration:
                break
            if piece is None:
                break
            yield piece


def get_generator(
    provider: str | None = None, *, config: LLMConfig | None = None
) -> Generator:
    """取生成器。默认抽取式（见模块 docstring 的隐私理由）。

    @param provider **显式覆盖**（请求级）；给了它就不看 `config`。
    @param config D170：从库里读到的配置（`load_llm_config*`）。
        只有 `config.usable()` 为真才真的外传，否则退回抽取式。
    """
    if config is not None and provider is None:
        # 🔑 D171：细粒度开关 —— 只有"问答"这个功能开了才走外部 LLM
        if config.usable_for("ask"):
            return OpenAICompatGenerator(config)
        return ExtractiveGenerator()

    s = get_settings()
    name = (provider or s.llm_provider or "extractive").lower()
    if name in ("extractive", "local", "default"):
        return ExtractiveGenerator()
    if name in OPENAI_PROVIDERS:
        # 显式点名 openai_compat：用环境里的凭据构造，但**强制** provider/enabled，
        # 这样"缺凭据"时报的是 base_url/model/api_key（而不是"enabled=false"）。
        env = _env_config()
        return OpenAICompatGenerator(
            LLMConfig(
                provider="openai_compat",
                enabled=True,
                base_url=env.base_url,
                model=env.model,
                api_key=env.api_key,
                extra_headers=env.extra_headers,
                temperature=env.temperature,
            )
        )
    raise ValueError(
        f"未知的生成器 provider={name!r}，可用：extractive / openai_compat"
    )


def parse_citations(text: str, n_evidences: int) -> tuple[list[int], list[int]]:
    """从答案文本里解出角标。返回 `(合法角标, 悬空角标)`。

    **悬空角标必须为空** —— 出现 `[9]` 而只有 3 条证据，意味着答案里
    引用了不存在的东西，那比没有引用更糟（用户点进去发现是空的）。
    旧 Demo 的 `/ask` 就有这个校验（`service/rag.py::answer`），搬过来了。
    """
    cited = sorted({int(m) for m in CITATION_RE.findall(text or "")})
    dangling = [c for c in cited if not 1 <= c <= n_evidences]
    return [c for c in cited if 1 <= c <= n_evidences], dangling