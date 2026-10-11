"""🔴 D124：视觉腿（colSmol）的防回归。

这一组测试全部**不加载 colSmol**（2.5GB + 一次前向 15s，不该进单元测试）。
它们锁的是"让视觉腿坏掉的那几件事"，而不是检索质量：

| # | 曾经坏在哪 | 用什么钉住 |
|---|---|---|
| 1 | 拿 `AutoModel` 加载 → 960 维，写进 128 维 collection | 源码形状（`MultiVectorEncoder` + `VDR_DIM`） |
| 2 | processor 只传 `images=` → 没有 `input_ids` | 不再手调 processor，只调 `encode_document` |
| 3 | 调了两个**不存在**的函数 → NameError | 视觉 payload 由一个可单测的函数产出 |
| 4 | `enqueue` 去重不看 payload → 一页只入队一个 | 真的建一个假 session 跑一遍 |
| 5 | 纯扫描件永远停在 pending | 状态翻转的条件是"点数补齐" |

第 4 条是本组里最重要的一条：它**不会报错**（任务状态全 done、日志干净），
只有"页数对不上"才看得出来 —— 而页级索引当时根本没人在意。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
POOL = ROOT / "services" / "retrieval" / "pool.py"
HANDLERS = ROOT / "services" / "worker" / "handlers" / "__init__.py"
PIPELINE = ROOT / "services" / "ingest" / "pipeline.py"
#: 文档终态判定的**唯一**实现（D127）。视觉腿的 `ready` 翻转在这里。
DOC_STATE = ROOT / "services" / "worker" / "doc_state.py"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _code_only(path: Path) -> str:
    """把 docstring 与注释剥掉，只留**会被执行**的代码。

    ⚠️ 这一组测试里好几条是"某段代码不许出现"，而这些模块的 docstring
    里**必然**要写清"第一版错在哪"（里面就有 `AutoModel`、`images=`）。
    用文本匹配的话，解释原因的注释会把测试自己告发 ——
    `tests/test_retrieval_filters.py` 已经踩过这个（它改用 AST）。
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


# ============================================================
# 1. 加载方式：MultiVectorEncoder，不是 AutoModel
# ============================================================


def test_visual_model_is_loaded_as_multivector_encoder():
    """🔑 `vidore/colSmol-500M` 是 SentenceTransformers 的 MultiVectorEncoder。

    `modules.json` = Transformer → 1_Dense(960→128) → Normalize → 3_MultiVectorMask。
    裸 `AutoModel` 只有 960 维（投影层以 `linear.weight | UNEXPECTED` 被丢掉），
    写进 `VDR_DIM=128` 的 `kb_visual` 必然 400 —— 这就是 `kb_visual` 长期 0 点的根因。
    """
    code = _code_only(POOL)
    assert "MultiVectorEncoder" in code, "没有用 MultiVectorEncoder 加载 colSmol"
    assert "AutoModel" not in code, (
        "pool.py 里还有 AutoModel —— colSmol 不是裸 transformers 模型"
    )
    assert "AutoProcessor" not in code, (
        "pool.py 里还在手工调 processor（第一版就是这里错的，见模块 docstring）"
    )


def test_visual_dim_matches_the_collection():
    """🔑 编码输出的维度必须等于 collection 的维度，两边同源。

    写死 128 的时代里，`model_status()` 报 128 而模型给 960 ——
    健康检查说"正常"，upsert 报 400，没人会把两件事联系起来。
    """
    from services.common.qdrant import VDR_DIM

    assert VDR_DIM == 128
    assert "VDR_DIM" in _src(POOL), "model_status 没用 VDR_DIM（又抄了一份数字）"
    assert '"dim": 128' not in _src(POOL), "pool.py 里还有写死的 128"


def test_encode_entrypoints_go_through_the_encoder():
    """文档侧走 `encode_document`、查询侧走 `encode_query`（不传 images）。"""
    code = _code_only(POOL)
    assert "encode_document(" in code, "encode_images 没走 encode_document"
    assert "encode_query(" in code, "encode_vdr_query 没走 encode_query"
    # ⚠️ colSmol README 的官方写法是 `images=[[]]`，transformers 5.18 会抛
    # AttributeError —— 那个坑只能靠"不手调 processor"来避，不靠注释。
    assert "images=" not in code, "pool.py 的代码里出现了 images=（别照 README 抄）"


# ============================================================
# 2. 视觉 payload（曾经调了两个不存在的函数）
# ============================================================


class _FakeSession:
    def __init__(self, rows: dict[int, object]) -> None:
        self.rows = rows

    def get(self, model, pk):  # noqa: ANN001, ANN201
        return self.rows.get(pk)


class _Doc:
    id = 7
    kb_id = 3
    file_name = "扫描件.pdf"
    mime_type = "application/pdf"


class _Kb:
    id = 3
    workspace_id = 11
    visibility = "private"
    source_kind = "file"
    created_by = 42


def test_visual_payload_carries_the_same_tenant_fields_as_text_legs():
    """🔑 视觉点的租户字段必须与文本腿**逐字一致**（照 `_index_text_legs` 抄）。

    第一版把 `visibility` 写死成 `"workspace"`、`owner_id` 写死成 `None`。
    今天不构成越权（`build_qdrant_filter` 只按 `kb_id` 过滤，租户判定
    在 PG 侧已折叠成 `readable_kb_ids`），但 payload 撒谎会让
    §2.4 建的索引和将来任何"按 visibility 过滤"的改动全部错位。
    """
    from services.worker.handlers import _visual_payload

    pl = _visual_payload(_FakeSession({3: _Kb()}), _Doc(), 2)
    assert pl["workspace_id"] == 11, "workspace_id 不是从 KB 取的"
    assert pl["visibility"] == "private", "visibility 被写死了"
    assert pl["source_kind"] == "file", "source_kind 不是从 KB 取的"
    assert pl["owner_id"] == 42, "owner_id 被写死成 None 了"
    assert pl["chunk_index"] == -2, "页级点必须用负 chunk_index（三条腿靠这个符号分开）"
    assert pl["page"] == 2


def test_visual_payload_matches_text_leg_field_names():
    """两腿的 payload 字段名必须一致 —— `mime` 而不是 `mime_type` 之类。"""
    from services.ingest import chunker
    from services.worker.handlers import _visual_payload

    pl = _visual_payload(_FakeSession({3: _Kb()}), _Doc(), 1)
    text_leg = chunker.build_payload(
        chunker.Chunk(text="x"), chunk_index=0, kb_id=3, doc_id=7,
        workspace_id=11, visibility="private", source_kind="file",
        owner_id=42, file_name="a.pdf", mime_type="application/pdf",
    )
    # 文本腿有、视觉腿没有的：text_raw（页级没有正文可存）
    assert set(pl) - set(text_leg) == set()
    assert {"mime", "owner_id", "visibility", "workspace_id"} <= set(pl)


def test_visual_payload_raises_when_the_kb_is_gone():
    """KB 没了要报错，不能写一个 `workspace_id=None` 的点进去。"""
    from services.common.jobs import KIND_EMBED_VISUAL  # noqa: F401 — 确认常量在
    from services.ingest.pipeline import IngestError
    from services.worker.handlers import _visual_payload

    with pytest.raises(IngestError):
        _visual_payload(_FakeSession({}), _Doc(), 1)


def test_handlers_no_longer_reference_undefined_helpers():
    """🔑 防回归：视觉 handler 不许再**调用** `_workspace_of` / `_source_kind`。

    这两个函数**从来没有定义过**，但代码"看起来是对的"。
    它没暴露是因为前面还有一个更早的报错（processor 少传 `text=`）——
    **一个 bug 掩盖另一个 bug**，修掉第一个才会看见第二个。
    """
    code = _code_only(HANDLERS)
    for name in ("_workspace_of", "_source_kind"):
        assert name not in code, f"handlers 还在调不存在的 {name}（会 NameError）"


def test_every_bare_call_in_services_resolves_to_a_definition():
    """🔑 `services/` 里每个**裸函数调用**都要能在代码里找到定义。

    与"不许出现某某名字"相比，这条更通用：它能挡住**下一个**
    同形状的坑（抄一段看起来合理的代码，helper 忘了带过来）。

    ⚠️ 判据用 AST（不是文本），且**只看裸名调用**：
    - `obj.method()` 的接收者类型静态推不出来（`TermRegistry.ensure_loaded`
      是同步的、`TermRegistryAsync.ensure_loaded` 是协程的，同名）
    - 收集定义时把**所有作用域**的绑定都算上（参数、循环变量、
      嵌套函数、推导式变量…），否则满屏误报
    """
    import builtins

    def bound_names(tree: ast.AST) -> set[str]:
        out: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out.add(node.name)
                args = getattr(node, "args", None)
                if args is not None:
                    for a in (*args.args, *args.posonlyargs, *args.kwonlyargs):
                        out.add(a.arg)
                    if args.vararg:
                        out.add(args.vararg.arg)
                    if args.kwarg:
                        out.add(args.kwarg.arg)
            elif isinstance(node, ast.Lambda):
                a = node.args
                for arg in (*a.args, *a.posonlyargs, *a.kwonlyargs):
                    out.add(arg.arg)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for al in node.names:
                    out.add((al.asname or al.name).split(".")[0])
            elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                out.add(node.id)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                out.add(node.name)
        return out

    known = set(dir(builtins))
    missing: list[str] = []
    for path in sorted((ROOT / "services").rglob("*.py")):
        tree = ast.parse(_src(path), filename=str(path))
        local = known | bound_names(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id not in local:
                    missing.append(
                        f"{path.relative_to(ROOT)}:{node.lineno} {node.func.id}()"
                    )
    assert not missing, (
        "这些函数在本文件里没有任何定义（运行到就 NameError）:\n  "
        + "\n  ".join(missing)
    )


# ============================================================
# 3. 一页一个任务（enqueue 去重必须看 payload）
# ============================================================


class _FakeJobRow:
    def __init__(self, payload):  # noqa: ANN001
        self.payload = payload


class _FakeResult:
    def __init__(self, rows):  # noqa: ANN001
        self._rows = rows

    def scalars(self):  # noqa: ANN201
        return self

    def all(self):  # noqa: ANN201
        return self._rows


class _DedupSession:
    """只实现 `_find_unfinished` 用到的那一点：`execute(...).scalars().all()`。"""

    def __init__(self, rows):  # noqa: ANN001
        self.rows = rows
        self.added: list[object] = []
        self.flushed = 0

    def execute(self, stmt, *a, **k):  # noqa: ANN001, ANN002, ANN201
        return _FakeResult(self.rows)

    def add(self, obj):  # noqa: ANN001, ANN201
        self.added.append(obj)

    def flush(self):  # noqa: ANN201
        self.flushed += 1


def test_enqueue_dedup_compares_payload():
    """🔑 `enqueue` 的去重必须**包含 payload**。

    视觉腿是一页一个任务（`{"page": n}`）。第一版的去重只比
    `(doc_id, kind)`，于是 3 页扫描件只入队了**一个** `embed_visual` ——
    第 2、3 页永远不会被编码。
    ⚠️ 症状极隐蔽：任务状态全是 done、日志干净、无任何报错，
    只有"页数对不上"才看得出来，而页级索引当时根本没人在意。
    """
    from services.common.jobs import KIND_EMBED_VISUAL, enqueue

    # 已有第 1 页的未完成任务 → 入队第 1 页应复用，第 2 页应新建
    s = _DedupSession([_FakeJobRow({"page": 1})])
    again = enqueue(s, KIND_EMBED_VISUAL, doc_id=7, kb_id=3, payload={"page": 1})
    assert s.added == [], "同一个 page 重复入队了"

    nxt = enqueue(s, KIND_EMBED_VISUAL, doc_id=7, kb_id=3, payload={"page": 2})
    assert len(s.added) == 1, "第 2 页没有入队（去重把不同 payload 也当成同一个）"
    assert nxt.payload == {"page": 2}


def test_enqueue_dedup_treats_none_and_empty_payload_as_same():
    """`None` 与 `{}` 必须视为同一个任务（老数据的 `payload` 可能是 `{}`）。

    否则一次"入队两次"的判断会在两种写法之间来回摇摆，
    症状是**重复摄取**（编码白跑、df 翻倍）。
    """
    from services.common.jobs import KIND_INGEST, enqueue

    s = _DedupSession([_FakeJobRow({})])
    enqueue(s, KIND_INGEST, doc_id=7, kb_id=3)
    assert s.added == [], "None payload 没被当成与 {} 相同"


def test_enqueue_dedup_does_not_use_scalar_one_or_none():
    """🔑 一页一个任务之后，同一文档会**同时**有多条未完成的同类任务。

    那是正常状态，所以去重查询不能用 `scalar_one_or_none()` ——
    它会抛 `MultipleResultsFound`，而失败点在"入队"里，
    报错完全指不到真正原因。
    """
    from services.common.jobs import KIND_EMBED_VISUAL, enqueue

    s = _DedupSession([_FakeJobRow({"page": 1}), _FakeJobRow({"page": 2})])
    job = enqueue(s, KIND_EMBED_VISUAL, doc_id=7, kb_id=3, payload={"page": 1})
    assert s.added == []
    assert job.payload == {"page": 1}
    job3 = enqueue(s, KIND_EMBED_VISUAL, doc_id=7, kb_id=3, payload={"page": 3})
    assert len(s.added) == 1, "第三条页没入队"


def test_enqueue_async_awaits_its_lookup():
    """🔑 异步版必须 `await` 那个查找。

    漏了 `await` 时拿到的是 **coroutine 对象**，而
    `if existing is not None` 对 coroutine **恒为真** ——
    于是每次上传都"去重成功"、**一个任务都不入队**：
    worker 空转、文档永远 pending、端点照样返回 202。
    这是本项目在同一个坑上栽的第四次（前三次见
    `tests/test_unawaited_coroutines.py` 的 docstring）。
    """
    tree = ast.parse(_src(ROOT / "services" / "common" / "jobs.py"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "enqueue_async")
    looked_up = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Assign) and "_find_unfinished_async" in ast.unparse(n)
    ]
    assert looked_up, "enqueue_async 没有调用 _find_unfinished_async"
    for n in looked_up:
        assert isinstance(n.value, ast.Await), (
            f"jobs.py:{n.lineno} `_find_unfinished_async(...)` 少了 await —— "
            "coroutine 对象恒为真，于是所有上传都静默不入队"
        )


# ============================================================
# 4. 纯扫描件不能永远停在 pending
# ============================================================


def test_pure_scan_doc_is_pending_only_when_visual_jobs_queued():
    """🔑 `pending` 的前提是"**有**视觉任务在排队"。

    第一版无条件置 `pending`，而视觉任务失败时**没有任何地方**把它改回
    ready —— 文档永远停在"处理中"，前端
    （`KbDetailPage` 的 `pending` 判据）会一直轮询下去。
    用户看到的是"一直在处理"，而不是"处理失败了"。
    """
    src = _src(PIPELINE)
    assert 'doc.status = "pending" if (queue_visual and scan_pages) else "ready"' in src, (
        "纯扫描件的状态仍然与视觉任务是否入队无关"
    )


def test_scan_pages_is_persisted_for_reconciliation():
    """🔑 `documents.scan_pages` 必须落库 —— 它是页级索引唯一的 PG 真相来源。

    `is_scanned: bool` 说不出**页数**，没有它就无法对账视觉腿，
    而"无法对账"正是 D123 烂了这么久没人发现的根因。
    """
    assert "doc.scan_pages = scan_pages" in _src(PIPELINE), (
        "摄取时没有落库 scan_pages"
    )
    assert "scan_pages" in _src(ROOT / "services" / "common" / "models.py")


def test_ready_flip_is_judged_by_the_actual_point_count():
    """🔑 状态翻转的判据是 **Qdrant 里实际的点数**（不是"任务都 done 了"）。

    任务 done 只说明"没抛错"，而这一页的点到底在不在索引里才是事实。

    ⚠️ D127 起这段判定搬到了 `services/worker/doc_state.py` —— 因为只挂在
    `handle_visual` 的成功收尾上时，**全失败那条路径根本走不到收尾**
    （同一个症状的两半：成功不转 `ready` / 失败不转 `failed`）。
    判据本身没变，变的只是"谁来做这个判定"。
    """
    from services.worker.doc_state import JobView, decide_doc_state

    jobs = [JobView(id=1, kind="embed_visual", status="done", page=1)]
    common = dict(doc_status="pending", is_deleted=False, is_current=True,
                  scan_pages=[1], jobs=jobs)
    assert decide_doc_state(**common, point_count=1).status == "ready", (
        "点数齐了却没转 ready"
    )
    assert decide_doc_state(**common, point_count=0).status != "ready", (
        "点数为 0 也报 ready —— 那一页永远不会被检索到"
    )
    assert "visual_point_count(" in _src(DOC_STATE), (
        "终态判定没有查 Qdrant 的实际点数（判据回到「任务都 done 了」）"
    )


def test_vdr_switch_exists_and_is_documented():
    """`VDR_ENABLED=false` 必须是**诚实的开关**，不是静默降级。

    关掉时：worker 不加载 colSmol、扫描件不入队视觉任务、文档直接 ready。
    """
    from services.common.config import Settings

    assert Settings().vdr_enabled is True, "默认必须是开（关掉会静默丢功能）"
    src = _src(ROOT / "services" / "common" / "config.py")
    assert "vdr_enabled" in src


# ============================================================
# 5. 单图成本是常数（实测结论，别再用会过时的数字）
# ============================================================


def test_seconds_per_page_is_the_measured_value():
    """🔑 `VDR_SECONDS_PER_PAGE` 必须与实测一致（fp32 ≈15s/图）。

    ⚠️ 这不是"文档里的数字"问题：它直接决定 D37 的页数上限，
    而那个上限的用途是"别让用户等一小时"。
    跟 66s 那个过时值有关的教训见 PROGRESS §5.88。
    """
    from services.ingest.pipeline import MAX_SCAN_PAGES, VDR_SECONDS_PER_PAGE

    assert VDR_SECONDS_PER_PAGE == 15, (
        f"单页成本写成了 {VDR_SECONDS_PER_PAGE}s —— 与实测（fp32 ≈15s）不符"
    )
    # D37 的上限按这个常数算：50 页 ≈ 12 分钟
    assert MAX_SCAN_PAGES * VDR_SECONDS_PER_PAGE / 60 < 20


def test_stale_66s_figures_are_gone_from_code():
    """代码里的 "66s/页" 是**过时实测**（bf16 + 旧硬件），实测是 15s/图。

    留着会让下一个人按它做预算 —— 而它错了 4.6 倍
    （D37 的页数上限就是拿这种数字算的）。

    ⚠️ 判据用 `_code_only`（剥掉 docstring）：`pipeline.py` 里**故意**留着
    一句"当初按 66s/页 给的是 55 分钟"来说明上限为什么变宽松 ——
    那是历史记录，不是当前主张。
    """
    for path in (POOL, HANDLERS, PIPELINE, ROOT / "services" / "worker" / "main.py",
                 ROOT / "services" / "common" / "jobs.py",
                 ROOT / "services" / "common" / "db.py",
                 ROOT / "services" / "api" / "main.py",
                 ROOT / "services" / "ingest" / "locations.py"):
        assert "66s" not in _code_only(path), (
            f"{path.name} 里还有过时的 66s/页（实测 15s/图）"
        )
