"""🔴 D158：视觉查询服务（读侧）的防回归 —— 协议、降级、隔离。

视觉腿之前是"只写不读"：`kb_visual` 有活点，检索时那条腿在 API 进程
恒 `skipped`。D158 把读侧接进来，方式是一个**只做编码**的旁路进程
（`services/visual_service/main.py`），而这份测试锁的是**接线与边界**，
不是检索质量（那要真跑 colSmol，见 `deploy/verify_visual_leg.py`）。

## 这一组锁的六件事

| # | 锁什么 | 判据指向 |
|---|---|---|
| 1 | **编码**可以走远端，**租户过滤与 Qdrant 查询不能** | AST：三个步骤的归属 + 实参数据流 |
| 2 | 远端挂 / 503 / 超时 ⇒ `skipped`，**且带可操作的原因** | 真调 `search_visual` |
| 3 | §4.1 仍成立：API 进程 `get_visual_model()` **仍抛错** | 真调一次 |
| 4 | `available_legs()` **不硬编码** | AST + 双向真跑 |
| 5 | 协议形状与"**没有 `images` 字段**" | 纯函数 + 请求模型 |
| 6 | 跨租户仍只有**一份**过滤逻辑 | 唯一入口 + 禁裸 filter 字面量 |

⚠️ **本文档里最该被记住的一条**（第 1 条）：视觉服务的价值在于"让 API 进程
能编码"；如果顺手把 `build_qdrant_filter` 或 Qdrant 查询也搬过去，
就多了一份租户过滤逻辑，而两份迟早漂移，漂移的方向是**越权**。

## 为什么测试函数是**同步的**

与 `tests/test_retrieval_e2e.py` 同一条理由（PROGRESS §5.17）：模块级单例
（httpx client、探活缓存）绑在**创建它的事件循环**上，而 anyio 的 pytest 插件
**每个测试函数一个新 loop**。

⚠️ `pytest.ini` 里的 `anyio_mode = "auto"` **当前是死配置** ——
   那个文件用的节名是 `[tool.pytest.ini_options]`（`pyproject.toml` 的写法），
   而 `pytest.ini` 只读 `[pytest]` 节 ⇒ 整份配置都没生效。
   所以别写 `async def test_...`（会被报 "async functions are not natively
   supported"），也别以为"改了 `pytest.ini` 就生效"。
   详见 `PROGRESS.md` §5.160（**没顺手修**：修了会让所有 async 测试
   突然被 anyio 接管，属另一个决定）。
"""

from __future__ import annotations

import ast
import asyncio
import re
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
VISUAL_PY = ROOT / "services" / "retrieval" / "visual.py"
POOL = ROOT / "services" / "retrieval" / "pool.py"
SVC = ROOT / "services" / "visual_service" / "main.py"
ENGINE = ROOT / "services" / "retrieval" / "engine.py"

_loop = asyncio.new_event_loop()


def run(coro: Coroutine[Any, Any, Any]) -> Any:
    """在**模块级共享**的 loop 上跑一个 coroutine。"""
    return _loop.run_until_complete(coro)


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _code_only(path: Path) -> str:
    """剥掉 docstring，只留会被执行的代码。

    ⚠️ 这一组好几条判据是"某段代码不许出现"，而这些模块的 docstring 里
    **必然**要写清"第一版错在哪"（里面就有 `images=`、`build_qdrant_filter`、
    "不查 Qdrant"）。文本匹配的话，解释原因的注释会把测试自己告发
    （`tests/test_retrieval_filters.py` 已踩过，§5.149b）。
    """
    tree = ast.parse(_src(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree))


def _fn(path: Path, name: str) -> ast.AST:
    """按名字取一个函数的 AST 节点（取不到直接失败，不静默跳过）。"""
    tree = ast.parse(_src(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    raise AssertionError(f"{path.name} 里没有函数 {name}（守卫要跟着改）")


def _called_names(fn: ast.AST) -> set[str]:
    """函数体内**被调用的裸名**（`obj.m()` 不算 —— 接收者类型静态推不出来）。

    ⚠️ **也收集"作为参数传出去的裸名"**：`anyio.to_thread.run_sync(_query_sync, …)`
    里 `_query_sync` 是**实参**而不是 `Call.func`，只判 `Call.func` 会漏掉它 ——
    而 `_query_sync` 正是"MaxSim 查询在本进程"这件事的证据。
    """
    out: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            out.add(node.func.id)
        elif isinstance(node, ast.Name):
            out.add(node.id)
    return out


def _referenced_attrs(fn: ast.AST) -> set[str]:
    return {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}


# ============================================================
# 1. 编码走远端；租户过滤与 Qdrant 查询留在本地（架构边界）
# ============================================================


def test_search_visual_never_builds_the_filter_in_the_remote_process():
    """🔑 `search_visual` 必须**自己**调 `build_qdrant_filter`。

    反面（第一版会写成的样子）：把 `filter` 当参数传给一个"远端查询"函数。
    那样过滤逻辑就跟着搬到了新进程 ⇒ §0.2 的"唯一入口"变成两个，
    而漂移的方向是**越权看到别人的照片**（§0.6）。

    ⚠️ 判据指向**属性名**（`build_qdrant_filter` 这个标识符）而不是某一行：
    钉 `build_qdrant_filter(ctx, kb_id, kb_ids=kb_ids)` 的逐字形状的话，
    改个参数名就红，而行为没变（§5.151c）。
    """
    fn = _fn(VISUAL_PY, "search_visual")
    called = _called_names(fn) | _referenced_attrs(fn)
    assert "build_qdrant_filter" in called, (
        "search_visual 没在本进程构造租户过滤 —— 过滤被搬到远端去了，"
        "那是 §0.2 说的'第二份过滤逻辑'（漂移方向 = 越权）"
    )


def test_visual_service_never_touches_qdrant_or_auth():
    """🔑 视觉服务**不查 Qdrant、不碰租户**，它只编码。

    🔴 这是本任务最容易"顺手多做一点"的地方：服务已经有一个 HTTP 入口、
    一个配置对象，而 Qdrant 客户端就在**同一棵依赖树**里 ⇒ 搬过去只差几行。
    而那几行就是"第二份租户过滤"的开始。

    判据扫**整个模块的可执行代码**：`collections` / `get_client` 这些
    Qdrant REST 入口的名字、以及 `build_qdrant_filter` / `UserContext`
    （租户上下文的唯一类型）都不许出现。
    ⚠️ 用 `_code_only`：模块 docstring 里**故意**解释了这些名字
    （"不查 Qdrant、不做租户过滤"）—— 那是说明，不是主张。
    """
    code = _code_only(SVC)
    for banned in (
        "build_qdrant_filter",
        "build_kb_scope_sql",
        "UserContext",
        "readable_kb_ids",
        "get_client",
        # ⚠️ 判 Qdrant 的 REST 路径 `/collections/…` 而不是裸词 `collections` ——
        #   裸词会撞上 `from collections import deque`，而那不是 Qdrant
        #   （我第一版就写成裸词，第一跑就红在这）。
        '"/collections',
        "'/collections",
        "upsert_points",
        "search_points",
    ):
        assert banned not in code, (
            f"视觉服务里出现了 {banned} —— 它只该做编码（CONVENTIONS §0.2 / D158）"
        )


def test_search_visual_still_filters_and_queries_in_this_process():
    """🔑 ①编码 → ②租户过滤 → ③Qdrant MaxSim 三个步骤**都在本进程**。

    ⚠️ 为什么顺序也重要：过滤必须在查询**参数**里（`filter`），
    而不能是"查回来再过滤"。后者在跨租户时会把别人的 payload 带进进程 ——
    哪怕不返回给用户，也已经是一次数据泄漏。
    """
    called = _called_names(_fn(VISUAL_PY, "search_visual"))
    assert "build_qdrant_filter" in called
    assert "_query_sync" in called, "search_visual 没在本进程查 Qdrant"


def _string_constants_from(tree: ast.AST) -> set[str]:
    """模块里所有**字符串常量**（与引号风格无关）。"""
    return {
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def _string_constants(path: Path) -> set[str]:
    return _string_constants_from(ast.parse(_src(path)))


def _filter_var_names(fn: ast.AST) -> set[str]:
    """哪些变量是**直接**由 `build_qdrant_filter(...)` 赋出来的。

    🔑 抽成纯函数才能被合成输入测：判据写在测试体里时，
    变异掉那段推导会全绿（§5.151c：判据要指向性质，不是中间状态）。
    """
    out: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign) or node.value is None:
            continue
        from_filter = any(
            isinstance(n, ast.Call) and getattr(n.func, "id", "") == "build_qdrant_filter"
            for n in ast.walk(node.value)
        )
        if from_filter:
            out |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    return out


def _passed_names(fn: ast.AST) -> set[set[str]]:
    """每个调用**实参**里出现的裸名（一个调用一个 `frozenset`）。

    ⚠️ `_query_sync` 是作为 `anyio.to_thread.run_sync` 的**实参**出现的，
    不是 `Call.func` —— 只扫 `Call.func` 会报"没调用"（我第一版就这样，
    第一跑就红）。所以这里扫**所有**调用的实参。
    """
    # ⚠️ 返回 `frozenset` 而不是 `set`：外层是 `set[set[str]]`，
    #   `set` 不可 hash（我第一版写成 `list` 然后又写成 `set`，两次都红在这）。
    return frozenset(
        frozenset(
            a.id for arg in node.args for a in ast.walk(arg)
            if isinstance(a, ast.Name)
        )
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
    )


def test_qdrant_query_is_given_the_tenant_filter():
    """🔑 算出的过滤 `flt` **必须**就是传给 `_query_sync` 的那个。

    ⚠️ 这是上一条的**接线守卫**：上一条只判"两个函数都被调用了"，
    变异成 `_query_sync(vecs, None, limit)` 时照样全绿 ——
    而那等于**查了别人的照片再在下游扔掉**（跨租户时是真泄漏）。
    ⇒ 判据指向**数据流**：那个变量既要从过滤器来，又要被传进查询。
    """
    fn = _fn(VISUAL_PY, "search_visual")
    flt_vars = _filter_var_names(fn)
    assert flt_vars, (
        "没有哪个变量是由 build_qdrant_filter 算出来的 —— "
        "过滤要么被跳过了，要么被内联进别的调用"
    )
    assert any(flt_vars & argnames for argnames in _passed_names(fn)), (
        f"由 build_qdrant_filter 算出来的变量 {sorted(flt_vars)} "
        "没有传给任何调用 —— MaxSim 会查回全库的点再在下游丢弃"
        "（= 跨租户泄漏）"
    )
    assert any("_query_sync" in argnames and flt_vars & argnames
               for argnames in _passed_names(fn)), (
        "_query_sync 收到的不是那个过滤器变量（= 跨租户泄漏）"
    )


def test_filter_var_detector_finds_nothing_when_filter_is_never_called():
    """🔑 "过滤器变量探测"本身必须能判"没有"（合成输入，§5.151c）。"""
    src = ast.parse(
        "def f(ctx):\n"
        "    flt = build_qdrant_filter(ctx, 1)\n"
        "    return _query_sync(None)\n"
    )
    assert _filter_var_names(src.body[0]) == {"flt"}
    src2 = ast.parse("def g(ctx):\n    return None\n")
    assert _filter_var_names(src2.body[0]) == set()


def test_encode_vdr_query_local_is_untouched():
    """🔑 worker 的**写**路径必须继续用本地 `encode_images`。

    ⚠️ "两个编码入口合成一个"看起来是去重，但写侧要编码**图片**（15s/图），
    走 HTTP 会让摄取依赖一个"查得到性"的旁路服务 ——
    视觉服务一挂，**摄取**就停，而它本该与查询无关。
    """
    handlers = _src(ROOT / "services" / "worker" / "handlers" / "__init__.py")
    assert "encode_images" in handlers, (
        "worker 的视觉 handler 不再用 encode_images —— 写侧不该改成 HTTP"
    )
    assert "encode_vdr_query_remote" not in handlers, (
        "worker 的写侧用了远端编码 —— 视觉服务一挂摄取就停"
    )


# ============================================================
# 2. 降级：远端任何异常 ⇒ skipped，绝不抛出去
# ============================================================


class _Ctx:
    """够用的 `UserContext` 替身。只提供 `build_qdrant_filter` 读的那几个字段。"""

    user_id = 1
    readable_kb_ids = (4249,)
    workspace_id = 1


class _Resp:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _FakeClient:
    """假 `httpx.AsyncClient`：`get` 探活、`post` 编码。**记录调用次数**。"""

    def __init__(self, *, health: _Resp | None = None,
                 encode: _Resp | None = None,
                 raise_on: str | None = None) -> None:
        self.health = health if health is not None else _Resp(
            200, {"model_loaded": True, "pid": 1}
        )
        self.encode = encode if encode is not None else _Resp(
            200, {"vectors": [[0.0] * 8] * 3, "n_tokens": 3}
        )
        self.raise_on = raise_on
        self.get_calls = 0
        self.post_calls = 0

    async def get(self, path: str, **kw: Any) -> _Resp:  # noqa: ANN401
        self.get_calls += 1
        if self.raise_on == "health":
            raise ConnectionError("refused")
        return self.health

    async def post(self, path: str, **kw: Any) -> _Resp:  # noqa: ANN401
        self.post_calls += 1
        if self.raise_on == "encode":
            raise ConnectionError("refused")
        if self.raise_on == "post_503":
            return _Resp(503, {"code": "VISUAL_UNAVAILABLE", "detail": "模型未加载完"})
        return self.encode


@pytest.fixture
def fake_visual(monkeypatch):  # noqa: ANN001, ANN201
    """把 `pool` 的 HTTP 层换成假的，并**清掉探活缓存**。

    🔑 清缓存不是可选的：`visual_service_health` 有 5s TTL（刻意如此），
    而它是**模块级单例**、活过整个测试会话 —— 不清的话上一个用例的
    "服务不可达"会被下一个用例读成缓存值，症状是"测试之间互相污染，
    单跑就绿"（最难查的一类）。
    """
    from services.retrieval import pool

    def install(client: _FakeClient) -> _FakeClient:
        pool.invalidate_visual_probe()
        monkeypatch.setattr(pool, "_get_async_client", lambda: client)
        monkeypatch.setattr(pool, "visual_enabled", lambda: False)
        return client

    yield install
    pool.invalidate_visual_probe()


def test_search_visual_degrades_to_skipped_when_service_is_down(fake_visual):
    """🔑 判据 3：**视觉服务停掉时检索仍 200**，视觉腿 `skipped` 且原因可操作。

    "可操作"的判据是**文案里必须出现怎么修**（起它的命令）——
    只说"失败"的话，下一个人会去查 Qdrant / colSmol / 索引，
    而真正的原因只是"那个进程没起"（§5.93/§5.94 那一族）。

    🔑 这里断言的是"**没有异常抛出去**"（函数正常返回 `LegResult`）：
    抛出去的话 `engine._gather_legs` 会让整个 `search()` 500，
    那正是判据 3 要防的。真打一次 HTTP 的判据在 `eval/api_1d_check.py`。
    """
    fake_visual(_FakeClient(raise_on="health"))

    from services.retrieval.visual import search_visual

    res = run(search_visual(_Ctx(), "红色的那张照片", kb_id=4249))  # type: ignore[arg-type]
    assert res.skipped is not None, "服务不可达却没降级"
    assert "dev.sh start" in res.skipped, (
        f"降级原因不可操作（没说要怎么修）：{res.skipped}"
    )
    assert "http://127.0.0.1:8020" in res.skipped, (
        f"降级原因没说是哪个服务：{res.skipped}"
    )


def test_search_visual_degrades_on_503(fake_visual):
    """🔑 判据 4：视觉服务返回 503 ⇒ 同样是 `skipped`，**不是 500**。

    ⚠️ 探活说"模型已就绪"但 `/encode` 回 503 是一条真实路径
    （模型加载失败后 `model_loaded` 仍是 true，或两者之间它挂了）。
    所以这条路径必须有测试 —— 只测"探活失败"的话，
    变异成"`/encode` 的 503 直接往上抛"会**全绿**。
    """
    fake_visual(_FakeClient(raise_on="post_503"))

    from services.retrieval.visual import search_visual

    res = run(search_visual(_Ctx(), "照片", kb_id=4249))  # type: ignore[arg-type]
    assert res.skipped is not None, "503 没有降级（会变成 500）"
    assert "503" in res.skipped, f"降级原因里没有状态码：{res.skipped}"
    assert "模型" in res.skipped, (
        f"503 的文案没说是模型没就绪：{res.skipped} —— "
        "运维会去看 Qdrant，而 Qdrant 没问题"
    )


def test_search_visual_degrades_on_timeout(monkeypatch):  # noqa: ANN001, ANN201
    """🔑 超时也降级，且文案说清**预算**是多少。

    ⚠️ 判据指向"文案里那个超时预算数字 = 配置值"：写死成别的数字时，
    运维会去调一个不存在的旋钮（配置改了但文案不变，比没有文案更坏）。

    ⚠️ 这里**不**走 `encode_vdr_query_remote` 的真超时路径（那要 2s 挂起），
    而是喂一个已经翻译好的 `VisualServiceError` ——
    真超时那一侧的翻译由下面 `test_remote_encode_*` 两条覆盖。
    """
    from services.common.config import get_settings
    from services.retrieval import pool
    from services.retrieval.pool import VisualServiceError

    budget = get_settings().visual_service_timeout_s

    async def _boom(*a: Any, **k: Any) -> Any:  # noqa: ANN401
        raise VisualServiceError(
            f"视觉服务 {get_settings().visual_service_url} 超时（预算 {budget:.1f}s）",
            hint="它在排队/编码上花太久了；先看它自己的负载。",
        )

    monkeypatch.setattr(pool, "encode_vdr_query_remote", _boom)
    from services.retrieval import visual as vmod

    pool.invalidate_visual_probe()
    monkeypatch.setattr(pool, "visual_enabled", lambda: False)
    monkeypatch.setattr(pool, "_get_async_client", lambda: _FakeClient())
    monkeypatch.setattr(vmod, "_query_sync", lambda vecs, flt, limit: [])

    res = run(vmod.search_visual(_Ctx(), "照片", kb_id=4249))  # type: ignore[arg-type]
    assert res.skipped is not None, "超时没有降级"
    assert str(budget) in res.skipped, (
        f"降级原因里的超时预算 {budget}s 与配置不符：{res.skipped}"
    )


def test_search_visual_succeeds_via_remote(fake_visual, monkeypatch):  # noqa: ANN001, ANN201
    """🔑 判据 2（正向）：远端可用时 `skipped is None` 且有命中。

    ⚠️ 顺带钉住"命中带了 `visual:` 命名空间前缀"（PROGRESS §5.10）：
    `kb_visual` 的页级点与 `kb_dense` 的 chunk 点 ID 空间独立，
    不加前缀会让"文本 chunk 3"和"扫描页 3"在 RRF 里互相抵消。
    """
    import services.retrieval.visual as vmod

    fake_visual(_FakeClient())
    monkeypatch.setattr(
        vmod, "_query_sync",
        lambda vecs, flt, limit: [
            {"id": 12345, "score": 3.5, "payload": {
                "doc_id": 7, "page": 2, "file_name": "a.jpg",
                "text_raw": "红色的那张", "source_kind": "photo",
            }}
        ],
    )

    res = run(vmod.search_visual(_Ctx(), "照片", kb_id=4249))  # type: ignore[arg-type]
    assert res.skipped is None, f"远端可用却降级了：{res.skipped}"
    assert len(res.hits) == 1, "视觉腿没召回任何东西"
    hit = res.hits[0]
    assert hit.key.startswith("visual:"), f"命名空间前缀丢了：{hit.key}"
    assert hit.chunk_index == 2, "页码没进 chunk_index（前端画不出缩略图）"


def test_search_visual_uses_local_encoder_when_this_process_enabled_it(
    fake_visual, monkeypatch  # noqa: ANN001, ANN201
):
    """🔑 本进程 `enable_visual(True)` 时**不发 HTTP**，直接本地编码。

    🔑 这是 worker 侧那条路径（`CONVENTIONS.md` §4.1），也是
    "API 进程不许加载 colSmol" 那条守卫的另一半。
    ⚠️ 判据是"**一次 HTTP 都没发**"：变异成"永远走远端"时，
    worker 在没有视觉服务的机器上会退化成恒 skipped —— 而它明明能本地编码。
    """
    import services.retrieval.visual as vmod
    from services.retrieval import pool

    client = fake_visual(_FakeClient())
    seen: list[str] = []

    monkeypatch.setattr(pool, "visual_enabled", lambda: True)
    monkeypatch.setattr(
        pool, "encode_vdr_query",
        lambda text: (seen.append(text), [[0.0] * 4])[1],
    )
    monkeypatch.setattr(vmod, "_query_sync", lambda vecs, flt, limit: [])

    res = run(vmod.search_visual(_Ctx(), "照片", kb_id=4249))  # type: ignore[arg-type]
    assert res.skipped is None, f"本地编码却降级了：{res.skipped}"
    assert seen == ["照片"], f"没走本地 encode_vdr_query：{seen}"
    assert client.get_calls == 0 and client.post_calls == 0, (
        "本地编码却发了 HTTP —— 不该有任何远端调用"
    )


def test_visual_service_error_carries_an_actionable_hint():
    """🔑 `VisualServiceError` 必须带 `reason` **和** `hint` 两段。

    ⚠️ 判据指向**两个字段都非空**：只判 `str(exc)` 非空的话，
    变异成 `VisualServiceError(f"{reason}")`（丢掉 hint）会全绿，
    而那正是 `/search/debug` 的 `skipped` 变得不可操作的那一步。
    """
    from services.retrieval.pool import VisualServiceError

    exc = VisualServiceError("视觉服务不可达", hint="起它：bash deploy/dev.sh start")
    assert exc.reason
    assert exc.hint
    assert "dev.sh start" in exc.hint
    # 两个字段都要进 str —— 否则日志里只有一半
    assert "视觉服务不可达" in str(exc) and "dev.sh start" in str(exc)


def _with_fake_client(monkeypatch, client):  # noqa: ANN001, ANN201
    from services.retrieval import pool

    pool.invalidate_visual_probe()
    monkeypatch.setattr(pool, "_get_async_client", lambda: client)
    return pool


def test_remote_encode_translates_transport_errors_into_visual_service_error(
    monkeypatch,  # noqa: ANN001, ANN201
):
    """🔑 `encode_vdr_query_remote` 把传输层异常**翻译**成带指引的那一种。

    ⚠️ 不是"包一层就行"：判据指向 `VisualServiceError` 这个**类型** ——
    原样抛 `httpx.ConnectError` 时 `visual.py` 的 `except VisualServiceError`
    接不住，会落到通用 `except Exception` 分支，
    `skipped` 里就只有 `ConnectError` 而**没有修复指引**。
    """
    from services.retrieval.pool import VisualServiceError

    pool = _with_fake_client(monkeypatch, _FakeClient(raise_on="encode"))
    with pytest.raises(VisualServiceError) as ei:
        run(pool.encode_vdr_query_remote("照片"))
    assert "dev.sh start" in ei.value.hint


def test_remote_encode_surfaces_a_503_as_visual_service_error(monkeypatch):  # noqa: ANN001, ANN201
    """🔑 503 也翻译成 `VisualServiceError`（且文案带"模型"两个字）。

    ⚠️ 判据指向**文案**：503 与"连不上"的处置完全不同
    （前者等服务就绪，后者去起进程），混成一句话会让排障走错方向。
    """
    from services.retrieval.pool import VisualServiceError

    pool = _with_fake_client(monkeypatch, _FakeClient(raise_on="post_503"))
    with pytest.raises(VisualServiceError) as ei:
        run(pool.encode_vdr_query_remote("照片"))
    assert "503" in ei.value.reason
    assert "模型" in ei.value.hint


def test_remote_encode_rejects_a_response_without_vectors(monkeypatch):  # noqa: ANN001, ANN201
    """🔑 远端返回 200 但**没有 `vectors`** ⇒ 也要降级，不能让它 500。

    ⚠️ 这是"半坏"的典型：服务起来了、状态码 200、协议却不对
    （比如中间有个代理把 JSON 换成了别的）。
    判据指向"抛 `VisualServiceError`"而不是"返回空"——
    返回空的话 `kb_visual` 的 MaxSim 会拿空查询去查（400，或更糟：全库）。
    """
    from services.retrieval.pool import VisualServiceError

    pool = _with_fake_client(monkeypatch, _FakeClient(encode=_Resp(200, {"ok": True})))
    with pytest.raises(VisualServiceError):
        run(pool.encode_vdr_query_remote("照片"))


def test_health_probe_result_is_cached(fake_visual):
    """🔑 探活结果**必须缓存**（TTL 5s），否则每次检索都多一次 RTT。

    ⚠️ 更要紧的是"服务没起"那条：那时每次探测都要等 connect 超时
    （本机收紧到 1s）⇒ 不缓存的话**每次检索都白等 1s**，
    而它本来可以立刻降级。

    ⚠️ 判据指向**HTTP 调了几次**，而不是"返回值对不对" ——
    后者在"缓存了"与"没缓存"两种实现下都是 `False`（§5.151c）。
    """
    from services.retrieval import pool

    client = fake_visual(_FakeClient(raise_on="health"))
    for _ in range(3):
        assert run(pool.visual_service_health())["reachable"] is False
    assert client.get_calls == 1, (
        f"探活发了 {client.get_calls} 次 HTTP（应缓存成 1 次）—— "
        "服务没起时每次检索都要白等一次连接超时"
    )


def test_probe_cache_can_be_invalidated(fake_visual):
    """🔑 缓存必须**能被清掉**（测试与"服务刚重启"场景需要）。

    ⚠️ 判据是**第二次探活真的又发了 HTTP**（计数从 1 变 2）：
    只判"缓存字段变 None"的话，变异成"`invalidate` 把自己也记进缓存"会全绿。
    """
    from services.retrieval import pool

    client = fake_visual(_FakeClient())
    run(pool.visual_service_health())
    assert pool._probe_cache["value"] is not None

    pool.invalidate_visual_probe()
    assert pool._probe_cache["value"] is None
    run(pool.visual_service_health())
    assert client.get_calls == 2, "invalidate 没生效（第二次探活没真的发 HTTP）"


# ============================================================
# 3. §4.1 仍然成立：API 进程不许加载 colSmol
# ============================================================


def test_api_process_still_refuses_to_load_visual_model():
    """🔑 判据 6：`CONVENTIONS.md` §4.1 **一个字都没改**。

    API 进程（没调 `enable_visual(True)`）里 `get_visual_model()` 必须**抛错**。

    🔴 这条最容易在"接通读侧"时被顺手删掉：改的人看到"现在有独立服务了，
    这个守卫就没用了" ⇒ 删掉之后下一次重构就会在 API 进程里加载 colSmol，
    而症状是"提问时 API 偶尔卡十几秒"（不会报错，只会慢）。
    判据是真**调一次**并看它抛，不是检查源码里有没有那句 `raise`。
    """
    from services.retrieval import pool

    pool.enable_visual(False)
    with pytest.raises(RuntimeError) as ei:
        pool.get_visual_model()
    msg = str(ei.value)
    # 报错要**指得到**该去哪儿修（D124 的教训：报错完全指不到真正的原因）
    assert "视觉" in msg, f"报错没说是模型没启用：{msg}"
    assert "worker" in msg or "视觉查询服务" in msg, (
        f"报错没指出该由哪个进程做编码：{msg}"
    )


def test_visual_service_enables_visual_for_itself():
    """🔑 视觉服务**自己**必须 `enable_visual(True)`，否则 `/encode` 全 503。

    ⚠️ 这条与上一条是一对：一个是"不许开"、一个是"必须开"，
    而它们之间**没有任何自动检查** —— `enable_visual` 刻意不判断进程身份
    （见 `pool.py` 的 docstring）。所以两端都得有测试。
    判据扫源码形状（lifespan 里那一句），因为真跑要加载 2.65GB。
    """
    assert "enable_visual(True)" in _code_only(SVC), (
        "视觉服务没调 enable_visual(True) —— get_visual_model() 会抛错，"
        "于是每个 /encode 都是 503（症状：视觉腿恒 skipped，"
        "而 /health 的 model_loaded 是 false）"
    )


def test_visual_service_warms_the_model_at_startup():
    """🔑 视觉服务必须在启动时**预热一次真实编码**。

    🔑 这不是优化，是**正确性**：模型懒加载 12.8~15.0s，而客户端超时只有 2s
    ⇒ 不预热的话第一个用户的查询必然降级，且用户看到的是
    "视觉腿时好时坏"（最难解释的那种症状）。
    ⚠️ 判据指向"**真的调了编码函数**"，不是"有个叫 preload 的东西"。
    """
    code = _code_only(SVC)
    assert "get_visual_model(" in code, "视觉服务没有触发模型加载"
    # ⚠️ 判 AST 形状而不是逐字 `'encode_vdr_query("预热")'`：
    #   `_code_only` 走的是 `ast.unparse`，它会把双引号规范成单引号 ——
    #   钉住引号风格的话，改一次排版就红（而行为没变）。
    fn = _fn(SVC, "lifespan")
    warmed = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "encode_vdr_query"
    ]
    assert warmed, (
        "视觉服务只加载了模型、没跑一次真实前向 —— "
        "第一个用户会撞上 13s 加载（而超时预算是 2s）"
    )


def test_visual_service_does_not_touch_the_text_model_pool():
    """🔑 视觉服务**不预热文本模型**（bge-small 与它无关，各 100MB / 2.65GB）。

    ⚠️ 判据指向"没有调 `preload_core`"：调了不会报错，只是白占内存 + ~3s 启动，
    而这种"没人会发现的浪费"正是 §5.155 那一族。
    """
    assert "preload_core" not in _code_only(SVC), (
        "视觉服务预热了文本模型（bge-small）—— 它只需要 colSmol"
    )


# ============================================================
# 4. 协议
# ============================================================


def test_encode_request_has_text_and_deliberately_no_images():
    """🔑 协议里**没有 `images` 字段**。

    🔴 D124 实测：`images=[[]]` 在 transformers 5.18 抛
    `AttributeError: 'list' object has no attribute 'device'`。
    把它做成**协议里根本不存在的字段**（而不是"收下再报错"），
    少一个能走错的路。
    """
    from services.visual_service.main import EncodeReq

    assert "text" in EncodeReq.model_fields
    assert "images" not in EncodeReq.model_fields, (
        "EncodeReq 又有了 images —— colSmol 的图片编码不在这个服务里"
    )


def test_encode_rejects_empty_text():
    """🔑 `text` 为空 ⇒ **400**（调用方的错），不是 503。

    🔑 分清 400 与 503 的意义：400 是"你的请求不对"，503 是"我这边的问题"。
    空查询算前者。判据是真的**调一次**端点。
    """
    from services.visual_service import main as mod

    # 强行让模型"已就绪"，只为走到参数校验之后那一步
    mod._state["model_loaded"] = True
    try:
        resp = run(mod.encode(mod.EncodeReq(text="   ")))
    finally:
        mod._state["model_loaded"] = False
    assert resp.status_code == 400, f"空 text 应 400，实得 {resp.status_code}"
    assert _json_of(resp)["code"] == "BAD_REQUEST", resp.body


def test_encode_returns_503_before_the_model_is_loaded():
    """🔑 模型没就绪 ⇒ **503 + VISUAL_UNAVAILABLE**（且说清在等什么）。

    ⚠️ 与"服务没起"要能分开：调用方看到 503 会等重试，
    看到"连不上"会去起进程。
    """
    from services.visual_service import main as mod

    mod._state["model_loaded"] = False
    mod._state["load_error"] = None
    resp = run(mod.encode(mod.EncodeReq(text="照片")))
    assert resp.status_code == 503, f"未加载应 503，实得 {resp.status_code}"
    body = _json_of(resp)
    assert body["code"] == "VISUAL_UNAVAILABLE", body
    # 🔑 判**中文**而不是 `b"..."`：JSON 响应的中文是 `\uXXXX` 转义，
    #   按字节判中文会在任何实现下都不成立（我第一版就这么写的，全绿）。
    assert "模型" in body["detail"], (
        f"503 的文案没说是模型没就绪：{body['detail']} —— "
        "运维会去看 Qdrant，而 Qdrant 没问题"
    )
    assert "13s" in body["detail"], (
        "没说加载要多久 —— 运维会以为服务坏了（实测 12.8~15.0s）"
    )


def _json_of(resp) -> dict:  # noqa: ANN001
    """`JSONResponse` 的 body → 已解析的 dict。"""
    import json

    return json.loads(resp.body)


def test_percentile_refuses_to_invent_a_number_from_too_few_samples():
    """🔑 p95 样本不足时给 `null`，**不给一个假的数字**。

    ⚠️ `/health` 的 `encode_p95_ms` 会被拿来判断"该不该扩容"。
    拿 3 个样本算出的"p95"是噪声，而它长得与真数字一模一样。
    判据是**合成输入**：3 个样本 → `None`，20 个 → 有值。
    """
    from services.visual_service.main import percentile

    assert percentile([1.0, 2.0, 3.0], 0.95) is None, (
        "3 个样本就报了 p95 —— 那是噪声"
    )
    assert percentile([float(i) for i in range(1, 21)], 0.95) is not None


def test_visual_service_exposes_no_openapi():
    """🔑 视觉服务**关掉 `/docs` 与 `/openapi.json`**。

    ⚠️ 它没有鉴权（D158 的设计如此：只绑回环、只有 API 一个调用方）。
    开着 `/docs` 等于把"这台机器上跑着什么、怎么调它"画给公网 ——
    与 `EXPOSE_DOCS` 那条决策（D138）同一条理由。
    """
    from services.visual_service.main import app

    assert app.docs_url is None
    assert app.openapi_url is None
    assert app.redoc_url is None


# ============================================================
# 5. available_legs 不得硬编码
# ============================================================


def test_available_legs_awaits_visual_available():
    """🔑 `available_legs()` 必须 **await** `visual_available()`。

    🔴 去掉 `await` 时那个调用返回一个 **coroutine 对象**，
    而 `if <coroutine>` **恒为真** ⇒ `available_legs()` 会永远说
    "视觉腿可用"，**即使视觉服务根本没起**（§5.151c 那一族：
    判据要指向性质，不是"调用过"）。
    ⚠️ 判据是 `await visual_available()` 这个**形状**：
    只判"出现过这个名字"的话，去掉 await 后名字还在，会全绿。
    """
    src = ast.unparse(_fn(ENGINE, "available_legs"))
    assert "await visual_available()" in src, (
        "visual_available 没有 await —— coroutine 对象恒为真 ⇒ "
        "available_legs 会永远说视觉腿可用"
    )


def test_available_legs_is_not_hardcoded():
    """🔑 `available_legs()` 必须**问** `visual_available()`，不能写死三条。

    ⚠️ 硬编码 `["dense","sparse","trgm"]` 时，判据在接通前后**都是对的**
    （接通前它恰好与实际一致）⇒ 没有测试会红，而运维会照着一个错的
    `/health` 排障 —— 那是"看起来实现了、其实没实现"的典型。
    """
    assert "visual_available" in _called_names(_fn(ENGINE, "available_legs"))


def test_available_legs_reports_visual_when_the_service_is_up(fake_visual):
    """🔑 判据 1：`available_legs()` 含 `visual`（远端可用时）。

    ⚠️ 反过来也判（服务不可达时**不含**）—— 只判前者的话，
    变异成"永远返回四条腿"会全绿，而那让 `/health` 撒谎。
    """
    from services.retrieval.engine import available_legs

    fake_visual(_FakeClient())
    assert "visual" in run(available_legs())

    fake_visual(_FakeClient(raise_on="health"))
    assert "visual" not in run(available_legs())


# ============================================================
# 6. 跨租户：仍然只有一份过滤逻辑
# ============================================================


def test_cross_tenant_filter_is_built_locally_by_the_same_single_entry():
    """🔑 跨租户仍拿不到别人的照片 —— 因为过滤入口**没有变过**。

    🔴 这条不是形式：D158 新增了一个进程，最容易出的事就是
    "为了让它也能查库"而在那边**再加一份过滤**。
    上面 `test_visual_service_never_touches_qdrant_or_auth` 钉住了
    "服务里没有过滤"，这条钉住"这边那份仍然是唯一入口" ——
    两条合起来才是"一份过滤逻辑"这个性质。
    """
    import services.common.auth as auth
    import services.retrieval.visual as vmod

    assert auth.build_qdrant_filter.__module__ == "services.common.auth"
    assert vmod.build_qdrant_filter is auth.build_qdrant_filter
    # 🔑 判"**代码里没有任何 filter 键名字符串**"而不是扫 `_code_only` 的文本：
    #   `ast.unparse` 会把 `"kb_id"` 规范成 `'kb_id'`（单引号），
    #   所以按双引号写的判据**永远成立** —— 我第一版就是这么写的，
    #   变异"自己拼一个 filter 字面量"时全绿（§5.151c）。
    #   ⇒ 直接扫 AST 的字符串常量，与引号风格无关。
    keys = _string_constants(VISUAL_PY)
    for banned in ("kb_id", "workspace_id", "visibility", "owner_id", "source_kind"):
        assert banned not in keys, (
            f"visual.py 里出现了 filter 键名字面量 {banned!r} —— "
            "过滤必须在 auth.build_qdrant_filter 里（CONVENTIONS §0.2）。"
            "自己拼 filter 就是 §0.6 那次真实越权的形状"
        )


def test_string_constant_scanner_sees_both_quote_styles():
    """🔑 上条那个探测器本身必须能抓到字面量（合成输入，§5.151c）。"""
    src = ast.parse('def f(kb_id):\n    return {"must": {"key": "kb_id"}}\n')
    got = _string_constants_from(src)
    assert "kb_id" in got, got
    src2 = ast.parse("def g():\n    return {'key': 'workspace_id'}\n")
    assert "workspace_id" in _string_constants_from(src2)


def test_visual_service_url_is_configurable_and_defaults_to_loopback():
    """🔑 `visual_service_url` 默认 `http://127.0.0.1:8020` 且可配。

    ⚠️ 默认值钉死回环：视觉服务**没有鉴权**，把它指到 `0.0.0.0`
    或公网地址等于让任何人都能刷这台机器的 2.65GB 模型。
    """
    from services.common.config import Settings

    s = Settings()
    assert s.visual_service_url == "http://127.0.0.1:8020"
    assert s.visual_service_url.startswith("http://127.0.0.1")
    assert s.visual_service_timeout_s == 2.0, (
        "超时不是 2s（实测 6 路并发 p95 905ms ⇒ 要两倍余量）"
    )
    assert s.visual_service_max_concurrency == 4, (
        "并发上限不是 4（6 路并发实测吃 8.7 核，放更多只会更慢）"
    )


def _dev_sh_case_branches(src: str) -> dict[str, str]:
    """`dev.sh` 的 `case` 里每个子命令分支的正文。"""
    m = re.search(r'case "\$\{1:-(\w+)\}" in(.*?)\nesac', src, re.S)
    assert m, "dev.sh 的 case 结构变了（守卫要跟着改）"
    body = m.group(2)
    out: dict[str, str] = {}
    cur: list[str] = []
    names: list[str] = []
    for line in body.splitlines():
        mm = re.match(r"\s*([a-z|]+)\)\s*$", line)
        if mm:
            if names:
                out[",".join(names)] = "\n".join(cur)
            names = mm.group(1).split("|")
            cur = []
        elif names:
            cur.append(line)
    if names:
        out[",".join(names)] = "\n".join(cur)
    return out


def test_dev_sh_starts_the_visual_service():
    """🔑 视觉服务必须**真的被起起来**，且**只绑回环**（D204：脚本化部署）。

    ⚠️ D204 之后启动命令搬到了 `deploy/visual.sh`，`dev.sh` 只负责**委托**它。
    判据分两半，各自指向**调用点/启动点的正文**而不是"脚本里出现过这个字符串"：
      ① `dev.sh` 的 `start` 分支必须**调用** `visual.sh start`；
      ② `visual.sh` 的 `start` 分支必须真的拉起 `services.visual_service.main`，
         并钉 `--host 127.0.0.1`（它没有鉴权，绑 0.0.0.0 等于对外开放模型服务）。
    """
    dev = _src(ROOT / "deploy" / "dev.sh")
    start_body = next(
        (v for k, v in _dev_sh_case_branches(dev).items() if "start" in k.split(",")),
        "",
    )
    assert "visual.sh" in start_body and "start" in start_body, (
        "dev.sh 的 start 分支没有委托 `visual.sh start`（改了不生效）"
    )

    vis = _src(ROOT / "deploy" / "visual.sh")
    vis_start = next(
        (v for k, v in _dev_sh_case_branches(vis).items() if "start" in k.split(",")),
        "",
    )
    assert "services.visual_service.main" in vis_start, (
        "visual.sh 的 start 分支里没有视觉服务（改了不生效）"
    )
    assert "pkh_visual.log" in vis_start, "视觉服务日志路径没定下来"
    assert "VISUAL_PID_FILE" in vis, "视觉服务没有 pid 文件（§0.10 的坑 #2）"
    # ⚠️ 必须判**视觉服务那一行**的 host，不能判整个 start 分支里有没有
    #   `127.0.0.1` —— API 那一行也是 127.0.0.1，于是判据在两种实现下都成立。
    vis_lines = [
        ln for ln in vis_start.splitlines()
        if "services.visual_service.main" in ln or "VISUAL_PORT" in ln
    ]
    assert vis_lines, "visual.sh 的 start 分支里找不到视觉服务的启动命令"
    joined = "\n".join(vis_lines)
    assert "--host 127.0.0.1" in joined, (
        f"视觉服务没有绑 127.0.0.1（它没有鉴权）—— 看到的是：{joined.strip()}"
    )
    assert "0.0.0.0" not in joined, "视觉服务绑了 0.0.0.0（它没有鉴权）"


def test_dev_sh_stops_the_visual_service():
    """🔑 `stop` 必须**真的调** `stop_visual`（不只是定义它）。

    ⚠️ 这条曾经的第一版判"脚本里出现过 `stop_visual`" ⇒ 变异把
    `stop` 分支里那一行删掉时**全绿** —— 而那正是"改了不生效"
    那个 20 分钟坑的形状（§0.10）：旧进程继续占着 2.65GB 并用旧代码应答。
    ⇒ 判据指向**调用点**（在 `stop` 分支的正文里），不是定义。
    """
    src = _src(ROOT / "deploy" / "dev.sh")
    branches = _dev_sh_case_branches(src)
    stop_body = next((v for k, v in branches.items() if "stop" in k.split(",")), "")
    assert "stop_visual" in stop_body, (
        "stop 分支里没调 stop_visual —— 改了不生效（CONVENTIONS §0.10）"
    )
    assert "stop_worker" in stop_body and "stop_api" in stop_body


def test_visual_sh_stop_sweeps_leftovers():
    """🔑 `visual.sh stop` 必须**按命令行特征扫残留**，不只是 kill pid 文件。

    ⚠️ 与 worker 的坑 #2 同形：`kill -- -PID` 杀不干净 `uv run` 的子进程。
    漏了这一条，视觉服务会留一个进程占 2.65GB 并继续用旧代码应答
    —— 而它"看起来是活的"（端口还通）。D204 之后 stop 归 `visual.sh`。
    """
    src = _src(ROOT / "deploy" / "visual.sh")
    # 🔑 判"`_stale_by_pattern "$VISUAL_PATTERN"` **用在 stop() 函数体里**。
    # ⚠️ 只判"脚本里出现过 `$VISUAL_PATTERN`"的话，把 stop() 里那两行
    #   换成 `pgrep -f colSmol`（或干脆删掉）也会成立 —— 而那正是
    #   §0.10 那个 20 分钟坑：旧进程继续占着 2.65GB 并用旧代码应答。
    body = src[src.index("stop() {"):]
    body = body[: body.index("\n}\n")]
    assert re.search(r'_stale_by_pattern\s+"?\$VISUAL_PATTERN', body), (
        f"visual.sh 的 stop() 没有按 VISUAL_PATTERN 扫残留（§0.10 的坑 #2）：{body}"
    )


def test_env_example_documents_the_visual_service():
    """🔑 `.env.example` 必须写出这三个新键。

    🔑 尤其 `VISUAL_SERVICE_URL` 留空是**关掉读侧**的官方方式 ——
    只跑文本检索的部署（小机器、2C4G）需要这条出口，
    而 §4.1b 的"想翻这条决策"条件之一正是 2C4G。
    """
    src = _src(ROOT / ".env.example")
    for key in ("VISUAL_SERVICE_URL", "VISUAL_SERVICE_TIMEOUT_S",
                "VISUAL_SERVICE_MAX_CONCURRENCY"):
        assert key in src, f".env.example 没写 {key}"


def test_visual_service_module_docstring_names_its_two_limits():
    """🔑 服务模块的 docstring 必须写清它**不做**什么，且是**靠行动动词**说的。

    ⚠️ 判据指向**"它不做 X"的句子**，而不是"docstring 里出现过 `Qdrant`"：
    后者在把"它**不是**查 Qdrant 的服务"那半句删掉之后仍然成立
    （"Qdrant" 还在前半句里）—— 我第一版就这么写的，变异全绿（§5.151c）。
    ⇒ 要求存在一句**否定式**的边界声明。
    """
    doc = ast.get_docstring(ast.parse(_src(SVC))) or ""
    for banned, verb in (("Qdrant", "查"), ("租户", "做")):
        assert re.search(rf"不{verb}[^。\n]*{banned}", doc), (
            f"docstring 没有一句明确说\"不{verb}{banned}\"的边界声明 —— "
            "下一个人会以为这个服务能查库/能过滤（而它刻意不做）"
        )
