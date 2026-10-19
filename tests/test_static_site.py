"""🔴 D136：FastAPI 托管前端构建产物（否则外网只有 API、没有界面）。

## 三个容易做错的地方（这一组就钉它们）

1. 🔑 **挂载必须在所有路由之后** —— FastAPI 按声明顺序匹配，
   挂前面会把 `/api/v1/*` 全吃掉。
2. 🔴 **SPA 回退不能吞掉真 404**。深链（`/kb/123`）要回 `index.html`，
   但 `/api/...`、`/mcp`、`/a2a`、`/health` 打错就应该是 404。
   ⚠️ 第一版的白名单写成了 `full_path.startswith("/api/")` —— 而路径参数
   **不带前导斜杠**，于是白名单形同虚设：`GET /api/v1/nope` 返回了
   **200 + 一段 HTML**（真 bug，被这组测试当场抓住）。
3. ⚠️ **`dist` 不存在时不能挂** —— 挂一个不存在的目录会让**每个** GET 都 500，
   连 `/api/v1/health` 都活不了。正确做法：不挂 + 记一条说清先构建的日志。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from services.api.static_site import NON_SPA_PREFIXES, dist_dir, mount_frontend

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module", autouse=True)
def _own_the_async_engine():
    """🔑 进出都销毁 async 引擎单例。

    `TestClient` 用 anyio 的 portal 跑应用 —— 它会**在 portal 的 loop 上**
    建好 asyncpg 连接池。而按字母序本文件排在 `tests/test_storage.py`
    之前，于是那边的 async 测试会报
    `got Future ... attached to a different loop`（CONVENTIONS.md §9）。

    ⚠️ 只在退出时销毁**不够**：连接池已经建在 portal 的 loop 上了，
    必须**进入时**也丢掉单例，让它在本文件之后的 loop 上重建。
    """
    import asyncio

    from services.common.db import dispose_engines

    dispose_engines()
    yield

    async def _close() -> None:
        from services.common.db import get_async_engine

        await get_async_engine().dispose()

    asyncio.new_event_loop().run_until_complete(_close())
    dispose_engines()


def _has_dist() -> bool:
    return (dist_dir() / "index.html").exists()


dist_required = pytest.mark.skipif(
    not _has_dist(), reason="没有 web/dist（先 cd web && npm run build）"
)


def _client(app):  # noqa: ANN001, ANN201
    from fastapi.testclient import TestClient

    # 对端设成回环：语义与生产一致（127.0.0.1 + 可能带转发头）
    return TestClient(app, client=("127.0.0.1", 12345))


# ============================================================
# 1. 纯函数：白名单与 dist 定位
# ============================================================


def test_non_spa_prefixes_cover_everything_outside_the_ui():
    """🔑 白名单必须含 API / MCP / A2A / 探活 / 文档入口。"""
    for p in ("/api/", "/health", "/mcp", "/a2a", "/docs", "/openapi.json"):
        assert p in NON_SPA_PREFIXES, f"{p} 不在白名单里 → 打错会返回 HTML"


def test_dist_dir_points_into_the_repo():
    d = dist_dir()
    assert d.name == "dist" and d.parent.name == "web", d
    assert (ROOT / "web").resolve() == d.parent.resolve(), d


# ============================================================
# 2. dist 不存在时（不挂，且说清要先构建）
# ============================================================


def test_missing_dist_is_not_mounted_and_api_still_works(tmp_path):
    """⚠️ 没有构建产物时**不挂**静态目录 —— 否则每个 GET 都 500。

    判据是"API 仍然可用"：这是"开发机没跑 npm run build"时的常态，
    挂了空目录会让开发机上的**所有**接口一起死掉。
    """
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    app = FastAPI()

    @app.get("/api/v1/ping")
    async def _ping():  # noqa: ANN202
        return JSONResponse({"pong": True})

    assert mount_frontend(app, directory=tmp_path / "nope") is False
    c = _client(app)
    assert c.get("/api/v1/ping").json() == {"pong": True}


def test_missing_dist_logs_what_to_do(tmp_path, caplog):
    """🔑 缺构建产物时要**说清怎么办**，而不是安静地只暴露 API。"""
    from fastapi import FastAPI

    with caplog.at_level("WARNING"):
        assert mount_frontend(FastAPI(), directory=tmp_path / "nope") is False
    text = " ".join(r.message for r in caplog.records)
    assert "npm run build" in text, text


# ============================================================
# 3. 有 dist 时（真实仓库构建产物）
# ============================================================


@dist_required
def test_root_serves_the_ui_when_built():
    from services.api.main import app, _FRONTEND_MOUNTED

    assert _FRONTEND_MOUNTED is True, (
        "仓库里有 web/dist，但应用没挂上 —— 是不是挂载顺序或路径错了"
    )
    r = _client(app).get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"], r.headers["content-type"]
    assert "<div id=\"root\"" in r.text or "<script" in r.text, r.text[:200]


@dist_required
def test_deep_link_falls_back_to_index_html():
    """🔑 深链刷新不能 404 —— 那是 SPA 最经典的翻车点。"""
    from services.api.main import app

    c = _client(app)
    for path in ("/kb/123", "/settings", "/ask", "/sessions"):
        r = c.get(path)
        assert r.status_code == 200, f"{path} → {r.status_code}"
        assert "text/html" in r.headers["content-type"], path


@dist_required
def test_typo_in_api_path_is_a_real_404_not_html():
    """🔴 打错的 API 端点必须 404 + JSON，**不能**回一段 HTML。

    ⚠️ 第一版的白名单漏了前导斜杠（路径参数不带 `/`），
    于是 `GET /api/v1/nope` 返回 **200 + HTML** —— 客户端完全无法区分
    "端点不存在"和"服务返回了别的东西"。
    """
    from services.api.main import app

    c = _client(app)
    for path in ("/api/v1/nope", "/api/v1/kb/1/nope", "/api/nope"):
        r = c.get(path)
        assert r.status_code == 404, f"{path} → {r.status_code}（应是 404）"
        assert "application/json" in r.headers["content-type"], path
    # MCP / A2A 也不能被吞：打错方法或缺 Key 时是它自己的错误
    assert _client(app).get("/mcp").status_code in (401, 405)


@dist_required
def test_assets_are_cached_and_index_is_not():
    """🔑 `index.html` 不缓存（否则新构建不生效）、带哈希的资源永久缓存。"""
    from services.api.main import app

    c = _client(app)
    idx = c.get("/")
    assert "no-cache" in idx.headers.get("cache-control", ""), dict(idx.headers)

    assets = sorted((dist_dir() / "assets").glob("*.js"))
    if not assets:
        pytest.skip("assets 目录里没有 js（构建产物不完整）")
    r = c.get(f"/assets/{assets[0].name}")
    assert r.status_code == 200
    assert "immutable" in r.headers.get("cache-control", ""), dict(r.headers)


@dist_required
def test_head_is_allowed_on_deep_links():
    """⚠️ `@app.get` 不会自动补 HEAD，于是 `curl -I /kb/123` 回 405
    —— 排查时第一眼像是"路由没挂"。"""
    from services.api.main import app

    assert _client(app).head("/kb/123").status_code == 200


@dist_required
def test_api_surface_is_untouched_by_the_mount():
    """🔑 挂前端**不许**改变任何 API 行为（这是回归守卫）。"""
    from services.api.main import app

    from services.api import main

    c = _client(app)
    assert c.get("/health").json()["status"] in ("ok", "degraded")
    assert c.get("/api/v1/health").status_code == 401, "无 Key 应 401"
    assert c.get("/api/v1/kb").status_code == 401
    # ⚠️ `/docs` 与 `/openapi.json` 的**期望值是部署选择**（`EXPOSE_DOCS`，
    #   D138）⇒ 判据要看**应用对象**（`app.docs_url`），不能写死 200 ——
    #   写死的话，把开关关掉（本机的推荐配置）就会让这条"API 没被挂载改动"
    #   的守卫红掉，而它其实没坏（§5.138 第二行那一族）。
    want_docs = 200 if main.app.docs_url else 404
    assert c.get("/docs").status_code == want_docs
    assert c.get("/openapi.json").status_code == want_docs
    assert c.get("/redoc").status_code == (200 if main.app.redoc_url else 404)

# ============================================================
# 4. 打错前缀 / 打错方法（D147）
# ============================================================


@dist_required
def test_write_to_a_typo_prefixed_path_is_404_not_405():
    """🔑 `POST /api/v1/mcp` 与 `POST /api/v1/a2a/tasks` 要回 **404**。

    🔴 D147 修的就是这个：catch-all 只挂 `GET/HEAD` 时，Starlette 在
    **进 handler 之前**就判成 405 —— 而 405 的字面意思是"路径存在、方法不对"，
    于是排查的人会去改 HTTP 方法，而真正要改的是**路径**
    （MCP/A2A 的真身在**根路径**，契约里写的就是 `/mcp`）。
    """
    from fastapi import FastAPI

    from services.api.routers import a2a, mcp

    app = FastAPI()
    # ⚠️ 只挂这两个：它们在**根路径**（D9/D10 的对外契约），
    #   正是"以为在 /api/v1 下"最容易被写错的那两个。
    app.include_router(mcp.router, prefix="")
    app.include_router(a2a.router, prefix="")
    assert mount_frontend(app) is True
    c = _client(app)

    for path in ("/api/v1/mcp", "/api/v1/a2a/tasks", "/api/v1/agent-card.json"):
        r = c.post(path, json={})
        assert r.status_code == 404, f"{path} → {r.status_code}（应为 404）"
        # 🔑 而且必须是**项目的错误信封** + JSON，不是裸的 {"detail": …}
        assert r.json()["error"]["code"] == "NOT_FOUND", r.text[:200]
        assert path in r.json()["error"]["message"], r.text[:200]


@dist_required
def test_write_to_an_unknown_ui_path_does_not_get_html():
    """🔑 写方法打**完全不存在的路径**也不能回 `index.html`。

    ⚠️ 这是本条最要紧的一半：SPA 回退的"善意"是给**浏览器导航**用的；
    一个 POST 拿到 200 + HTML 时，客户端会去解析 HTML 当结果，
    症状是"接口返回了奇怪的东西"而不是"路径打错了"。
    """
    from fastapi import FastAPI

    app = FastAPI()
    assert mount_frontend(app) is True
    c = _client(app)

    for method in ("POST", "PUT", "PATCH", "DELETE"):
        r = c.request(method, "/kb/123/ask")
        assert r.status_code == 404, f"{method} /kb/123/ask → {r.status_code}"
        assert r.json()["error"]["code"] == "NOT_FOUND", r.text[:200]


@dist_required
def test_known_path_with_wrong_method_is_also_404_and_that_is_deliberate():
    """⚠️ **有意的取舍**：已知路径用错方法也回 404（不是 405）。

    为什么不保留 405：让 catch-all 覆盖全部方法才能把"打错前缀"变成 404，
    而 Starlette 的匹配规则是"路径匹配但方法不匹配 ⇒ 记一个 partial，
    后面若出现**完整**匹配就用它" ⇒ catch-all 变成完整匹配后，
    真路由的 405 会被一起吞掉。想保留就得自己重写"按路径找路由"，
    那是把框架内部实现抄进项目。

    这个取舍的代价：调用方用错方法时拿到的是 404 而不是 405，
    少一个"路径存在、方法不对"的提示。**可接受**：契约里写明了方法，
    而 405 在"有 SPA 回退"的服务里几乎总是意味着"路径打错"
    —— 换句话说，它本来就在误导人。

    ⚠️ 判据钉的是**行为**（404 + JSON 信封），不是"handler 里的某一行"，
    所以将来有人改回只挂 GET/HEAD 时这条会红 —— 那正是要提醒他的事。
    """
    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/only-get")
    async def _only_get():  # noqa: ANN202
        return {"ok": True}

    assert mount_frontend(app) is True
    c = _client(app)
    r = c.post("/only-get")
    assert r.status_code == 404, r.status_code
    assert r.json()["error"]["code"] == "NOT_FOUND", r.text[:200]
    # 🔑 真的 GET 它仍然正常（别为了 404 把真路由弄坏了）
    assert c.get("/only-get").json() == {"ok": True}


@dist_required
def test_get_deep_link_still_serves_the_spa():
    """🔑 回归：GET 的深链**仍然**回 `index.html`（D136 的原意不能丢）。"""
    from fastapi import FastAPI

    app = FastAPI()
    assert mount_frontend(app) is True
    c = _client(app)
    for path in ("/", "/kb/123", "/settings/profile", "/audit"):
        r = c.get(path)
        assert r.status_code == 200, f"{path} → {r.status_code}"
        assert "text/html" in r.headers["content-type"], path
    # 🔑 而"打错的 API 前缀"仍然 404（GET 也要）
    assert c.get("/api/v1/definitely-not-a-route").status_code == 404


@dist_required
def test_real_app_keeps_its_routes_reachable_next_to_the_catch_all():
    """🔑 在**真 app** 上验证：catch-all 注册在最后，真路由仍然可达。

    ⚠️ 前一条测试自己造 `FastAPI()` 再显式调 `mount_frontend` —— 那样的
    "顺序正确"是**测试自己写死的**，验证不了 `main.py` 里的真实注册顺序。
    变异脚本立刻证明了这点：把挂载提前到路由之前，那条测试**照样全绿**
    （它压根不 import 那个 app）。⇒ 这条必须打真 app。

    ⚠️ 判据只能用**不碰 DB 的真路由**：本文件其它测试用 `TestClient`
    逐次调用（每次一个新 event loop），一旦某条请求把 asyncpg 池建在那个
    loop 上，后面本文件里别的 DB 请求就会撞上 §9 的"attached to a different loop"。
    `CurrentUser` 在**读不到 Authorization 头**时就抛（还没查库）⇒
    `/mcp`、`/.well-known/agent-card.json` 不带 Key 就是 DB-free 的真路由。

    ⚠️ 第一版只判状态码，变异又绿了：注入的 catch-all 返回的是
    **200 + `{"error":{"code":"NOT_FOUND"}}`**，状态码照样是 200。
    ⇒ 判据必须落在**响应体**上（这与 §5.114"判据要指向性质"同源：
    这里要验的是"真路由被调用了"，不是"回了个 200"）。
    """
    from services.api.main import _FRONTEND_MOUNTED
    from services.api.main import app as real_app

    assert _FRONTEND_MOUNTED is True, (
        "真 app 没挂上前端（web/dist 缺？）—— 这条测的是挂载后的路由顺序，"
        "跳过它等于什么都没验"
    )
    c = _client(real_app)

    # ① 真 GET 路由没被遮蔽：不带 Key ⇒ 401 INVALID_TOKEN（DB-free）。
    #    被 catch-all 吞掉的话会变成 200 + NOT_FOUND body。
    r = c.get("/.well-known/agent-card.json")
    assert r.status_code == 401, f"card 路由被遮蔽了？{r.status_code} {r.text[:120]}"
    assert r.json()["error"]["code"] == "INVALID_TOKEN", r.text[:200]

    # ② 真 POST 路由没被遮蔽：同样 401（`/mcp` 是对外契约，POST 才有）
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401, f"mcp 路由被遮蔽了？{r.status_code} {r.text[:120]}"
    assert r.json()["error"]["code"] == "INVALID_TOKEN", r.text[:200]

    # ③ 打错前缀 → 404（D147）；打错方法 → 404（有意取舍，见上面那条）
    assert c.post("/api/v1/mcp").status_code == 404
    assert c.post("/api/v1/a2a/tasks").status_code == 404
    assert c.post("/.well-known/agent-card.json").status_code == 404

    # ④ 深链仍是 HTML（catch-all 自己的本职）
    r = c.get("/kb/123")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
