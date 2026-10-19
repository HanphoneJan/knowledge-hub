"""外部 LLM 配置（D170）的单元测试。

覆盖：配置对象的生效判据、掩码、自定义请求头、生成器选择，以及
`PUT/GET /admin/llm` 的路由逻辑（用假 session，不依赖网络）。
"""

from __future__ import annotations

import asyncio

import pytest

from services.common.auth import UserContext
from services.common.schemas_ops import UpdateLlmReq
from services.retrieval.llm import (
    ExtractiveGenerator,
    LLMConfig,
    OpenAICompatGenerator,
    get_generator,
    mask_api_key,
)

_LOOP = asyncio.new_event_loop()


def run(coro):
    return _LOOP.run_until_complete(coro)


def teardown_module() -> None:
    _LOOP.close()


def _ctx(user_id: int = 7) -> UserContext:
    return UserContext(
        user_id=user_id, display_name="u", email="u@t.local", api_key_id=None
    )


class _Res:
    def __init__(self, first=None, scalar=None) -> None:
        self._first = first
        self._scalar = scalar

    def first(self):
        return self._first

    def scalar_one(self):
        return self._scalar


class _Sess:
    """极简假 session：`get` 返回预置行，`add` 记下新行。

    `execute` 供 `_assert_can_configure` 用：`owns` 决定"是不是工作区 owner"，
    `n_users` 决定单用户实例的放行。
    """

    def __init__(self, row=None, *, owns: bool = True, n_users: int = 1) -> None:
        self.row = row
        self.added: list = []
        self.commits = 0
        self.owns = owns
        self.n_users = n_users

    async def get(self, _model, _pk):  # noqa: ANN001
        return self.row

    async def execute(self, _stmt):  # noqa: ANN001
        return _Res(first=(1,) if self.owns else None, scalar=self.n_users)

    def add(self, obj) -> None:  # noqa: ANN001
        self.added.append(obj)
        self.row = obj

    async def commit(self) -> None:
        self.commits += 1


# ============================================================
# 配置对象
# ============================================================


def test_mask_api_key_keeps_prefix_and_suffix():
    assert mask_api_key("sk-abcdefghijklmnopqrstuvwxyz1234") == "sk-ab…1234"
    assert mask_api_key("short") == "sh…"
    assert mask_api_key(None) is None
    assert mask_api_key("") is None


def test_config_usable_only_when_fully_configured():
    assert not LLMConfig(provider="extractive").usable()
    # provider 对但没启用 → 不可用
    assert not LLMConfig(provider="openai_compat", base_url="u", model="m",
                         api_key="k", enabled=False).usable()
    # 启用但缺 key → 不可用
    assert not LLMConfig(provider="openai_compat", base_url="u", model="m",
                         enabled=True).usable()
    ok = LLMConfig(provider="openai_compat", base_url="u", model="m",
                   api_key="k", enabled=True)
    assert ok.usable()
    assert ok.effective_provider() == "openai_compat"
    # 不可用时**退回抽取式**，而不是抛
    assert LLMConfig(provider="openai_compat").effective_provider() == "extractive"


def test_missing_lists_the_actionable_reasons():
    m = LLMConfig(provider="openai_compat", enabled=True).missing()
    assert any("base_url" in x for x in m)
    assert any("model" in x for x in m)
    assert any("api_key" in x for x in m)


# ============================================================
# 细粒度功能开关（D171）
# ============================================================


def _usable(**kw) -> LLMConfig:
    base = dict(provider="openai_compat", base_url="https://gw/v1", model="m",
                api_key="k", enabled=True)
    base.update(kw)
    return LLMConfig(**base)


def test_usable_for_respects_per_feature_switch():
    assert _usable().usable_for("ask")
    assert _usable().usable_for("contextual")
    off = _usable(use_for_ask=False)
    assert not off.usable_for("ask"), "凭据齐但问答开关关了 ⇒ 不该外传"
    assert off.usable_for("contextual"), "只关问答不该影响 contextual"
    assert not _usable(use_for_contextual=False).usable_for("contextual")
    # 未知功能一律 False（fail-closed）
    assert not _usable().usable_for("nope")


def test_get_generator_honours_ask_switch():
    assert get_generator(config=_usable()).name == "openai_compat"
    assert isinstance(get_generator(config=_usable(use_for_ask=False)),
                      ExtractiveGenerator)


def test_contextualizer_honours_contextual_switch():
    from services.ingest.contextual import get_contextualizer

    assert get_contextualizer(_usable()) is not None
    assert get_contextualizer(_usable(use_for_contextual=False)) is None
    # 凭据不全时同样 None（不区分原因，调用方记 note）
    assert get_contextualizer(LLMConfig(provider="openai_compat")) is None



# ============================================================
# 自定义请求头
# ============================================================


def test_generator_merges_extra_headers():
    cfg = LLMConfig(
        provider="openai_compat", base_url="https://gw/v1", model="m",
        api_key="sk-x", enabled=True,
        extra_headers={"X-Title": "pkh", "HTTP-Referer": "https://x"},
    )
    h = OpenAICompatGenerator(cfg).headers()
    assert h["Authorization"] == "Bearer sk-x"
    assert h["X-Title"] == "pkh"
    assert h["HTTP-Referer"] == "https://x"


def test_extra_headers_can_override_authorization():
    """有些网关用别的鉴权头名 —— 自定义头放后面，允许覆盖。"""
    cfg = LLMConfig(
        provider="openai_compat", base_url="https://gw/v1", model="m",
        api_key="sk-x", enabled=True, extra_headers={"Authorization": "Custom y"},
    )
    assert OpenAICompatGenerator(cfg).headers()["Authorization"] == "Custom y"


# ============================================================
# 生成器选择
# ============================================================


def test_get_generator_uses_config_when_usable():
    cfg = LLMConfig(provider="openai_compat", base_url="https://gw/v1",
                    model="m", api_key="k", enabled=True)
    assert get_generator(config=cfg).name == "openai_compat"


def test_get_generator_falls_back_to_extractive_when_not_usable():
    cfg = LLMConfig(provider="openai_compat", enabled=True)  # 缺 key
    gen = get_generator(config=cfg)
    assert isinstance(gen, ExtractiveGenerator)


# ============================================================
# 路由：GET / PUT
# ============================================================


def test_get_llm_masks_the_key():
    from services.api.routers.llm import get_llm
    from services.common.models import LlmSettings

    row = LlmSettings(
        id=1, provider="openai_compat", base_url="https://gw/v1", model="m",
        api_key="sk-abcdefghijklmnop", enabled=True, extra_headers={},
        temperature=0.3,
    )
    out = run(get_llm(_ctx(), _Sess(row)))
    assert out.has_key is True
    assert out.api_key_masked == "sk-ab…mnop"
    assert "sk-abcdefghijklmnop" not in out.model_dump_json()
    assert out.external_active is True


def test_put_keeps_key_when_omitted():
    from services.api.routers.llm import update_llm
    from services.common.models import LlmSettings

    row = LlmSettings(
        id=1, provider="extractive", api_key="sk-old", extra_headers={},
        temperature=0.3, enabled=False,
    )
    sess = _Sess(row)
    run(update_llm(
        UpdateLlmReq(provider="openai_compat", base_url="https://gw/v1",
                     model="m2", enabled=True),
        _ctx(), sess,
    ))
    assert row.api_key == "sk-old", "不传 api_key 时不能把原 key 抹掉"
    assert row.model == "m2"
    assert row.enabled is True
    assert sess.commits == 1


def test_put_clear_api_key_explicitly():
    from services.api.routers.llm import update_llm
    from services.common.models import LlmSettings

    row = LlmSettings(id=1, provider="extractive", api_key="sk-old",
                      extra_headers={}, temperature=0.3, enabled=False)
    run(update_llm(UpdateLlmReq(provider="extractive", clear_api_key=True),
                   _ctx(), _Sess(row)))
    assert row.api_key is None


def test_put_creates_row_and_defaults_enabled_for_openai():
    from services.api.routers.llm import update_llm

    sess = _Sess(None)
    run(update_llm(
        UpdateLlmReq(provider="openai_compat", base_url="https://gw/v1",
                     model="m", api_key="sk-new"),
        _ctx(), sess,
    ))
    assert sess.added, "库里没有行时应新建"
    assert sess.row.enabled is True, "配了 openai_compat 却没给 enabled 应默认启用"


def test_put_normalises_headers_to_strings():
    from services.api.routers.llm import update_llm
    from services.common.models import LlmSettings

    row = LlmSettings(id=1, provider="extractive", extra_headers={},
                      temperature=0.3, enabled=False)
    run(update_llm(
        UpdateLlmReq(provider="openai_compat", extra_headers={"X-A": "1", "X-B": "2"}),
        _ctx(), _Sess(row),
    ))
    assert row.extra_headers == {"X-A": "1", "X-B": "2"}


def test_put_rejected_for_non_owner_in_multi_user_instance():
    """🔑 全局配置能改 `base_url` = 一条数据外流通道，多用户实例要 owner 才能改。"""
    from services.api.routers.llm import update_llm
    from services.common.errors import Forbidden

    sess = _Sess(None, owns=False, n_users=3)
    with pytest.raises(Forbidden):
        run(update_llm(UpdateLlmReq(provider="extractive"), _ctx(), sess))
    assert sess.commits == 0, "被拒时不能提交"


def test_put_sets_feature_switches():
    from services.api.routers.llm import update_llm
    from services.common.models import LlmSettings

    row = LlmSettings(id=1, provider="extractive", extra_headers={},
                      temperature=0.3, enabled=False)
    run(update_llm(
        UpdateLlmReq(provider="openai_compat", use_for_ask=False,
                     use_for_contextual=True),
        _ctx(), _Sess(row),
    ))
    assert row.use_for_ask is False
    assert row.use_for_contextual is True
