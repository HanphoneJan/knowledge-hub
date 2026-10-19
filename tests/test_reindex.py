"""🔑 重建索引（`KIND_REINDEX`）的关键性质。

这些性质都是**实测踩出来的**，所以钉死：

| 性质 | 不遵守的后果 |
|---|---|
| `reset_terms` 全局只调一次 | 前面几个库的词表被反复抹掉，`n_docs` 只剩最后一个库（实测 450 → 77） |
| 重建时 `_index_text_legs(index_terms=False)` | 词表登记两次，df 翻倍 → idf 变负，检索质量静默变差 |
| 重建过滤软删文档 | 搜得到回收站里的内容（正是 D7 要避免的） |
| 重建写**完整** payload | `/search` 的 `text`（D77）与 `locations`（L3）消失 |
| 对账**分腿** | 只有一条腿坏了报成"一致"（并集口径的盲点） |

前四条只能做源码级断言（都要真 DB + Qdrant 才有意义，
真跑的版本在 `eval/api_1d_check.py` §11）；最后一条有真值可测。
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HANDLERS = ROOT / "services" / "worker" / "handlers" / "__init__.py"
PIPE = ROOT / "services" / "ingest" / "pipeline.py"
CHUNKER = ROOT / "services" / "ingest" / "chunker.py"


def _fn_src(path: Path, name: str) -> str:
    """取某个函数的源码（用 AST 定位，避免字符串匹配整文件）。"""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.unparse(node)
    raise AssertionError(f"{path.name} 里没有函数 {name}")


def _fn_node(path: Path, name: str) -> ast.FunctionDef:
    """取函数的 AST 节点（`ast.parse` 顶层得到的是 Module，不是 FunctionDef —— 踩过）。"""
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 里没有函数 {name}")


def _calls(fn_src: str, callee: str) -> list[ast.Call]:
    tree = ast.parse(fn_src)
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and ast.unparse(n.func).endswith(callee)
    ]


# ============================================================
# 1. 词表是全局量：reset_terms 只能调一次，且必须在 per-KB 循环外
# ============================================================


def test_reset_terms_called_exactly_once():
    """`terms.df` / `corpus_stats` 是全局量（不带 kb_id）。

    放进 per-KB 循环 → 每处理一个库就抹掉前面登记的，实测 `n_docs` 从 450
    变成 77（只剩最后一个库）。所以"只调一次"是硬性质。
    """
    fn = _fn_src(HANDLERS, "handle_reindex")
    assert len(_calls(fn, "reset_terms")) == 1, (
        f"handle_reindex 里 reset_terms 调了 {len(_calls(fn, 'reset_terms'))} 次"
    )


def test_reset_terms_is_not_inside_a_kb_loop():
    """更精确的一条：它不能在任何 `for kb_id in ...` 循环体里。"""
    tree = ast.parse(_fn_src(HANDLERS, "handle_reindex"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.For):
            continue
        body = ast.unparse(node)
        if "reset_terms" in body:
            raise AssertionError("reset_terms 落在循环体里 —— 前面几个库会被抹掉")


# ============================================================
# 2. 重建不重复登记词表
# ============================================================


def test_reindex_passes_index_terms_false():
    """`register_chunks` 是"加 df"。重建时词表已登记过一遍，再登记就是双倍。

    df 翻倍 → idf 变负 → 检索被噪声淹没，而且**不报错**（最坏的失败模式）。
    """
    fn = _fn_src(HANDLERS, "handle_reindex")
    calls = _calls(fn, "_index_text_legs")
    assert calls, "handle_reindex 没调 _index_text_legs"
    for c in calls:
        kw = {k.arg: ast.unparse(k.value) for k in c.keywords}
        assert kw.get("index_terms") == "False", (
            f"重建时必须 index_terms=False，实际 {kw}"
        )


def test_index_terms_defaults_to_true():
    """入库那条路径必须**照常**登记词表 —— 默认值错了会让正常摄取不更新 df。"""
    args = _fn_node(PIPE, "_index_text_legs").args
    # ⚠️ kw_defaults 是**右对齐**的（有默认值的只是最后几个），
    #   而且 zip 出来的 key 是 AST 节点不是名字 —— 两处都踩过。
    n_def = len(args.kw_defaults)
    pairs = {
        a.arg: d
        for a, d in zip(args.kwonlyargs[len(args.kwonlyargs) - n_def:], args.kw_defaults)
    }
    assert "index_terms" in pairs, pairs.keys()
    assert ast.literal_eval(pairs["index_terms"]) is True, (
        "index_terms 默认必须是 True（入库要登记词表）"
    )


# ============================================================
# 3. 重建的其它两条性质
# ============================================================


def test_reindex_skips_soft_deleted_documents():
    """软删（回收站里）的文档**不重建** —— 否则搜得到已删内容（D7 要避免的）。"""
    fn = _fn_src(HANDLERS, "handle_reindex")
    assert "is_current" in fn and "deleted_at" in fn, (
        "重建没有过滤 is_current / deleted_at"
    )


def test_reindex_rebuilds_both_text_legs():
    """只重建稀疏腿的话，dense 侧的漂移**没有任何修复路径**（老实现就是这样）。"""
    fn = _fn_src(HANDLERS, "handle_reindex")
    assert "COLL_DENSE" in fn and "COLL_SPARSE" in fn


def test_reindex_orphan_cleanup_covers_all_three_legs():
    """🔑 reindex **不重建**视觉腿，但**孤儿清理必须覆盖它**。

    两条规则是分开的，理由不同：

    | 动作 | 覆盖 | 理由 |
    |---|---|---|
    | 按库先删后建（**逐个库**，D179） | dense + sparse | 删完一定会建回来 |
    | 孤儿清理 | **三条腿** | 库已不在 PG 里 → 那些点**永远**建不回来，是真垃圾 |

    ⚠️ 孤儿清理漏掉视觉腿的后果是实测的：加了「视觉腿对账」巡检项之后
    **第一次跑**就报"多 8"，查出来正是 eval 硬删 photo 库留下的页级点
    （D116 那类"级联硬删从不碰索引"的残留，视觉腿也要清）。
    """
    src = (ROOT / "services" / "worker" / "handlers" / "__init__.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(src)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "handle_reindex")

    def _in(fn_, name: str) -> bool:
        return any(
            isinstance(n, ast.Name) and n.id == name for n in ast.walk(fn_)
        )

    #: 父节点表 —— 用来找**最近的**外层 for（`ast.walk` 会同时命中内外两层）
    parent: dict[ast.AST, ast.AST] = {}
    for outer in ast.walk(fn):
        for child in ast.iter_child_nodes(outer):
            parent[child] = outer

    def _enclosing_iter_src(node: ast.AST) -> str:
        """这个 `for coll in (...)` **最近**的外层 for 迭代的是什么。

        用来区分「按 targets 删」与「按 orphan_kbs 删」——
        两者的内层 `for coll in (...)` 文本可能一模一样，只看内层会认错。
        """
        cur = parent.get(node)
        while cur is not None:
            if isinstance(cur, ast.For) and cur is not node:
                return ast.unparse(cur.iter)
            cur = parent.get(cur)
        return ""

    # ---- ① 孤儿清理：传给 `_orphan_kb_ids_in_qdrant` 的集合要含 COLL_VISUAL ----
    #   ⚠️ 判据是"实参的内容"，不是"函数里出现过这个名字"：
    #   曾经写成 `assert "COLL_VISUAL" in src` —— 那个版本把代码改回去
    #   测试照样绿（因为 import 与按库删除那两处仍有这个名字）。
    orphan_args = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and ast.unparse(n.func).endswith("_orphan_kb_ids_in_qdrant")
    ]
    assert orphan_args, "reindex 没有清孤儿点"
    for call in orphan_args:
        arg = ast.unparse(call.args[0]) if call.args else ""
        assert "COLL_VISUAL" in arg, f"孤儿清理漏了视觉腿：{arg}"

    # ---- ③ 孤儿点的**删除**循环也要带视觉腿 ----
    #   ⚠️ 按"外层循环变量"区分两个删除循环：
    #   按库删除现在在 `for ... in enumerate(todo, …)` 里（D179 改成按库逐个删），
    #   孤儿删除是 `for kb_id in sorted(orphan_kbs)`
    #   —— 两者的内层 `for coll in (...)` 文本可能一模一样，
    #   只看内层会把按库删除那个也算进来，然后要求它带视觉腿（那是错的，见 ④）。
    def _coll_iters(outer_src: str) -> list[str]:
        return [
            ast.unparse(n.iter) for n in ast.walk(fn)
            if isinstance(n, ast.For)
            and isinstance(n.iter, (ast.Tuple, ast.List))
            and isinstance(n.target, ast.Name) and n.target.id == "coll"
            and any(
                isinstance(s, ast.Call)
                and ast.unparse(s.func).endswith("delete_by_filter")
                for s in ast.walk(n)
            )
            # 外层循环的迭代对象决定这是"按库"还是"按孤儿"
            and _enclosing_iter_src(n) == outer_src
        ]

    orphan_del = _coll_iters("sorted(orphan_kbs)")
    assert orphan_del, "没有找到孤儿点的删除循环"
    for it in orphan_del:
        assert "COLL_VISUAL" in it, f"孤儿点删除漏了视觉腿：{it}"

    # ---- ④ 但**按库删除**必须**不含**视觉腿 ----
    #   🔑 "不重建"必须连"不删"一起：删了建不回来就是数据没了，
    #   那比漂移更糟。本轮真的踩过 —— 给删除循环加上 COLL_VISUAL 之后，
    #   一次 reindex 把 25 个页级点全删了，视觉腿从有数据变成 0 点。
    #   ⚠️ D179 后按库删除在 `enumerate(todo, …)` 那个循环里（切库时删当前库）。
    per_kb_iters = _coll_iters("enumerate(todo, start=1)")
    assert per_kb_iters, (
        "找不到按库删除的循环（D179 后它应在 enumerate(todo, …) 内、切库时执行）"
    )
    for it in per_kb_iters:
        assert "COLL_VISUAL" not in it, (
            f"按库删除里出现了视觉腿：{it} —— 它不会被重建，删了就是数据没了（D124）"
        )
        assert "COLL_DENSE" in it and "COLL_SPARSE" in it, (
            f"按库删除应该覆盖两条文本腿：{it}"
        )

    # ---- ⑤ 视觉腿**不重建**（贵 100 倍，且那是"重新摄取"） ----
    assert not _in(fn, "_visual_payload"), "reindex 不该重建视觉腿"
    assert not _in(fn, "encode_images"), "reindex 不该调 colSmol 编码"


def test_reindex_deletes_per_kb_not_all_targets_first():
    """🔑 D179：删除**按库、紧挨它自己的重建**做，不是先把所有目标库删光。

    原实现先把**所有**目标库的点删掉再逐个重建 ⇒ 多库部署一次全量重建时
    **所有**库一起空窗（100 万 chunk 实测 ~17ms/chunk ⇒ 约 4.7 小时搜不到）。
    按库逐个做，任一时刻只有一个库在空窗。

    **变异**：把删除挪回"先删所有库"，或去掉按 kb 排序，这条都会红。
    """
    fn = _fn_src(HANDLERS, "handle_reindex")
    # ① 必须按 kb_id 排序（否则同一个库分散，切库删除会抹掉刚建好的点）
    assert "todo.sort(" in fn and "r[1]" in fn, (
        "todo 没按 kb_id 排序 —— 同一库会分散，切库时会把刚重建的点删掉"
    )
    # ② 按库删除必须在重建循环**里**，不能又变成"先删所有库"
    build_start = fn.index("enumerate(todo")
    assert "delete_by_filter" in fn[build_start:], "按库删除没有跟着每个库做"
    assert "for kb_id in targets" not in fn[:build_start], (
        "又变回『先把所有目标库删光』了（D179）—— 那会让所有库一起空窗"
    )


def test_docref_carries_every_field_index_text_legs_reads():
    """🔑 `_DocRef` 的字段必须覆盖 `_index_text_legs` 实际读的每一个 `doc.*`。

    `_DocRef` 是 reindex 重建向量时用的文档替身（避免访问惰性关系 `doc.kb`，
    D124）。它**不是** `Document`，所以少一个字段不会在构造时被发现 ——
    只会在跑到那行时炸。本轮实测连炸两次（`file_name` → `mime_type`），
    每次都要跑一次完整 reindex 才发现。

    ✅ 用 AST 读 `_index_text_legs` 里出现的 `doc.<attr>`，
    再断言 `_DocRef.__slots__` 覆盖它 —— 将来多读一个字段这里立刻红。
    """
    import inspect

    from services.ingest.pipeline import _index_text_legs
    from services.worker.handlers import _DocRef

    tree = ast.parse(
        (ROOT / "services" / "ingest" / "pipeline.py").read_text(encoding="utf-8")
    )
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_index_text_legs")
    used = {
        n.attr for n in ast.walk(fn)
        if isinstance(n, ast.Attribute)
        and isinstance(n.value, ast.Name) and n.value.id == "doc"
    }
    assert used, "没解析出任何 doc.* —— 判据本身坏了"
    have = set(_DocRef.__slots__)
    missing = used - have
    assert not missing, (
        f"_DocRef 缺字段 {sorted(missing)}（_index_text_legs 会读 doc.{sorted(missing)[0]}）"
    )
    # 反向：多带的字段是死重量，也会误导读代码的人
    assert not (have - used), (
        f"_DocRef 多带了字段 {sorted(have - used)}（没人读它）"
    )
    # 🔑 构造参数**个数**要与 __slots__ 一致，且每个字段都被赋值 ——
    #   `__slots__` 与 `__init__` 不同步时，`__slots__` 会**静默失效**
    #   （类重新拿到 __dict__，漏赋的字段要运行到那行才 AttributeError）。
    #   这正是本轮踩的形状，所以两条都要钉。
    params = list(inspect.signature(_DocRef.__init__).parameters)[1:]
    assert len(params) == len(have), (
        f"__init__ 有 {len(params)} 个参数，__slots__ 有 {len(have)} 个 —— 不同步"
    )
    assigned = {
        n.attr for n in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(_DocRef.__init__))))
        if isinstance(n, ast.Attribute)
        and isinstance(n.value, ast.Name) and n.value.id == "self"
        and isinstance(n.ctx, ast.Store)
    }
    assert assigned == have, f"__init__ 没给每个 __slots__ 赋值：{sorted(have - assigned)}"
    # 每个字段都要真的可读（漏赋值的话这里就 AttributeError）
    inst = _DocRef(1, "a.pdf", "application/pdf")
    assert (inst.id, inst.file_name, inst.mime_type) == (1, "a.pdf", "application/pdf")


def test_reindex_does_not_use_lazy_kb_relationship():
    """🔑 重建循环不许访问 `doc.kb` —— 那是个**时间窗炸弹**。

    实测（并行跑 eval 与 web e2e）：e2e 的清场删掉了 1d 的库，
    而 reindex 已经跑了一会儿，`d.kb` 变成 `None` →
    `AttributeError: 'NoneType' object has no attribute 'id'`，
    **整个重建任务失败**（词表已经提交，回滚不了）。

    ✅ 所以 `live_rows` 用一条 SQL 同时取文档与 `kb_id`，
    重建循环里显式查 `kb` 并按 `kb_id` 缓存。
    """
    src = (ROOT / "services" / "worker" / "handlers" / "__init__.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(src)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "handle_reindex")
    lazy_kb = [
        ast.unparse(n) for n in ast.walk(fn)
        if isinstance(n, ast.Attribute) and n.attr == "kb"
        and isinstance(n.value, ast.Name) and n.value.id in ("d", "doc")
    ]
    assert not lazy_kb, (
        f"reindex 里还在访问惰性关系 {lazy_kb} —— 库在重建期间被删就会崩"
    )
    # 必须显式查 kb（而不是假设它一定在）
    assert "session.get(KnowledgeBase" in src, "重建循环没有显式查 KnowledgeBase"


def test_reindex_reports_visual_leg_is_skipped():
    """视觉腿不重建是**取舍**，必须在源码里说清楚（D72：不许静默不做）。"""
    doc = (HANDLERS.read_text(encoding="utf-8"))
    i = doc.index("def handle_reindex(")
    j = doc.index("HANDLERS: dict[str, Handler]", i)
    assert "kb_visual" in doc[i:j] or "视觉腿" in doc[i:j]


# ============================================================
# 4. 对账分腿
# ============================================================


def test_qdrant_scan_takes_collections():
    """分腿 = 扫描函数必须接受 collection 列表。

    只有一个集合参数 = 只扫一条腿 = 那条腿的问题看得见、另一条看不见。
    """
    # ⚠️ D143：取点器搬到 `common/consistency.qdrant_point_ids`
    #   （端点 / 巡检 / worker 共用那一份），端点现在只调用它。
    node = _fn_node(ROOT / "services" / "common" / "consistency.py",
                    "qdrant_point_ids")
    params = [a.arg for a in node.args.args]
    assert params[:2] == ["collections", "kb_ids"], params


def test_report_has_per_leg_breakdown():
    """`legs` 字段 + `LegReport`：只有一条腿坏了也要看得见（并集口径的盲点）。"""
    from services.common.schemas_ops import ConsistencyReport, LegReport

    assert "legs" in ConsistencyReport.model_fields
    for f in ("collection", "points", "missing", "dangling", "consistent"):
        assert f in LegReport.model_fields, f

    r = ConsistencyReport(
        kb_ids=[1], pg_chunks=74, qdrant_points=147,
        legs={
            "kb_dense": LegReport(collection="kb_dense", points=73, missing=[7],
                                  missing_count=1, consistent=False),
            "kb_sparse": LegReport(collection="kb_sparse", points=74,
                                   consistent=True),
        },
        consistent=True,  # 并集口径：dense 少一个、sparse 补上 → 仍"一致"
    )
    assert r.legs["kb_dense"].consistent is False
    assert r.legs["kb_sparse"].consistent is True
    assert r.consistent is True, "顶层是并集口径 —— 单腿缺失只能从 legs 看"


# ============================================================
# 5. chunker.from_rows：ORM 行 → chunker.Chunk
# ============================================================


def test_from_rows_maps_orm_fields():
    """字段名不对齐：ORM 行是 `content_norm`，`chunker.Chunk` 是 `text`。

    直接把 ORM 行当 Chunk 传会 `AttributeError` —— 重建索引第一版就踩了。
    """
    from services.ingest.chunker import Chunk, from_rows

    class Row:
        content_norm = "第 12 条 备注栏不要填"
        kind = "table"
        locations = [{"page": 3, "bbox": [0.1, 0.2, 0.3, 0.4]}]
        table_index = 1
        row_count = 9
        col_count = 7
        table_json = {"header": ["a"], "rows": [["1"]], "row_count": 2, "col_count": 1}
        char_start = 100
        char_end = 140
        token_count = 12

    out = from_rows([Row()])
    assert len(out) == 1
    c: Chunk = out[0]
    assert c.text == Row.content_norm, "必须用归一化文本（与入库时一致）"
    assert c.kind == "table"
    assert c.page == 3, "页码取第一个坐标"
    assert (c.row_count, c.col_count) == (9, 7)
    # D203：重建必须带上结构化网格，否则"重建"会把表格 JSON 丢掉（§5.178 同族）
    assert c.table_json == Row.table_json
    assert (c.char_start, c.char_end, c.token_count) == (100, 140, 12)


def test_from_rows_defaults_for_sparse_rows():
    """脏行（没有 locations / kind）不该让重建整个失败 —— 一行坏掉重建就白跑。"""
    from services.ingest.chunker import from_rows

    class Row:
        content_norm = "只有正文"

    c = from_rows([Row()])[0]
    assert c.text == "只有正文"
    assert c.kind == "text" and c.locations == [] and c.page == 1
    assert c.table_index is None and c.row_count is None


def test_from_rows_handles_empty_input():
    from services.ingest.chunker import from_rows

    assert from_rows([]) == []