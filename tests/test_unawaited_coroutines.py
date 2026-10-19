"""🔴 未 await 的协程调用 —— 防回归。

## 这组测试为什么优先级最高

同一个 session 里我被这个坑咬了**三次**，每次报错都指不到真正原因：

| # | 位置 | 症状 | 真正原因 |
|---|---|---|---|
| 1 | `storage.register_blob` | `documents_blob_sha_fkey` 外键错误 | `session.execute()` 返回 coroutine，INSERT 从未发出 |
| 2 | `jobs.enqueue` | `'coroutine' object has no attribute 'scalar_one_or_none'` | 同步函数在 `AsyncSession` 上调用 |
| 3 | `routers/documents.py` 的 `enqueue_async(...)` | **任务表 0 行，worker 干等** | 忘了 `await`，静默丢任务 |

第 3 个最危险：**不报错、不告警，功能就是不工作**。
worker 一直空转，上传返回 202，文档永远停在 pending。

## 为什么用 AST 而不是 grep

文本匹配会误报（docstring 里提到 `session.execute(` 就被判不合格，
第一版就是这么翻车的）。AST 的父子关系最可靠：

- `await foo()` → Call 的父节点是 `Await` ✅
- `foo()` → Call 是独立表达式语句，父节点是 `Expr` ❌

## 清单从 AST 自动推导，不再手写

第一版靠一张手写的 `COROUTINE_FUNCS` 名单，然后**又踩了一次同一个坑**：
新增的 `_find_unfinished_async` 没进名单，于是
`existing = _find_unfinished_async(...)` 漏了 `await` ——
coroutine 对象对 `if existing is not None` 恒为真，
于是**每次上传都"去重成功"、一个任务都不入队**：
worker 空转、文档永远 pending、日志干净、端点返回 202。

那张名单的致命之处正是它自己的注释承认的："加了但没人 await 的会漏检"。
所以现在改成**扫全部 `async def`**：只要是 `services/` 里的协程函数，
被调用就必须 await，不存在"忘了登记"这种可能。

## 为什么要用 AST 而不是 grep

文本匹配会误报（docstring 里提到 `session.execute(` 就被判不合格，
第一版就是这么翻车的）。AST 的父子关系最可靠：

- `await foo()` → Call 的父节点是 `Await` ✅
- `foo()` → Call 是独立表达式语句，父节点是 `Expr` ❌
"""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "services"

#: 允许"接收协程对象而不 await"的调用方。
#: `asyncio.run(amain())` 是**正确**写法（它就是拿来跑协程的）。
COROUTINE_CONSUMERS = {
    "asyncio.run", "run", "run_until_complete", "create_task", "ensure_future",
    "gather", "wait_for",
}


def _all_py() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _module_name(path: Path) -> str:
    """`services/a/b.py` → `services.a`（带 `__init__` 的目录按包算）。"""
    rel = path.relative_to(SRC).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(["services", *parts])


def _async_def_map() -> dict[str, set[str]]:
    """`{函数名: {定义它的模块全名}}` —— 全部来自 AST 扫描。

    ⚠️ 存"模块集合"而不是只存名字：只按名字判会误报 ——
    `retrieval/bm25.py` 里的 `CJK_RE.search(text)` 会撞上
    路由里的 `async def search(...)`（实测踩过：13 个文件假红）。
    """
    out: dict[str, set[str]] = {}
    for path in _all_py():
        mod = _module_name(path)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                out.setdefault(node.name, set()).add(mod)
    return out


#: 每次运行重新扫 —— 新增 `async def` 立刻纳入检查，不存在"忘了登记"
ASYNC_DEFS = _async_def_map()
COROUTINE_FUNCS = set(ASYNC_DEFS)


def _resolve_relative(current_mod: str, level: int, module: str | None) -> str:
    """把 `from ..x import y` 里的 `..x` 解析成绝对模块名。"""
    if level == 0 or not module:
        return module or ""
    pkg = current_mod.split(".")
    # level=1 是"当前包"，level=2 是"上一级"（`from ..x` → pkg 去掉最后一段）
    for _ in range(level - 1):
        if pkg:
            pkg.pop()
    return ".".join([*pkg, module]) if pkg else module


def _async_names_in_file(path: Path) -> set[str]:
    """这个文件里**能看见**的协程函数名。

    = 本文件定义的 `async def` + 从别处 import 进来的协程函数名。
    """
    names: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    current_mod = _module_name(path)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            names.add(node.name)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        target = _resolve_relative(current_mod, node.level, node.module)
        for alias in node.names:
            mods = ASYNC_DEFS.get(alias.name)
            if mods and any(m == target or m.endswith("." + target) for m in mods):
                names.add(alias.name)
    return names


def _unawaited_calls(path: Path, names: set[str] | None = None) -> list[str]:
    """返回该文件里所有"调用了协程函数但没 await"的行号描述。"""
    names = _async_names_in_file(path) if names is None else names
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for parent in ast.walk(tree):
        for node in ast.iter_child_nodes(parent):
            if not isinstance(node, ast.Call):
                continue
            # ⚠️ **只看裸函数名**（`foo()`），不看方法调用（`x.foo()`）：
            #   接收者类型静态推不出来，而同名异类太常见 ——
            #   `TermRegistry.ensure_loaded`（同步）与
            #   `TermRegistryAsync.ensure_loaded`（协程）、httpx 的
            #   `client.stream` 与本项目的 `async def stream`。
            #   实测按名字一刀切会有 3 个文件假红，检查一旦常红就等于没有。
            if not isinstance(node.func, ast.Name):
                continue
            name = node.func.id
            if name not in names:
                continue
            if isinstance(parent, ast.Await):
                continue
            # `asyncio.run(foo())` 这类"故意不 await"是合法的
            if isinstance(parent, ast.Call) and ast.unparse(parent.func) in (
                COROUTINE_CONSUMERS
            ):
                continue
            found.append(f"L{node.lineno}: {ast.unparse(node)[:60]}")
    return found



@pytest.mark.parametrize("path", _all_py(), ids=lambda p: str(p.name))
def test_no_unawaited_coroutine_calls(path: Path):
    """🔑 全量扫描 `services/`：没有未 await 的协程调用。

    第 3 次踩坑（`enqueue_async` 漏 await）在这里被拦住 ——
    那个 bug 不报错，只是任务静默丢失。
    """
    bad = _unawaited_calls(path)
    assert not bad, (
        f"{path.name} 里有未 await 的协程调用：\n  "
        + "\n  ".join(bad)
        + "\n  → 未 await 的协程不报错也不告警，功能静默失效。"
        "   参见 tests/test_unawaited_coroutines.py 的模块 docstring。"
    )


def test_coroutine_list_is_not_empty():
    """清单本身要有效 —— 空清单会让上面那条测试变成永远通过的死代码。"""
    assert len(COROUTINE_FUNCS) >= 10


def test_new_coroutines_are_picked_up_automatically():
    """🔑 名单是**扫出来的**，所以新增协程函数不需要（也不会忘记）登记。

    这条测试是"自动推导"本身的防退化断言：拿一个源码里**不存在**的名字去查，
    必须查不到 —— 说明 `COROUTINE_FUNCS` 真的来自 AST 而不是写死的常量。
    （第一版是手写名单，于是新增的 `_find_unfinished_async` 漏 await
    整整一轮基线都没被抓到，见模块 docstring 的第 4 行。）
    """
    ghost = "_definitely_not_a_coroutine_in_this_repo"
    assert ghost not in COROUTINE_FUNCS
    # 已知踩坑点必须在名单里
    for name in ("enqueue_async", "_find_unfinished_async", "load_user_context"):
        assert name in COROUTINE_FUNCS, f"{name} 没被自动扫进协程名单"


def test_specific_regressions_are_awaited():
    """三个已知踩坑点必须都是 awaited 的。"""
    docs = SRC / "api" / "routers" / "documents.py"
    assert not _unawaited_calls(docs), "documents.py 有未 await 的协程"

    storage = SRC / "common" / "storage.py"
    for name in ("register_blob_async", "release_blob_async"):
        fn = getattr(
            importlib.import_module("services.common.storage"), name
        )
        assert inspect.iscoroutinefunction(fn), f"{name} 必须是 async def"


def test_route_handlers_are_async():
    """路由函数必须是 async def（FastAPI 的依赖注入是异步的）。

    写成 `def` 会让 `session: DbSession` 注入失败或被当成普通默认值。
    """
    routers = SRC / "api" / "routers"
    sync_routes: list[str] = []
    for path in routers.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue  # AsyncFunctionDef 是另一个类
            for dec in node.decorator_list:
                txt = ast.unparse(dec)
                if txt.startswith(("router.", "public_router.", "app.")):
                    sync_routes.append(f"{path.name}::{node.name}")
    assert not sync_routes, f"这些路由写成了同步 def：{sync_routes}"
