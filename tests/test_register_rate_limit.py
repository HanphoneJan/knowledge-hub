"""🔴 D135：`/auth/register` 的限流（它**不需要 API Key**，而部署已对公网可达）。

## 三件要分别验的事

1. 🔑 **超限被拒**：第 N+1 次注册返回 429，且**带 `Retry-After`**。
   验收标准是"响应体里的错误码是 `RATE_LIMITED`" —— 不是"状态码是 429"。
2. 🔑 **限流先于业务判断**：超限时**连"该邮箱已注册"都不泄露**
   （那是资源探测信号）。判据：一个**已存在**的邮箱在超限窗口里得到 429 而不是 400。
3. 🔑 **桶的键**：真实客户端 IP 取自 `CF-Connecting-IP`（Cloudflare Tunnel 下
   uvicorn 看到的对端是本机），而**对端可路由时不信转发头**（否则外部能伪造）。

## ⚠️ 本机来源单独一桶

验收脚本与本机 curl 都从 loopback 来，若与公网共用配额，
**跑一轮全量基线就会把自己限死**（一轮约 10 次注册）。所以
`client_key()` 返回 `"local"` 时走宽松桶 `LOCAL_REGISTER_LIMIT`。
这条设计的后果与风险写在 `services/api/rate_limit.py` 的 docstring 里。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from services.api import rate_limit as rl

#: 一个模块级 loop（CONVENTIONS.md §9：asyncpg 连接池绑定创建它的那个 loop）
_LOOP = asyncio.new_event_loop()


@pytest.fixture(autouse=True)
def _clean_buckets():
    rl.reset_buckets()
    yield
    rl.reset_buckets()


class _Headers(dict):
    """够用的假 headers（大小写不敏感地按原样取即可）。"""


# ============================================================
# 1. 桶键怎么算
# ============================================================


def test_local_peer_without_forward_header_is_local():
    assert rl.client_key("127.0.0.1", _Headers()) == "local"
    assert rl.client_key("::1", _Headers()) == "local"
    assert rl.client_key(None, _Headers()) == "local"


def test_forward_header_is_used_when_peer_is_local():
    """🔑 本机对端 + Cloudflare 的头 ⇒ 那是真实客户端 IP。"""
    h = _Headers({"cf-connecting-ip": "203.0.113.9"})
    assert rl.client_key("127.0.0.1", h) == "ip:203.0.113.9"
    # XFF 的**第一个**值是调用方自己写的，取它等于信任调用方 ——
    # 但只在"对端是本机"（也就是刚穿过隧道）时才这么干。
    h2 = _Headers({"x-forwarded-for": "198.51.100.7, 10.0.0.1"})
    assert rl.client_key("127.0.0.1", h2) == "ip:198.51.100.7"


def test_forward_header_is_ignored_when_peer_is_public():
    """🔴 对端是**可路由**地址时不采信转发头 —— 那是外部自己发的。

    否则"直连开端口 + 伪造 `CF-Connecting-IP`"就能无限换桶限流。
    """
    h = _Headers({"cf-connecting-ip": "203.0.113.9"})
    assert rl.client_key("198.51.100.7", h) == "ip:198.51.100.7"


def test_public_peer_without_headers_is_its_own_bucket():
    assert rl.client_key("198.51.100.7", _Headers()) == "ip:198.51.100.7"


def test_non_ip_peer_is_not_trusted():
    """⚠️ 怪对端（代理后的 unix socket 之类）⇒ 走"直连分支"，不采信头。"""
    h = _Headers({"cf-connecting-ip": "203.0.113.9"})
    key = rl.client_key("weird-name", h)
    assert key != "ip:203.0.113.9", "非 IP 对端却采信了转发头"


# ============================================================
# 2. 计数行为
# ============================================================


def test_limit_blocks_after_n_hits():
    for i in range(3):
        ok, _ = rl.check(3, window=60, key="k", now=1000.0 + i)
        assert ok, f"第 {i + 1} 次不该被拒"
    ok, retry = rl.check(3, window=60, key="k", now=1003.0)
    assert not ok
    assert 1 <= retry <= 60


def test_window_is_sliding_not_fixed():
    """🔑 滑动窗口：第 4 次在"窗口末尾"来，第 1 次就该出窗了。

    固定分桶的边界效应会让"窗口开头 N 次 + 窗口末尾 N 次"在 1 秒内通过 2N 次。
    """
    rl.check(3, window=10, key="k", now=1000.0)
    rl.check(3, window=10, key="k", now=1005.0)
    rl.check(3, window=10, key="k", now=1009.0)
    # 第 1 次（t=1000）已出窗（现在 1011，距它 11s ≥ 10）
    ok, _ = rl.check(3, window=10, key="k", now=1011.0)
    assert ok, "滑动窗口下第 4 次应该放行（第 1 次已出窗）"


def test_buckets_are_independent():
    for i in range(3):
        rl.check(3, window=60, key="a", now=1000.0 + i)
    ok, _ = rl.check(3, window=60, key="b", now=1002.0)
    assert ok, "另一个桶不该受影响"
    ok, _ = rl.check(3, window=60, key="local", now=1002.0)
    assert ok, "本机桶不该受影响"


def test_expired_hits_are_cleaned_up():
    for i in range(50):
        rl.check(10, window=5, key="k", now=1000.0 + i * 10)
    assert len(rl._buckets["k"]) <= 2, (
        f"过期项没清掉，桶会无限增长：{len(rl._buckets['k'])}"
    )


def test_local_bucket_is_much_looser_than_remote():
    """🔑 本机桶必须比公网桶宽 —— 否则一轮全量基线（≈10 次注册）会把自己限死。"""
    assert rl.LOCAL_REGISTER_LIMIT >= rl.REGISTER_LIMIT * 5, (
        "本机桶不够宽：验收脚本与本机 curl 都共用它"
    )

# ============================================================
# 3. 端点接线（真跑一次 HTTP 层语义）
# ============================================================


def test_register_endpoint_enforces_the_limit_before_any_business_check(monkeypatch):
    """🔑 超限时**连"邮箱已注册"都不泄露** —— 那是资源探测信号。

    ⚠️ 判据要挑一个**必然已存在**的邮箱：只有在这种情况下仍然返回 429，
    才证明限流排在业务判断**之前**。

    🔑 还要把 `allow_register` 打开（D138）：这台机器的推荐配置是
    `ALLOW_REGISTER=false`，而**开关在限流之前** —— 于是"远端来源"会先拿到
    403，限流那条路径**根本走不到**（这正是 §5.138 第三行记的那个互斥）。
    本条要测的是**限流**，所以先把遮住它的东西挪开。
    """
    from services.common.config import get_settings

    monkeypatch.setattr(get_settings(), "allow_register", True)
    import asyncio

    from sqlalchemy import select

    from services.api.routers.auth import register
    from services.common.db import session_scope, sync_session_scope
    from services.common.errors import RateLimited
    from services.common.models import User
    from services.common.schemas import RegisterReq

    try:
        with sync_session_scope() as s:
            taken = s.execute(select(User.email).limit(1)).scalar_one_or_none()
        if taken is None:
            pytest.skip("库里还没有用户")

        # 把某个"公网样例"客户端的桶打满。
        # ⚠️ **必须用当前 monotonic 打底**，不能写死 `1_000_000.0`：
        #   限流窗口是相对 `time.monotonic()` 的，机器 uptime 一旦超过
        #   "写死值 + 窗口"，这些预填就全部过期 ⇒ 端点走到业务判断，
        #   测试变成"上线十来天后自动变红"的**时间炸弹**（实测踩过）。
        key = "ip:203.0.113.77"
        base = time.monotonic()
        for i in range(rl.REGISTER_LIMIT):
            rl.check(rl.REGISTER_LIMIT, rl.REGISTER_WINDOW_SECONDS, key,
                     now=base + i)

        class _Req:  # noqa: N801 — 只要 .client 与 .headers
            class client:  # noqa: N801
                host = "127.0.0.1"

            headers = {"cf-connecting-ip": "203.0.113.77"}

        async def _call():  # noqa: ANN202
            async with session_scope() as session:
                await register(
                    req=RegisterReq(email=taken, password="pw12345678",
                                    display_name="x"),
                    session=session, request=_Req(),
                )

        with pytest.raises(RateLimited):
            _LOOP.run_until_complete(_call())
    finally:
        rl.reset_buckets()


def test_rate_limited_error_carries_retry_after_header():
    """🔑 429 要带 `Retry-After` **头**。

    ⚠️ 夹在 JSON 的 `detail` 里不算 —— 客户端（与标准中间件）按 HTTP 头取。

    ⚠️ 这里判的是**处理器层**（`PkhError.headers` → `JSONResponse.headers`），
    而不是端到端：`TestClient` 每次调用都开新 event loop，而 asyncpg 的
    连接池绑定创建它的那个 loop ⇒ 穿过中间件就会报
    `got Future ... attached to a different loop`（CONVENTIONS.md §9）。
    端到端那一条由 `eval/api_1b_check.py` §19 负责（真 HTTP，真头）。
    """
    import asyncio

    from services.api.main import pkh_error_handler
    from services.common.errors import RateLimited

    class _Req:  # noqa: N801 — 处理器只读 .state.request_id
        state = type("S", (), {"request_id": "req_test"})()

    exc = RateLimited("太频繁", headers={"Retry-After": "123"})
    resp = _LOOP.run_until_complete(pkh_error_handler(_Req(), exc))
    assert resp.status_code == 429
    assert resp.headers.get("retry-after") == "123", dict(resp.headers)
    assert b"RATE_LIMITED" in resp.body

    # 没有 headers 的错误不应凭空多出这个头
    plain = _LOOP.run_until_complete(pkh_error_handler(_Req(), RateLimited("x")))
    assert "retry-after" not in plain.headers, dict(plain.headers)
    del asyncio
