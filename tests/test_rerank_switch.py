"""🔑 per-KB 精排开关（D87/D90）：**必须有写路径**。

## 这个文件在钉什么

`knowledge_bases.rerank_model` 这一列存在了很久，读路径也齐了
（`/search`、`/ask`、MCP、A2A 四处都读它），但**写路径一处都没有**：
`CreateKbReq` / `UpdateKbReq` 里没有它，`KbOut` 里也不返回它。

后果不是"少个参数"，而是**这个功能对真实用户不存在** ——
唯一能开启它的方式是手写 SQL，而 `eval/api_1d_check.py` 正是这么干的：

```python
_psql(f"UPDATE knowledge_bases SET rerank_model='BAAI/bge-reranker-v2-m3' WHERE id={kb_id}")
```

也就是说验收脚本在验一个**用户无法使用的功能**。REQUIREMENTS M2/M6
把 per-KB 精排标成 ✅，而代码不支持 —— 这类"文档 ✅ / 代码没有"
正是本项目反复吃亏的地方（PROGRESS §5.86 的"库里没有"三态、
§5.92 的"看起来对"的代码）。

## 为什么 `null` 必须表示"不启用"而不是"用全局默认"

第一版 `model_name()` 写成 `if kb_rerank is not None: return kb_rerank`，
于是**每个库都在跑精排**（实测 `applied=true`、白花 5.2s），
而 `/search/debug` 显示的模型名还是"配置里那个"——
从界面上完全看不出配置没生效（D90）。
"""

from __future__ import annotations
from _docs import contract_text

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


# ============================================================
# 1. 写路径存在（这是本文件的核心断言）
# ============================================================


def test_update_kb_req_accepts_rerank_model():
    """🔑 `PATCH /kb/{id}` 必须能写 `rerank_model`。"""
    from services.common.schemas import UpdateKbReq

    assert "rerank_model" in UpdateKbReq.model_fields, (
        "UpdateKbReq 没有 rerank_model —— per-KB 精排只能靠手写 SQL 开启"
    )


def test_kb_out_reports_rerank_model():
    """🔑 `KbOut` 必须**返回**它，否则界面无法显示当前状态。"""
    from services.common.schemas import KbOut

    assert "rerank_model" in KbOut.model_fields, (
        "KbOut 不返回 rerank_model —— 前端看不到这个库到底开没开精排"
    )
    # 默认必须是"未启用"，而不是全局默认（D90）
    import datetime as _dt

    kb = KbOut(
        id=1, workspace_id=1, name="k", description=None, visibility="private",
        source_kind="file", embed_model="BAAI/bge-small-zh-v1.5",
        chunk_size=500, chunk_overlap=50, generate_image_desc=False,
        pii_mask=False, created_by=1,
        created_at=_dt.datetime.now(_dt.UTC), updated_at=_dt.datetime.now(_dt.UTC),
    )
    assert kb.rerank_model is None, "没配就该是 None（= 不启用）"


def test_rerank_is_writable_through_the_only_kb_write_endpoint():
    """🔑 逐字检查 `update_kb` 里真的有赋值（而不是只在 schema 里声明）。

    ⚠️ schema 有字段但路由忘了赋值，是本项目踩过的形状
    （`/search/debug` 那个字段：端点有、字段有、前端也能画，
    实际从没被写过 —— PROGRESS §3.4 的"有端点 ≠ 有进度"同源）。
    """
    src = (ROOT / "services" / "api" / "routers" / "kb.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(src)
    fn = next(
        n for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "update_kb"
    )
    assigns = [
        ast.unparse(n) for n in ast.walk(fn)
        if isinstance(n, ast.Assign)
        and "kb.rerank_model" in ast.unparse(n)
    ]
    assert assigns, "update_kb 从不写 kb.rerank_model（PATCH 收下就丢）"


def test_empty_string_means_disabled_not_dirty_data():
    """🔑 `""` 必须归一成 `None`（不启用）。

    留着 `""` 的话 `rerank.model_name()` 会拿它去加载模型然后失败 ——
    而失败点在"加载模型"，报错完全指不到"这个字段本该是关的"。
    """
    from services.common.schemas import UpdateKbReq

    req = UpdateKbReq(rerank_model="   ")
    assert req.rerank_model is not None, "测试本身坏了（空串应被收下再归一）"

    src = (ROOT / "services" / "api" / "routers" / "kb.py").read_text(
        encoding="utf-8"
    )
    assert ".strip() or None" in src, (
        "rerank_model 没有把空白串归一成 None（D90：空 = 不启用）"
    )


# ============================================================
# 2. 读路径与写路径同源（不许有第二处真相）
# ============================================================


def test_every_reader_reads_the_column_not_a_request_param():
    """🔑 读 `kb.rerank_model` 的地方不许从**请求参数**取。

    精排改变排序，所以它必须是**服务端按库**决定的；
    客户端能指定就等于任意用户能改全库的排序行为。
    """
    # ⚠️ `/ask` 的路由层**不直接读**这一列 —— 它调 `retrieval/ask.py::prepare`，
    #   由那里统一读（§0.3：检索只有一个实现）。判据要落在**真正读的那一层**。
    readers = {
        "services/api/routers/search.py": "KnowledgeBase.rerank_model",
        "services/retrieval/ask.py": "KnowledgeBase.rerank_model",
        "services/mcp/tools.py": "KnowledgeBase.rerank_model",
    }
    for path, needle in readers.items():
        src = (ROOT / path).read_text(encoding="utf-8")
        assert needle in src, f"{path} 没有从库里读 rerank_model"

    src = (ROOT / "services" / "common" / "schemas.py").read_text(encoding="utf-8")
    # SearchReq / AskReq 里都不许出现 rerank_model
    tree = ast.parse(src)
    for cls in (
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name in ("SearchReq", "AskReq")
    ):
        assert "rerank" not in ast.unparse(cls), (
            f"{cls.name} 里有 rerank 字段 —— 排序不能由客户端指定"
        )


def test_mcp_and_a2a_also_read_the_column():
    """MCP / A2A 与 API **共用同一套检索**（D10），所以也必须读这一列。

    漏掉的后果：网页上开了精排，Claude 那边搜出来的排序不一样 ——
    同一份数据两个答案，而没有任何地方提示过。
    """
    # a2a 复用 MCP 的内核（D10），所以它自己没有独立的读取点也是对的 ——
    # 判据是"这两个外部入口最终都走到同一处读取"。
    for path in ("services/mcp/tools.py", "services/api/routers/a2a.py"):
        src = (ROOT / path).read_text(encoding="utf-8")
        assert "rerank" in src, f"{path} 完全没提精排 —— 外部 AI 搜出来的排序会与网页不同"


# ============================================================
# 3. 契约（唯一端点来源）
# ============================================================


def test_contract_documents_the_rerank_field():
    """契约里 `PATCH /kb/{id}` 的字段清单必须包含它，否则照契约写的前端会漏。"""
    text = contract_text()
    assert "rerank_model" in text, (
        "DESIGN-API.md 没写 rerank_model —— 契约与代码漂移"
    )


@pytest.mark.parametrize(
    "path",
    [
        "services/common/schemas.py",
        "services/api/routers/kb.py",
        "services/common/models.py",
    ],
)
def test_column_exists_in_model(path: str):
    """列必须真的在模型里（否则 PATCH 写到不存在的属性上，静默无效）。"""
    assert "rerank_model" in (ROOT / path).read_text(encoding="utf-8")
