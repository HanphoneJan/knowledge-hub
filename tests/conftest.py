"""pytest 全局配置与共享 fixture。

## 为什么需要 `pythonpath`

`services/` 是**源码目录**不是安装到 site-packages 的包
（`pyproject.toml` 没有 build-system，项目不发布）。
`pytest.ini` 的 `pythonpath` 在某些调用方式下不生效，所以显式插 `sys.path`。

## 为什么需要共享 event loop

`services/common/db.py` 的异步引擎是**模块级单例**，asyncpg 连接池绑定
**创建它的那个 event loop**（PROGRESS.md §5.17 记录的坑）。

单进程 FastAPI 服务不受影响（全程一个 loop），但测试里如果每个用例各调一次
`asyncio.run()`，就会：

```
RuntimeError: Event loop is closed
# 或
RuntimeError: Task ... got Future ... attached to a different loop
```

所以这里建一个 **session 级的 event loop** 供所有异步测试复用，
`run_async` 把 coroutine 丢进那个 loop 而不是新建。
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable, Coroutine, Iterator
from pathlib import Path
from typing import Any, TypeVar

import pytest

# 🔴 强制离线加载模型（HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE）。
#   不加的话，每次加载 bge-small / colSmol 都会去 huggingface.co 查 HEAD，
#   而本机网络不可达 ⇒ 每个请求重试 5 次（1+2+4+8+8≈23s）。实测整套 pytest
#   从 ~50s 涨到 **28 分钟**，症状是"测试没变慢、机器没变慢，就是慢"。
#   `deploy/dev.sh` 早就导出了这两个变量（第 32~33 行），但 pytest 与 eval
#   脚本没设 ⇒ 只有它们慢。用 `setdefault`：运维仍可用 `HF_HUB_OFFLINE=0` 覆盖。
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

T = TypeVar("T")


@pytest.fixture(scope="session")
def event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """整个测试会话共用一个 event loop（见模块 docstring）。"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()


def _purge_test_users(emails: list[str]) -> None:
    """删掉测试造的账号，并**重建 BM25 与向量**。

    🔑 **不要在测试里直接 `DELETE FROM users`**（D146）。级联硬删会干掉
    `chunks` 行，而 BM25 的 `df` 只在入库与 `handle_reindex` 里维护
    ⇒ 删完 `corpus_stats.n_docs` 虚高。后果**不是报错**而是检索质量悄悄变差
    （`idf = log(1 + (N - df + .5)/(df + .5))` 里 N 虚高 → idf 被压平），
    而且下一次跑 pytest 会**假红**在 `test_corpus_stats_matches_live_chunks`
    —— 症状与被测代码毫无关系（PROGRESS §5.109）。

    ⚠️ 这个坑最恶的地方是**顺序依赖**：紧跟着的 eval 脚本自己会重建 BM25，
    于是"跑一遍全量基线，下一次 pytest 就绿了"，看起来像偶发（真被咬过一次）。
    本函数把重建放在**清场的同一个 fixture 里**，让漂移根本没机会产生。

    连带好处：`reset_users_and_bm25` 里的 `handle_reindex(doc_id=None)`
    会清掉 Qdrant 的**孤儿点** —— 硬删用户留下的点否则会变成"多 N 个"
    （§5.43 那一族：pg 没了、qdrant 还在）。

    @param emails 完整邮箱（`eval/_reset.drop_users` 用 `IN` 精确匹配）
    """
    if not emails:
        return
    if str(ROOT / "eval") not in sys.path:
        sys.path.insert(0, str(ROOT / "eval"))
    from _reset import reset_users_and_bm25  # noqa: PLC0415 —— 按需引入

    reset_users_and_bm25(emails)


@pytest.fixture(scope="session")
def purge_users() -> Callable[[list[str]], None]:
    """清场助手（见 `_purge_test_users`）。

    🔑 做成 fixture 而不是让各文件 `from conftest import …`：`tests/` **不是包**
    （没有 `__init__.py`），那种 import 依赖 pytest 的 import mode，属于"能跑但别信"。
    ⚠️ `scope="session"`：用它的多半是 `scope="module"` 的清场 fixture，
    function 级会被 pytest 判 ScopeMismatch。

    @usage
        @pytest.fixture(scope="module")
        def _key(purge_users):
            ...
            purge_users([mail])
    """
    return _purge_test_users


@pytest.fixture
def run_async(event_loop) -> Callable[[Coroutine[Any, Any, T]], T]:
    """在共享 loop 上跑 coroutine。

    @usage
        def test_x(run_async):
            rows = run_async(fetch())
    """

    def _run(coro: Coroutine[Any, Any, T]) -> T:
        return event_loop.run_until_complete(coro)

    return _run
