"""Contextual Chunking 的 LLM 适配器（D164/D170）。

文章 §应对策略 的 Contextual Chunking：用 LLM 给每个块生成一句"我在讲什么、
在全文哪一段"，前置到块文本再嵌入，让块"知道自己属于哪篇的哪一节"。

## 🔴 隐私前提：只有**显式配了外部 LLM** 才可能返回非 None

本项目的默认生成层是**抽取式、零外传**（第一卖点）。Contextual Chunking
必然要把原文送进一个 LLM，所以只有 `LLMConfig.usable()` 为真（provider=
openai_compat + enabled + base_url/model/api_key 齐全）才返回一个真正会外传
原文的 callable；否则返回 `None`，由 `strategies` 记一条 note 并把策略退回
`recursive` —— **不静默、不拒绝配置**。

## D170：配置来自库（页面可配），请求头可自定义

配置不再直接读环境变量，而是调用方从 `llm_settings` 表读出来传进来
（`retrieval.llm.load_llm_config_sync`）。`extra_headers` 与 `Authorization`
合并 —— 接入要求特殊请求头的网关不用改代码。

## 为什么是同步 HTTP

摄取跑在**同步 worker** 里（`services/worker`），而 `retrieval.llm` 的生成器
是 async 流式（面向 `/ask` 的 SSE）。这里用同步 `httpx.Client` 打
**非流式** `chat/completions`，自包含、不依赖事件循环。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from ..retrieval.llm import LLMConfig, env_llm_config

logger = logging.getLogger(__name__)

#: 送进 LLM 的全文上限（字符）。太大 token 成本高、也没必要。
MAX_DOC_CHARS = 6000

_CONTEXT_PROMPT = (
    "下面是一份文档的节选，以及其中一个片段。请用**一句中文**（不超过 50 字）"
    "说明这个片段在文档中的位置与主题，用于检索增强。只输出这句话，不要解释。\n\n"
    "文档节选：\n{doc}\n\n片段：\n{chunk}"
)


def get_contextualizer(
    config: LLMConfig | None = None,
) -> Callable[[str, str], str] | None:
    """返回 `(doc_text, chunk_text) -> context`，或 `None`（未配置外部 LLM）。

    @param config 从库里读到的配置；不给则用环境变量兜底（向后兼容）。
    """
    cfg = config or env_llm_config()
    # 🔑 D171：`usable()` 只判凭据；这里还要看"contextual 这个功能"的细粒度开关。
    if not cfg.usable_for("contextual"):
        reasons = cfg.missing() or [
            "use_for_contextual=false（该功能未启用外部 LLM）"
        ]
        logger.info("contextual 不启用（%s）⇒ 策略退回 recursive", "；".join(reasons))
        return None

    base = (cfg.base_url or "").rstrip("/")
    model = cfg.model
    temperature = cfg.temperature
    headers: dict[str, str] = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }
    # 🔑 自定义请求头与 Authorization 合并（可覆盖，见 llm.OpenAICompatGenerator.headers）
    for k, v in (cfg.extra_headers or {}).items():
        if k and v is not None:
            headers[str(k)] = str(v)

    def _contextualize(doc_text: str, chunk_text: str) -> str:
        import httpx

        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": _CONTEXT_PROMPT.format(
                        doc=(doc_text or "")[:MAX_DOC_CHARS], chunk=chunk_text
                    ),
                }
            ],
            "temperature": temperature,
            "stream": False,
        }
        # trust_env=False：本机 fclash 代理会截回环（CONVENTIONS §2.5）。
        with httpx.Client(timeout=60, trust_env=False) as c:
            r = c.post(f"{base}/chat/completions", json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
        return (data["choices"][0]["message"]["content"] or "").strip()

    return _contextualize


__all__ = ["get_contextualizer", "MAX_DOC_CHARS"]
