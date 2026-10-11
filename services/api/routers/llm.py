"""外部 LLM 配置端点（D170 · 契约 §12）。

页面可配、立即生效：把生成层与 Contextual 切分要用的外部模型配置
（provider / base_url / model / api_key / 自定义请求头 / 温度）落库。

## 🔴 两条与隐私有关的硬规矩

1. **读端点只回掩码**：`api_key` 明文永远不进响应（否则读一次配置就泄露一次）。
   写端点里 `api_key` **不传 = 保持原值**，要清空必须显式 `clear_api_key=true` ——
   否则"只改模型名"会把 key 抹掉而界面上看不出来。
2. **不静默外传**：只有 `provider=openai_compat` **且** `enabled=true`
   **且** base_url/model/api_key 齐全，`external_active` 才为真；否则生成层
   退回抽取式（零外传）。`LlmConfigOut.external_active` 由后端算好，前端不重算。

## ⚠️ 全局单例

这是**实例级**配置（不是按用户/按库）—— 一个实例一个外部模型。
代价是"一个人配的 key 所有人都在用"，本项目定位是个人/小机实例（D8/D23），
已知并接受。写入会记 `updated_by`（谁改的）。
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter
from sqlalchemy import func, select

from ...common.deps import CurrentUser, DbSession
from ...common.errors import Forbidden
from ...common.models import LlmSettings, User, Workspace
from ...common.schemas_ops import LlmConfigOut, LlmTestResp, UpdateLlmReq
from ...retrieval.llm import (
    LLMConfig,
    OpenAICompatGenerator,
    env_llm_config,
    load_llm_config,
    mask_api_key,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["admin"])


async def _assert_can_configure(ctx: CurrentUser, session: DbSession) -> None:
    """谁能改全局 LLM 配置：**工作区 owner**，或单用户实例的唯一用户。

    🔑 为什么要门禁：配置是**全局**的，而 `base_url` 可以指向任意主机 ——
    任何登录用户都能改的话，一个人就能把**所有人的检索原文**导向自己的服务器。
    这不是"改个模型名"的粒度，是一条数据外流通道。
    ⚠️ 单用户实例（D8/D23 最常见的部署）里 owner 判定不适用，放行。
    🔑 D173：超级管理员当然也能配。
    """
    if ctx.is_superadmin:
        return
    owns = (
        await session.execute(
            select(Workspace.id).where(
                Workspace.owner_id == ctx.user_id, Workspace.deleted_at.is_(None)
            )
        )
    ).first()
    if owns is not None:
        return
    n_users = (
        await session.execute(
            select(func.count(User.id)).where(User.is_active.is_(True))
        )
    ).scalar_one()
    if n_users <= 1:
        return
    raise Forbidden("只有工作区 owner 能配置全局外部 LLM（它会用到所有人的检索原文）")


def _out(row: LlmSettings | None) -> LlmConfigOut:
    if row is None:
        # 库里没有行 = 抽取式（隐私默认）。把 env 兜底也反映出来，便于排障。
        cfg = env_llm_config()
        return LlmConfigOut(
            provider=cfg.provider,
            base_url=cfg.base_url,
            model=cfg.model,
            enabled=cfg.enabled,
            has_key=bool(cfg.api_key),
            api_key_masked=mask_api_key(cfg.api_key),
            extra_headers=dict(cfg.extra_headers or {}),
            temperature=cfg.temperature,
            use_for_ask=cfg.use_for_ask,
            use_for_contextual=cfg.use_for_contextual,
            external_active=cfg.usable(),
        )
    cfg = LLMConfig(
        provider=row.provider or "extractive",
        base_url=row.base_url,
        model=row.model,
        api_key=row.api_key,
        extra_headers=dict(row.extra_headers or {}),
        temperature=row.temperature if row.temperature is not None else 0.3,
        enabled=bool(row.enabled),
        use_for_ask=bool(getattr(row, "use_for_ask", True)),
        use_for_contextual=bool(getattr(row, "use_for_contextual", True)),
    )
    return LlmConfigOut(
        provider=row.provider,
        base_url=row.base_url,
        model=row.model,
        enabled=row.enabled,
        has_key=bool(row.api_key),
        api_key_masked=mask_api_key(row.api_key),
        extra_headers=dict(row.extra_headers or {}),
        temperature=cfg.temperature,
        use_for_ask=cfg.use_for_ask,
        use_for_contextual=cfg.use_for_contextual,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
        external_active=cfg.usable(),
    )


@router.get("/admin/llm", response_model=LlmConfigOut, summary="读外部 LLM 配置（Key 只回掩码）")
async def get_llm(ctx: CurrentUser, session: DbSession) -> LlmConfigOut:
    return _out(await session.get(LlmSettings, 1))


@router.put("/admin/llm", response_model=LlmConfigOut, summary="写外部 LLM 配置（页面可配）")
async def update_llm(
    req: UpdateLlmReq, ctx: CurrentUser, session: DbSession
) -> LlmConfigOut:
    await _assert_can_configure(ctx, session)
    row = await session.get(LlmSettings, 1)
    if row is None:
        row = LlmSettings(id=1)
        session.add(row)

    row.provider = req.provider
    row.base_url = (req.base_url or "").strip() or None
    row.model = (req.model or "").strip() or None

    # ⚠️ api_key：不传 = 保持原值；clear_api_key 才清空。见模块 docstring。
    if req.clear_api_key:
        row.api_key = None
    elif req.api_key is not None and req.api_key.strip():
        row.api_key = req.api_key.strip()

    if req.extra_headers is not None:
        row.extra_headers = {
            str(k): str(v) for k, v in req.extra_headers.items() if k and v is not None
        }
    if req.temperature is not None:
        row.temperature = req.temperature
    if req.enabled is not None:
        row.enabled = req.enabled
    else:
        # 首次写入且没给 enabled：按 provider 推断（配了就默认启用，
        # 否则用户以为配好了却没生效 —— 那正是"静默"的形状）。
        if req.provider == "openai_compat":
            row.enabled = True
    # 🔑 D171：细粒度功能开关（不传 = 保持原值）
    if req.use_for_ask is not None:
        row.use_for_ask = req.use_for_ask
    if req.use_for_contextual is not None:
        row.use_for_contextual = req.use_for_contextual

    row.updated_by = ctx.user_id
    await session.commit()
    # 🔑 不打印 key，只打印"谁把 provider 改成了什么"
    logger.info(
        "外部 LLM 配置更新（by user %s）：provider=%s model=%s enabled=%s",
        ctx.user_id, row.provider, row.model, row.enabled,
    )
    return _out(row)


@router.post("/admin/llm/test", response_model=LlmTestResp, summary="用当前配置试发一次请求")
async def test_llm(ctx: CurrentUser, session: DbSession) -> LlmTestResp:
    import anyio.to_thread
    import httpx

    await _assert_can_configure(ctx, session)
    cfg = await load_llm_config(session)
    if not cfg.usable():
        return LlmTestResp(ok=False, message="未启用或配置不全：" + "；".join(cfg.missing()))

    base = (cfg.base_url or "").rstrip("/")
    headers = OpenAICompatGenerator(cfg).headers()
    payload = {
        "model": cfg.model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8,
        "temperature": 0,
        "stream": False,
    }

    def _call() -> tuple[bool, str]:
        # trust_env=False：本机 fclash 代理会截（CONVENTIONS §2.5）
        with httpx.Client(timeout=30, trust_env=False) as c:
            r = c.post(f"{base}/chat/completions", json=payload, headers=headers)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}：{r.text[:200]}"
        return True, "连接成功"

    t0 = time.perf_counter()
    try:
        ok, msg = await anyio.to_thread.run_sync(_call)
    except Exception as exc:  # noqa: BLE001 — 测试端点的全部意义是"别抛"
        ok, msg = False, f"{type(exc).__name__}：{exc}"
    return LlmTestResp(
        ok=ok,
        message=msg,
        latency_ms=int((time.perf_counter() - t0) * 1000),
        model=cfg.model,
    )
