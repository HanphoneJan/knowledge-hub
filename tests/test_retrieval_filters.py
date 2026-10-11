"""🔑 防回归：**检索层不许自己拼租户 filter**（CONVENTIONS §0.2）。

这条约束的价值在本项目已经被证明过一次：`build_qdrant_filter` 之所以是
"唯一入口"，正是因为漏一处的后果**测不出来** —— 只有一个用户时隔离逻辑恒真（D54）。

本文件把约束从"文档里的约定"变成"CI 里的断言"：

1. 只有 `auth.py` 允许出现 Qdrant filter 的字面量
2. 每条腿的查询**必须**带 filter（漏了就是全库可读）
3. 软删除的二次过滤不可省（PG 已删但 Qdrant 还能搜到 = 数据泄漏）
4. `services/` 不得 import `pipeline/` / `index/`
5. trgm 腿必须 join 到 `knowledge_bases` 才能做租户判定
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from services.common.auth import build_qdrant_filter
from services.common.qdrant import get_client

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ROOT / "services"


def _py_files(sub: str | None = None) -> list[Path]:
    base = SERVICES / sub if sub else SERVICES
    return sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)


# ============================================================
# 1. filter 字面量只能出现在 auth.py
# ============================================================


#: 允许出现 filter 字面量的地方，以及**为什么**。
#:
#: ⚠️ 这里列出来的每一项都要能回答"它为什么不需要租户判定"。
#: 加新条目时把这个理由一起写进 commit message —— 否则这个白名单
#: 会慢慢变成"哪里都能拼 filter"的破洞。
_FILTER_LITERAL_ALLOWLIST = {
    "common/auth.py": "唯一入口本身（CONVENTIONS §0.2）",
    # reindex 任务删掉**自己那个 kb** 的全部稀疏点后重建。
    # kb_id 来自 job 的 payload（`jobs.kb_id`），不是请求方指定的，
    # 也不是"读"路径 —— 不存在把别人的库删了的可能。
    # 范围是"重建整个库"，不是"检索库里的东西"。
    "worker/handlers/__init__.py": "reindex 的按库删除，非读路径、kb_id 来自 job payload",
}


def _code_with_dict_key(path: Path, key: str) -> list[int]:
    """返回**真实代码**（排除 docstring 与注释）里出现 dict key `key` 的行号。

    用 AST 而不是字符串匹配 —— 否则模块 docstring 里为了说明
    "这里不该出现 `{"key": "kb_id"}`"而写的反例，会把自己告发。
    搜索/评测脚本里的文档字符串提到这个形状是完全正常的。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for k in node.keys:
            # 跳过 **dict（k=None）与 *dict 展开
            if isinstance(k, ast.Constant) and k.value == key:
                lines.append(node.lineno)
    return lines


def test_only_auth_py_builds_qdrant_filters():
    """🔑 任何**不是**白名单的文件都不许在代码里拼 Qdrant filter。"""
    offenders: list[str] = []
    for path in _py_files():
        rel = path.relative_to(SERVICES).as_posix()
        if rel in _FILTER_LITERAL_ALLOWLIST:
            continue
        hits = _code_with_dict_key(path, "kb_id")
        # 只在「值是个 dict 且里面有 match」时才算 filter —— 单纯的
        # payload 组装 `{"kb_id": 1}` 是允许的（写入路径需要它）
        if hits:
            text = path.read_text(encoding="utf-8")
            for ln in hits:
                snippet = text.splitlines()[ln - 1]
                if "match" in snippet or "must" in snippet:
                    offenders.append(f"{rel}:{ln}: {snippet.strip()}")
    assert not offenders, (
        "这些文件在拼 Qdrant filter，必须改成调 auth.build_qdrant_filter"
        f"（CONVENTIONS §0.2）:\n  " + "\n  ".join(offenders)
    )


def test_allowlist_entries_still_exist():
    """白名单里的文件不能被删掉/改名 —— 否则这条测试会**假绿**。"""
    for rel, why in _FILTER_LITERAL_ALLOWLIST.items():
        assert (SERVICES / rel).exists(), (
            f"白名单条目 {rel} 不存在了（{why}）。若已重构请清理白名单，"
            "否则这条测试会因为路径匹配不上而失去意义"
        )


def test_every_leg_passes_a_filter_to_qdrant():
    """🔑 每条查 Qdrant 的腿都必须把 filter 传给请求体。

    实测过"漏一个字段"的失败方式：Qdrant **不报错**，
    只是返回全库的结果 —— 单用户时完全看不出来（D54）。
    """
    for name in ("dense", "sparse", "visual"):
        src = (SERVICES / "retrieval" / f"{name}.py").read_text(encoding="utf-8")
        # 请求体里必须有 "filter": flt
        assert re.search(r'"filter"\s*:', src), f"{name}.py 没给 Qdrant 传 filter"
        # 且必须来自 build_qdrant_filter
        assert "build_qdrant_filter(" in src, f"{name}.py 没用 build_qdrant_filter"


def test_trgm_leg_uses_build_kb_scope_sql():
    """🔑 trgm 腿在 Postgres 里，必须用 SQL 版的租户过滤。

    它**必须** join 到 `knowledge_bases` —— 可见性判定需要 `created_by`
    与 `workspace_id`，光看 `chunks` / `documents` 拿不到。
    """
    src = (SERVICES / "retrieval" / "trgm.py").read_text(encoding="utf-8")
    assert "build_kb_scope_sql(" in src
    assert re.search(r"\.join\(KnowledgeBase,", src), (
        "trgm 腿没 join knowledge_bases → 无法判定 workspace/global 可见性"
    )


def test_retrieval_modules_never_recompute_visibility():
    """🔑 检索层不许**判定**可见性（`==` / `!=` / `in` 与 visibility 字面量比较）。

    三档可见性的判定**只**在 `auth.load_user_context` 与
    `routers/kb.py::_visible_kb_filter` 两处。检索层再写一遍就是
    "两处复刻可见性逻辑" —— CONVENTIONS §0.6 那个真实越权的成因
    （而且那个 bug 的二次错误更隐蔽：加了限定分支却写成另一个 `or`，完全无效）。

    用 AST 判**比较**而不是判"出现"：`knowledge_bases.visibility` 作为
    SELECT 列被读出来放进 payload（供前端显示）是合理的，
    但 `payload["visibility"] == "workspace"` 这种判定就是越权风险。
    """
    for path in _py_files("retrieval"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            # 左值是 visibility 的访问（含下标与属性）
            left = node.left
            mentions_visibility = (
                (isinstance(left, ast.Subscript) and _subscript_is(left, "visibility"))
                or (isinstance(left, ast.Attribute) and left.attr == "visibility")
            )
            if not mentions_visibility:
                continue
            # 比较对象是字符串字面量 → 是可见性判定
            literals = [
                c.value
                for c in node.comparators
                if isinstance(c, ast.Constant) and isinstance(c.value, str)
            ]
            if literals:
                pytest.fail(
                    f"{path.relative_to(ROOT)}:{node.lineno} 判定了可见性 "
                    f"{literals}。\n"
                    "租户范围只能来自 auth.build_qdrant_filter / build_kb_scope_sql，"
                    "检索层重写一遍可见性就是 CONVENTIONS §0.6 那个越权的成因"
                )


def _subscript_is(node: ast.Subscript, key: str) -> bool:
    sl = node.slice
    return isinstance(sl, ast.Constant) and sl.value == key


# ============================================================
# 2. 依赖方向（DESIGN-MODULES §2）
# ============================================================

#: 🔴 D157 起允许存在的顶层目录。v1 遗留的 `service/` `index/` `pipeline/`
#: 已删除 —— 它们查的 `mem_text` / `mem_visual` collection 早就不存在，
#: 逻辑也已被 `services/` 取代（逐条证据见 `PLAN.md` §3）。
_ALLOWED_TOP_DIRS = frozenset({
    "services", "web", "tests", "eval", "deploy", "docs",
    "migrations", "data", "reports", "qdrant_data",
})


def _foreign_top_dirs(root: Path) -> set[str]:
    """`root` 底下**不在白名单里**的顶层目录（不含 `.`/`__` 开头的）。

    🔑 抽成纯函数是为了能被合成输入测：判据是"**这个计算真的成立**"，
    而第一版把计算写在测试体里，变异脚本把计算换成 `found = set()`
    时测试照样全绿 —— 那等于"守卫被清空了，而它报告自己是干净的"。
    """
    return {
        p.name
        for p in root.iterdir()
        if p.is_dir() and p.name not in _ALLOWED_TOP_DIRS
        and not p.name.startswith((".", "__"))
    }


def _foreign_top_imports(files: list[Path], root: Path) -> list[str]:
    """这些文件里 import 了**本仓顶层目录**（且那目录不在白名单）的行。

    同样是纯函数：`root` 与 `files` 都是参数 ⇒ 能用合成输入测。
    ⚠️ `migrations/` 不在扫描范围内（按 alembic 的方式运行，
    且 `table_index` 这类词不是包名 —— 真模块名全小写）。
    """
    offenders: list[str] = []
    for path in files:
        rel = path.relative_to(root)
        if not rel.parts or rel.parts[0] != "services":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            for name in names:
                head = name.split(".")[0]
                if head in ("services", "eval", "deploy", "tests"):
                    continue
                # 第三方与标准库不在范围内：靠"不是本仓顶层目录"区分
                if (root / head).exists() and head not in _ALLOWED_TOP_DIRS:
                    offenders.append(f"{rel}: {name}")
    return offenders


def test_no_second_implementation_directory_came_back():
    """🔑 顶层目录只有白名单里的那些 —— v1 遗留目录不得复活。

    ⚠️ D157 之前这条守卫是"`services/` 不得 import `pipeline/` / `index/`"。
    那三个目录删掉之后它**永远不会红**（死守卫）—— 而"死守卫比没有守卫更糟"：
    它让人以为这条约束还守着，其实什么都拦不住（§5.104）。
    ⇒ 改成白名单：新增任何顶层目录（尤其又一份实现）都会被抓住。
    """
    # 🔑 **把白名单本身钉死** —— 只判"当前没有白名单外的目录"是不够的：
    #   有人把 `pipeline` 加进白名单时这一条照样全绿（那正是它要防的事）。
    #   ⚠️ 我第一版的"建出白名单全部目录再断言为空"也抓不到 ——
    #   白名单多一项，tmp 里也多建一个，于是**照样为空**（判据落空，§5.114）。
    #   ⇒ 判据就是白名单的**确切内容**。
    assert _ALLOWED_TOP_DIRS == frozenset({
        "services", "web", "tests", "eval", "deploy", "docs",
        "migrations", "data", "reports", "qdrant_data",
    }), f"顶层目录白名单被改了：{sorted(_ALLOWED_TOP_DIRS)}\n" \
        "加一项之前先问：它是不是**第二套实现**？（D157 删掉的三个目录就是）"

    found = _foreign_top_dirs(ROOT)
    assert not found, (
        f"顶层出现了白名单之外的目录：{sorted(found)}\n"
        "顶层不该再有第二套实现（`service/` `index/` `pipeline/` 已于 D157 删除）。\n"
        "如果是新的**工具**目录，先想清楚它该放 `deploy/` 还是 `eval/`；\n"
        "如果是新的**实现**目录，那是**第二套实现** —— 比重复的代码更难查。"
    )


def test_services_only_import_sibling_service_packages():
    """🔑 `services/` 只能依赖 `services/` 内部的包（外加第三方与标准库）。

    ⚠️ 这是 D157 前那条"`services/` 不得 import 工具层"的**加强版**：
    旧版只能拦住 `pipeline.` / `index.` 这两个**已知**名字 ——
    有人新写一个顶层包 `foo/` 再 import 它，旧版照样绿。
    ⇒ 现在按"顶层目录白名单"判，**新出现的顶层包**也会被拦住。

    `migrations/` 不在扫描范围内：它们按 alembic 的方式运行，
    且 `table_index` / `col_count` 这类词不是包名（真模块名全小写）。
    """
    offenders = _foreign_top_imports(_py_files(), ROOT)
    assert not offenders, (
        "services/ 依赖了本仓顶层目录（运行时只有一棵依赖树，DESIGN-MODULES §2）:\n  "
        + "\n  ".join(offenders)
    )


def test_retrieval_is_the_only_place_qdrant_is_queried():
    """🔑 检索只在 `services/retrieval/` 实现（CONVENTIONS §0.3）。

    API / MCP / A2A 三方共用一个实现 —— 否则半年后
    "API 搜得到的东西 MCP 搜不到"，这类不一致极难排查。
    评测脚本 `eval/` 是例外。
    """
    allowed = {"retrieval/qdrant_queries_are_mine.py"}
    offenders: list[str] = []
    for path in _py_files():
        rel = path.relative_to(SERVICES).as_posix()
        if rel.startswith("retrieval/"):
            continue
        text = path.read_text(encoding="utf-8")
        # 直接打 Qdrant 的检索端点（不是 collection 管理、不是写入）
        if re.search(r"/points/(query|search)", text):
            offenders.append(rel)
    assert not offenders, (
        "这些文件直接查了 Qdrant，检索实现必须只在 services/retrieval/:\n  "
        + "\n  ".join(offenders)
    )


# ============================================================
# 3. 软删除二次过滤
# ============================================================


def test_engine_drops_soft_deleted_documents():
    """🔑 PG 已删但 Qdrant 还能搜到 = 真正的数据泄漏。

    D7 明确允许 PG 与 Qdrant 短暂不一致，所以**必须**在检索侧二次过滤。
    """
    src = (SERVICES / "retrieval" / "engine.py").read_text(encoding="utf-8")
    assert "is_current" in src, "engine 没查 is_current"
    assert "deleted_at" in src, "engine 没过滤 deleted_at"
    assert "_drop_deleted" in src, "engine 没有软删除二次过滤步骤"


# ============================================================
# 4. 运行时行为（需要 Qdrant 在线）
# ============================================================


def test_build_qdrant_filter_always_returns_a_constraint():
    """🔑 filter 永远至少有一条约束。空 filter = 不过滤 = 全库可读。"""
    from services.common.auth import UserContext

    ctx = UserContext(
        user_id=1, display_name="u", email="u@t.local", api_key_id=None,
        readable_kb_ids=frozenset(),
    )
    assert build_qdrant_filter(ctx)["must"], "空授权范围也要有约束（恒假）"
    assert build_qdrant_filter(ctx, 999)["must"], "无权限的 kb_id 也要有约束"
    ctx2 = UserContext(
        user_id=1, display_name="u", email="u@t.local", api_key_id=None,
        readable_kb_ids=frozenset({5}),
    )
    assert build_qdrant_filter(ctx2)["must"]
    assert build_qdrant_filter(ctx2, 5)["must"]


@pytest.mark.parametrize("coll", ["kb_dense", "kb_sparse", "kb_visual"])
def test_collections_exist_and_have_payload_indexes(coll: str):
    """🔑 三个 collection 与 payload 索引必须在（CONVENTIONS §2.4）。

    `kb_id` 缺索引的后果**不是报错而是全表扫描**（§5.14）。
    """
    from services.common.qdrant import payload_schema

    try:
        schema = payload_schema(coll)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Qdrant 不可用: {exc}")
    assert schema, f"{coll} 没有 payload 索引"
    for field in ("kb_id", "doc_id", "workspace_id"):
        assert field in schema, f"{coll} 缺 payload 索引 {field}（会导致全表扫描）"


# ============================================================
# 2b. 上面两个守卫的**计算本身**也要能测（合成输入）
# ============================================================
# 🔑 为什么单开一节：判据写在测试体里时，"把计算换掉"这种变异抓不到
# —— 守卫会安静地报告"一切正常"。第一版就是这么被骗过去的
# （`found = set()` 那条变异全绿）。⇒ 计算抽成纯函数后用**合成输入**验它。


def test_foreign_top_dirs_detector_reports_a_planted_directory(tmp_path):
    """🔑 白名单之外的顶层目录**必须被报出来**（合成输入）。"""
    (tmp_path / "services").mkdir()
    (tmp_path / "pipeline").mkdir()
    (tmp_path / ".venv").mkdir()          # 点开头的：不是顶层目录
    (tmp_path / "notadir.txt").write_text("x")
    found = _foreign_top_dirs(tmp_path)
    assert found == {"pipeline"}, found


def test_foreign_top_dirs_detector_covers_the_whole_allowlist(tmp_path):
    """🔑 白名单里的目录一个都不该被报（合成输入）。

    ⚠️ 这一条同时钉住"白名单被悄悄放宽" —— 如果有人把 `pipeline` 加进
    `_ALLOWED_TOP_DIRS`，上面的 planted 用例会通过（它只查 `pipeline`），
    但**这一条会红**：白名单多了一项，而它在 tmp 里也被建了出来。
    """
    for name in _ALLOWED_TOP_DIRS:
        (tmp_path / name).mkdir()
    assert _foreign_top_dirs(tmp_path) == set(), sorted(_ALLOWED_TOP_DIRS)


def test_foreign_top_imports_detector_finds_a_planted_dependency(tmp_path):
    """🔑 顶层目录的 import **必须被报出来**（合成输入）。"""
    (tmp_path / "services" / "retrieval").mkdir(parents=True)
    (tmp_path / "newoffline").mkdir()
    target = tmp_path / "services" / "retrieval" / "engine.py"
    target.write_text("import newoffline\n", encoding="utf-8")
    got = _foreign_top_imports([target], tmp_path)
    assert got == ["services/retrieval/engine.py: newoffline"], got


def test_foreign_top_imports_detector_ignores_third_party_and_files(tmp_path):
    """🔑 第三方库 / 标准库 / 非 `services/` 下的文件**不该被报**。"""
    (tmp_path / "services").mkdir(parents=True)
    (tmp_path / "migrations" / "versions").mkdir(parents=True)
    a = tmp_path / "services" / "x.py"
    a.write_text("import numpy as np\nfrom fastapi import FastAPI\n"
                 "from services.common import auth\n", encoding="utf-8")
    b = tmp_path / "migrations" / "versions" / "m.py"
    b.write_text("import newoffline\n", encoding="utf-8")
    assert _foreign_top_imports([a, b], tmp_path) == [], "标准库/第三方/migrations 不该报"


def test_foreign_top_imports_detector_scans_every_services_subdir(tmp_path):
    """🔑 `services/` 的**每个**子目录都要扫（不是只有 `ingest/`）。

    ⚠️ 变异 `ROOT / "services"` → `ROOT / "services" / "ingest"`
    在真实仓库上**抓不到**（要第二份实现正好放在 `services/common/` 才可见），
    而这条合成输入让它无从遁形。
    """
    for sub in ("api", "common", "ingest", "retrieval", "worker", "mcp"):
        (tmp_path / "services" / sub).mkdir(parents=True)
        f = tmp_path / "services" / sub / "m.py"
        f.write_text("import newoffline\n", encoding="utf-8")
    (tmp_path / "newoffline").mkdir()
    got = _foreign_top_imports([*(tmp_path / "services").rglob("*.py")], tmp_path)
    assert len(got) == 6, got
