"""🔴 D138：对外加固三件（登录限流 / 自助注册开关 / `/docs` 开关）。

## 为什么要做（实测的缺口，不是理论）

2026-10-04 实测公网入口（域名 + 隧道已上线）：

| 探测 | 结果 | 判定 |
|---|---|---|
| `GET /api/v1/kb`、`/audit`、`/admin/*`、`/mcp` | 401 | ✅ 租户隔离有效 |
| `POST /auth/register`（空体）| **422** | 🔴 注册人人可达（已由 D135 限流兜住）|
| `POST /auth/login` | **200，且当时无任何次数限制** | 🔴 **唯一通往数据的入口没有闸门** |
| `GET /docs`、`/openapi.json` | **200 公开** | 🔴 完整 API 面（含 MCP 工具名）画给任何人 |

⚠️ **登录限流与 Access 无关**：就算上了 Cloudflare Access，
通过了 Access 的账号**照样**没有次数上限。而对本项目（个人单用户实例）来说，
应用内三件比 Access 更合适：`/mcp` 对外时客户端本来就带 API Key，
不需要再叠一层 cookie/Service Token（`POST /mcp` 在**根路径**，也不在
`/api/v1` 下，加一层反向代理规则只会更容易写错）。

## 这一组钉的三个性质

1. 🔑 **只数失败**：成功登录不消耗配额（否则主人被自己的正常用法锁在门外）
2. 🔑 **IP 桶 + 账号桶**都要有：账号桶才是"保护某个具体用户密码"起作用的那个
3. 🔑 `local` 例外只对**本机**生效，公网路径永远不是 `local`
   （实测：公网注册 → 403，本机注册 → 201）
"""
from __future__ import annotations

import asyncio
import time

import pytest

from services.api import rate_limit as rl

#: 一个模块级 loop（CONVENTIONS.md §9：asyncpg 连接池绑定创建它的那个 loop）
_LOOP = asyncio.new_event_loop()


@pytest.fixture(scope="module", autouse=True)
def _own_the_async_engine():
    """🔑 进出都销毁 async 引擎单例 —— 本文件要真调 async 端点。"""
    from services.common.db import dispose_engines

    dispose_engines()
    yield
    asyncio.new_event_loop().run_until_complete(_dispose())
    dispose_engines()


async def _dispose() -> None:
    from services.common.db import get_async_engine

    await get_async_engine().dispose()


@pytest.fixture(autouse=True)
def _clean_buckets():
    rl.reset_buckets()
    yield
    rl.reset_buckets()


class _Req:  # noqa: N801 —— 端点只读 .client.host 与 .headers
    def __init__(self, host: str = "127.0.0.1", **headers: str) -> None:
        self.client = type("C", (), {"host": host})()
        self.headers = headers


# ============================================================
# 1. `/docs` 与 `/openapi.json` 的开关
# ============================================================


def test_docs_routes_off_removes_all_three_urls():
    """🔑 关掉时**三个**都要置 `None` —— 只关 `docs_url` 的话
    `/openapi.json` 还在，而那才是信息泄露的主体。"""
    from services.api.main import docs_routes

    off = docs_routes(False)
    assert off == {"docs_url": None, "redoc_url": None, "openapi_url": None}, off


def test_docs_routes_on_keeps_the_usual_paths():
    from services.api.main import docs_routes

    on = docs_routes(True)
    assert on["docs_url"] == "/docs"
    assert on["openapi_url"] == "/openapi.json"
    assert on["redoc_url"] == "/redoc"


def test_fastapi_really_hides_docs_when_urls_are_none():
    """🔑 机制本身：三个 `None` ⇒ 三条路由都不存在（不是"存在但返回 404 页"）。

    判据是**端点集合**而不是"打一发 404"：如果 FastAPI 改了默认行为
    （比如留一条空路由），打 404 仍然会绿。
    """
    from fastapi import FastAPI

    from services.api.main import docs_routes

    def paths(**kw):  # noqa: ANN003, ANN202
        app = FastAPI(**kw)
        # ⚠️ 别只筛 `APIRoute`：`/docs` `/openapi.json` `/redoc` 在 FastAPI 里
        #   是 `starlette.routing.Route`，不是 APIRoute —— 第一版筛了 APIRoute
        #   于是拿到空集合，这条判据直接变成恒真。
        return {getattr(r, "path", None) for r in app.routes}

    assert {"/docs", "/openapi.json", "/redoc"} <= paths(**docs_routes(True))
    off = paths(**docs_routes(False))
    assert not ({"/docs", "/openapi.json", "/redoc"} & off), off


def test_openapi_method_survives_docs_being_off():
    """🔑 关掉 `/openapi.json` **不能**影响 `app.openapi()` 这个**方法** ——
    `eval/contract_check.py` 靠的就是它（契约校验的输入不能被加固开关砍掉）。"""
    from fastapi import FastAPI

    from services.api.main import docs_routes

    app = FastAPI(**docs_routes(False))

    @app.get("/api/v1/demo")
    async def _demo():  # noqa: ANN202
        return {}

    schema = app.openapi()
    assert "/api/v1/demo" in schema["paths"], (
        "docs 关掉之后 openapi() 拿不到路径 ⇒ contract_check 会误报"
    )


def test_real_app_is_wired_to_the_setting():
    """🔑 **接线**（§5.109/§5.112：抽了 helper 必须落在调用点上）。

    判据是**应用对象真的按 settings 走**，而不是"helper 返回得对"。
    """
    from services.api import main
    from services.common.config import get_settings

    want = main.docs_routes(get_settings().expose_docs)
    assert main.app.docs_url == want["docs_url"], (
        f"app.docs_url={main.app.docs_url!r}，settings 推导出来是 {want['docs_url']!r}"
    )
    assert main.app.openapi_url == want["openapi_url"]


# ============================================================
# 2. 自助注册开关
# ============================================================


def test_registration_closed_is_403_with_its_own_code():
    """🔑 403 + **自己的** code：复用 `ResourceNotFound`（`KB_NOT_FOUND`）会让
    调用方分不清"注册关了"和"你看不到这个资源"—— 两者的正确反应完全不同。"""
    from services.common.errors import RegistrationClosed, ResourceNotFound

    assert RegistrationClosed.status_code == 403
    assert RegistrationClosed.code == "REGISTRATION_CLOSED"
    assert RegistrationClosed.code != ResourceNotFound.code


def test_register_is_closed_for_remote_callers_but_open_for_local(monkeypatch):
    """🔑 关掉自助注册对**公网**生效，对**本机脚本**不误伤。

    ⚠️ 判据选"已存在的邮箱"：它在两条路径下分别应该得到
    `RegistrationClosed`（远端）与 `InvalidRequest`（本机走到业务判断）——
    这同时证明了"开关在业务判断**之前**"。
    """
    from sqlalchemy import select

    from services.api.routers.auth import register
    from services.common.config import get_settings
    from services.common.db import session_scope, sync_session_scope
    from services.common.errors import InvalidRequest, RegistrationClosed
    from services.common.models import User
    from services.common.schemas import RegisterReq

    with sync_session_scope() as s:
        taken = s.execute(select(User.email).limit(1)).scalar_one_or_none()
    if taken is None:
        pytest.skip("库里还没有用户")

    monkeypatch.setattr(get_settings(), "allow_register", False)
    req = RegisterReq(email=taken, password="pw12345678", display_name="x")

    async def _call(request):  # noqa: ANN001, ANN202
        async with session_scope() as session:
            await register(req=req, session=session, request=request)

    # 🔴 带 `CF-Connecting-IP` ⇒ 桶键是 `ip:…` ⇒ 模拟公网
    with pytest.raises(RegistrationClosed):
        _LOOP.run_until_complete(_call(_Req(**{"cf-connecting-ip": "203.0.113.5"})))
    # 本机（loopback 且无转发头）⇒ 仍然放行到业务判断
    with pytest.raises(InvalidRequest):     # "该邮箱已注册"
        _LOOP.run_until_complete(_call(_Req()))


def test_switches_default_to_open_and_env_can_flip_them(monkeypatch):
    """🔑 两件事分开钉：

    1. **代码里的默认是开着** —— 本机开发、pytest、三套 eval、三套 e2e
       都靠自助注册；默认关会让整套基线红，而"忘了开"很难发现。
       ⚠️ 断言必须看 `model_fields`（**代码默认**），不能看 `get_settings()`
       —— 后者会读本机 `.env`，本机正是关着的（第一版就是这么写的，
       于是测试在自己的机器上红）。
    2. **环境变量能翻它**（`.env` / 进程环境都行，大小写不敏感）。
    """
    from services.common.config import Settings

    for name in ("allow_register", "expose_docs"):
        assert Settings.model_fields[name].default is True, (
            f"{name} 的代码默认变成了 False —— 换台机器 clone 下来基线会全红"
        )
        monkeypatch.setenv(name.upper(), "false")
        assert Settings().model_dump()[name] is False, f"{name} 没被环境变量翻掉"
        monkeypatch.delenv(name.upper())


# ============================================================
# 3. 登录失败限流
# ============================================================


def test_ip_bucket_blocks_after_n_failures():
    ok, _, _ = rl.login_fail_budget("ip:203.0.113.9", "a@x")
    assert ok
    for _ in range(rl.LOGIN_FAIL_LIMIT):          # 记满配额
        rl.record_login_fail("ip:203.0.113.9", "a@x")
    ok, retry, which = rl.login_fail_budget("ip:203.0.113.9", "a@x")
    assert not ok and which == "ip" and 1 <= retry <= rl.LOGIN_FAIL_WINDOW_SECONDS


def test_account_bucket_blocks_even_when_every_request_comes_from_a_new_ip():
    """🔑 这是"保护某个具体用户的密码"真正起作用的那个桶：换 IP 无效。"""
    for i in range(rl.LOGIN_FAIL_ACCOUNT_LIMIT):
        ok, _, _ = rl.login_fail_budget(f"ip:198.51.100.{i + 1}", "victim@x")
        assert ok, f"第 {i + 1} 次不该被拒"
        rl.record_login_fail(f"ip:198.51.100.{i + 1}", "victim@x")
    ok, retry, which = rl.login_fail_budget("ip:203.0.113.200", "victim@x")
    assert not ok and which == "account"
    assert retry > rl.LOGIN_FAIL_WINDOW_SECONDS, "账号窗口应当比 IP 窗口长"


def test_account_bucket_is_normalized():
    """🔑 `User.email` 的唯一约束按**原样**比对，而账号桶若不归一化，
    `A@x` 与 `a@x` 就是两个桶 ⇒ 限流可绕。"""
    assert rl.account_key("  A@X  ") == rl.account_key("a@x")


def test_local_bucket_is_looser_but_not_unlimited():
    """🔑 本机桶要比公网宽（基线里没有失败登录），
    但**仍然是有限的** —— "本机不受限"等于本机也没有防护。"""
    assert rl.LOCAL_LOGIN_FAIL_LIMIT > rl.LOGIN_FAIL_LIMIT
    ok, _, _ = rl.login_fail_budget("local", "a@x")
    assert ok
    for _ in range(rl.LOCAL_LOGIN_FAIL_LIMIT):
        rl.record_login_fail("local", "a@x")
    ok, _, _ = rl.login_fail_budget("local", "a@x")
    assert not ok


def test_login_endpoint_limits_failures_and_never_says_which_bucket():
    """🔑 端点级：第 N+1 次失败 → 429；文案**不泄露**是哪个桶
    （"这个账号正在被人尝试"本身就是信息）。"""
    from sqlalchemy import select

    from services.api.routers.auth import login
    from services.common.db import session_scope, sync_session_scope
    from services.common.errors import InvalidRequest, RateLimited
    from services.common.models import User
    from services.common.schemas import LoginReq

    with sync_session_scope() as s:
        taken = s.execute(select(User.email).limit(1)).scalar_one_or_none()
    if taken is None:
        pytest.skip("库里还没有用户")

    req = LoginReq(email=taken, password="definitely-wrong")
    request = _Req(**{"cf-connecting-ip": "203.0.113.77"})

    async def _call():  # noqa: ANN202
        async with session_scope() as session:
            await login(req=req, session=session, request=request)

    for i in range(rl.LOGIN_FAIL_LIMIT):
        with pytest.raises(InvalidRequest):
            _LOOP.run_until_complete(_call())
    with pytest.raises(RateLimited) as ei:
        _LOOP.run_until_complete(_call())
    assert ei.value.headers.get("Retry-After", "").isdigit()
    assert "ip" not in ei.value.message.lower(), ei.value.message
    assert "account" not in ei.value.message.lower(), ei.value.message


def test_successful_login_does_not_consume_the_budget():
    """🔑 **最关键的一条**：成功登录**不**消耗配额。

    ⚠️ 如果成功也计数，一轮全量基线（三套 e2e + 三个 eval，每轮几十次登录）
    就能把本机桶填满 ⇒ 第二天早上主人被自己的基线锁在门外。
    """
    from sqlalchemy import select

    from services.api.routers.auth import login, register
    from services.common.db import session_scope, sync_session_scope
    from services.common.models import User
    from services.common.schemas import LoginReq, RegisterReq

    email = f"oklogin-{time.time():.0f}@t.local"
    pw = "pw12345678"

    async def _register():  # noqa: ANN202
        async with session_scope() as session:
            return await register(req=RegisterReq(email=email, password=pw,
                                                   display_name="ok"),
                                  session=session, request=_Req())

    _LOOP.run_until_complete(_register())
    # 🔑 先清场：上面那次 `register` **本来就该**记一次注册尝试（D135），
    #   不清的话下面那句"没有命中"就会被它污染。
    rl.reset_buckets()

    # 用一个**公网样例**的 IP 连成功登录 3 倍配额那么多次
    times = rl.LOGIN_FAIL_LIMIT * 3
    for i in range(times):
        async def _login():  # noqa: ANN202
            async with session_scope() as session:
                return await login(req=LoginReq(email=email, password=pw),
                                   session=session, request=request())

        def request() -> _Req:
            # 每次换个 IP：这样只有"成功也计数"才会被抓住
            return _Req(**{"cf-connecting-ip": f"203.0.113.{i % 250 + 1}"})

        out = _LOOP.run_until_complete(_login())
        assert out.api_key, f"第 {i + 1} 次成功登录失败"
    with sync_session_scope() as s:
        n = s.execute(select(User.id).where(User.email == email)).first()
    assert n is not None
    # 🔑 判据是"**一个命中都没有**"，而不是"桶的数量少"：
    #   `peek()` 会给 defaultdict 建出空的键，所以"桶多"与"有失败"无关
    #   （第一版写成 `len(_buckets) <= 2`，结果被 30 个空桶顶红 —— 而它想说的
    #   那件事，用 `all(not v)` 表达才准确）。
    hits = {k: v for k, v in rl._buckets.items() if v}
    assert hits == {}, f"成功登录竟然也记录了失败：{hits}"
