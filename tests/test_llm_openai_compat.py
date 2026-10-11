"""🔑 `openai_compat` 生成器：对**本地 OpenAI 兼容 mock** 验证 SSE 全链路。

## 为什么是 mock 而不是真 LLM

M7 的「LLM 可插拔」在本机**无法对着真实厂商验证**（没有 API 凭据，
而且 D2/D73 的隐私红线要求：把私人原文送出本机必须显式配置）。
但"没法验真机"**不等于"不验"** ——
这个适配器会做的东西是可枚举的：拼请求体、解析 SSE、遇 `[DONE]` 停、
跳过 keepalive、把 `delta.content` 逐块吐出来。这些**全部**可以对着
一个按协议说话的本地服务器验证，而且**比真机更严**：
真机返回的东西每次都不一样，而 mock 可以专门喂它畸形输入。

⚠️ 边界要说清楚：这里验证的是**我们的解析与拼装**，
**不验证任何厂商的真实行为**（字段名差异、鉴权方式、限流…）。
那部分要等有凭据时再验。

## 覆盖的坑

| 坑 | 为什么它真实存在 |
|---|---|
| `data: [DONE]` | 协议结束标记，忘了就**永远读下去** |
| `data:` 前缀是可选空格 | 真实实现有不带空格的 |
| keepalive 空行 / 非 `data:` 行 | 不跳过就把 `json.loads` 炸掉 |
| `delta` 缺 `content`（首包常只有 role） | 不判就 KeyError |
| `choices` 为空数组 | 有的实现在结束时会发一次 |
| 服务端 4xx/5xx | 不 `raise_for_status` 就变成"空回答"，用户以为模型没话说 |
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


class _MockOpenAI(BaseHTTPRequestHandler):
    """按 OpenAI 兼容协议回 SSE 的最小服务器。"""

    #: 由测试用例设置：`(状态码, [要发的原始行])`
    script: tuple[int, list[str]] = (200, [])
    #: 收到的最后一个请求（供断言）
    seen: dict = {}

    def log_message(self, *a):  # noqa: ANN002, ANN201 — 别刷测试输出
        pass

    def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler 的命名
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        type(self).seen = {
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "body": json.loads(raw.decode()) if raw else None,
        }
        status, lines = type(self).script
        self.send_response(status)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if status != 200:
            self.wfile.write(b'{"error":{"message":"boom"}}')
            return
        for line in lines:
            self.wfile.write((line + "\n\n").encode())
        self.wfile.flush()


@pytest.fixture()
def mock_server():
    srv = HTTPServer(("127.0.0.1", 0), _MockOpenAI)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}", _MockOpenAI
    finally:
        srv.shutdown()
        srv.server_close()


def _sse(delta: dict) -> str:
    return "data: " + json.dumps(
        {"choices": [{"delta": delta}]}, ensure_ascii=False
    )


def _generator(base_url: str, monkeypatch: pytest.MonkeyPatch):
    """构造一个指向 mock 的 `OpenAICompatGenerator`。"""
    from services.common import config as config_mod
    from services.retrieval import llm as llm_mod

    fake = config_mod.Settings(
        llm_provider="openai_compat",
        llm_base_url=base_url,
        llm_model="mock-model",
        llm_api_key="sk-mock",
    )
    monkeypatch.setattr(llm_mod, "get_settings", lambda: fake)
    return llm_mod.OpenAICompatGenerator()


def _evidence():
    # ⚠️ `Evidence` 住在 `llm.py` 自己模块里（不是 `retrieval/types.py`）
    from services.retrieval.llm import Evidence

    return [
        Evidence(
            idx=1, doc_id=1, chunk_index=0, title="甲.txt",
            text="张三在 2024 年 7 月入职。", kind="text",
        )
    ]


async def _collect(gen, question="张三什么时候入职？"):
    return "".join([piece async for piece in gen.stream(question, _evidence())])


# ============================================================
# 1. 正常流
# ============================================================


@pytest.mark.anyio
async def test_streams_content_and_stops_at_done(mock_server, monkeypatch):
    """🔑 逐块吐 `delta.content`，遇 `[DONE]` **停**。"""
    base, mock = mock_server
    mock.script = (200, [
        _sse({"role": "assistant"}),          # 首包常只有 role
        _sse({"content": "张三"}),
        _sse({"content": "在 2024 年 7 月入职。"}),
        "data: [DONE]",
        _sse({"content": "【DONE 之后的不该出现】"}),
    ])
    gen = _generator(base, monkeypatch)
    out = await _collect(gen)
    assert out == "张三在 2024 年 7 月入职。", out


@pytest.mark.anyio
async def test_sends_correct_request_shape(mock_server, monkeypatch):
    """🔑 请求体必须符合协议：`model` / `messages` / `stream: true`。

    ⚠️ `stream` 漏了 → 有些实体会返回整段 JSON 而不是 SSE，
    于是**一个字都读不出来**、回答是空的（而端点仍然返回 200）。
    """
    base, mock = mock_server
    mock.script = (200, [_sse({"content": "好"}), "data: [DONE]"])
    gen = _generator(base, monkeypatch)
    await _collect(gen)

    body = mock.seen["body"]
    assert body["model"] == "mock-model"
    assert body["stream"] is True, "stream 不是 true —— 真机可能返回整段 JSON"
    assert body["messages"][0]["role"] == "system"
    # 🔑 证据必须进 prompt，且带编号（引用角标靠它）
    user = body["messages"][1]["content"]
    assert "[1]" in user and "2024 年 7 月入职" in user, user[:120]
    assert mock.seen["auth"] == "Bearer sk-mock", mock.seen["auth"]
    assert mock.seen["path"].endswith("/chat/completions"), mock.seen["path"]


@pytest.mark.anyio
async def test_tolerates_messy_sse_lines(mock_server, monkeypatch):
    """🔑 keepalive 空行、非 `data:` 行、空 `choices` 都不能炸。"""
    base, mock = mock_server
    mock.script = (200, [
        "",
        ": keepalive",
        _sse({"content": "A"}),
        "data: " + json.dumps({"choices": []}),          # 空 choices
        "data: " + json.dumps({"choices": [{"delta": {}}]}),  # delta 无 content
        "data:not-json-at-all",                          # 畸形 JSON
        _sse({"content": "B"}),
        "data: [DONE]",
    ])
    gen = _generator(base, monkeypatch)
    out = await _collect(gen)
    assert out == "AB", out


# ============================================================
# 2. 失败必须**看得见**
# ============================================================


@pytest.mark.anyio
async def test_http_error_raises_instead_of_empty_answer(mock_server, monkeypatch):
    """🔑 4xx/5xx 必须抛错，**不是**静默变成空回答。

    静默空回答是最坏的失败方式：用户看到"模型说没有依据"，
    而真相是 key 错了或额度没了 —— 用户会去调检索参数，
    一直查不到原因（§5.57「数字/结论只能来自真实来源」同源）。
    """
    base, mock = mock_server
    mock.script = (401, [])
    gen = _generator(base, monkeypatch)
    with pytest.raises(Exception) as ei:
        await _collect(gen)
    assert "401" in str(ei.value) or "Unauthorized" in str(ei.value), str(ei.value)


@pytest.mark.anyio
async def test_unconfigured_refuses_loudly(monkeypatch):
    """🔑 没配 base_url/model/api_key 就抛错，**不静默降级**成抽取式。

    静默降级会让用户以为自己在用 LLM（D2 的原话）。
    """
    from services.common import config as config_mod
    from services.retrieval import llm as llm_mod

    monkeypatch.setattr(
        llm_mod, "get_settings",
        lambda: config_mod.Settings(llm_provider="openai_compat"),
    )
    with pytest.raises(RuntimeError) as ei:
        llm_mod.OpenAICompatGenerator()
    msg = str(ei.value)
    # D170：配置从库/环境读，报错文案指向**字段名**（不再硬编码 env 变量名）
    assert "base_url" in msg and "api_key" in msg, msg
    # ⚠️ 报错必须说清代价（私人原文会离开本机），否则用户会以为只是配置问题
    assert "送出本机" in msg or "隐私" in msg, msg


# ============================================================
# 3. 真的会被选中吗（别只测适配器本身）
# ============================================================


def test_get_generator_picks_it_when_fully_configured(monkeypatch):
    """配齐三项时 `get_generator` 必须选它 —— 不然适配器是死代码。"""
    from services.common import config as config_mod
    from services.retrieval import llm as llm_mod

    monkeypatch.setattr(llm_mod, "get_settings", lambda: config_mod.Settings(
        llm_provider="openai_compat", llm_base_url="http://x",
        llm_model="m", llm_api_key="k",
    ))
    gen = llm_mod.get_generator()
    assert gen.name == "openai_compat", gen.name


def test_get_generator_refuses_half_configured(monkeypatch):
    """⚠️ 配一半（缺 key）必须抛错，**不能**当抽取式跑。

    症状最隐蔽的一种：用户填了 base_url 与 model 就以为接上了，
    实际每次问答都在跑抽取式，而界面显示的是 LLM 的名字。
    """
    from services.common import config as config_mod
    from services.retrieval import llm as llm_mod

    monkeypatch.setattr(llm_mod, "get_settings", lambda: config_mod.Settings(
        llm_provider="openai_compat", llm_base_url="http://x", llm_model="m",
    ))
    with pytest.raises(RuntimeError):
        llm_mod.get_generator()
