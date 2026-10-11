"""🔴 D129：库与工作区的**回收站列表**（看不见的反悔窗口等于没有）。

## 缺口是怎么发现的

D125 补了 `POST /kb/{id}/restore` 与 `POST /workspaces/{id}/restore`，
并在 PROGRESS 里记了一句"⚠️ **没有**工作区的回收站页面，
所以只能按 id 调端点恢复"。D127 回头查这件事时发现比"没有页面"更严重：

**连"列出我删过哪些"的端点都没有** —— 界面无从显示，脚本也无从断言。

| 承诺 | D129 之前 |
|---|---|
| D26「保留 30 天后物理清理 / 给误删一个**反悔窗口**」 | 恢复端点有（D125），但**看不见**删过什么 |
| D42「工作区删除：软删除，**可恢复**，保留 30 天」 | 同上 |

看不见的窗口等于没有：用户以为还有 29 天，却找不到"我删过什么"。

## 这一组测试钉的是**权限矩阵**，不是"端点在"

回收站是**唯一**会把"已软删的实体"暴露给列表的地方，
所以它的可见性条件必须与"恢复"的判据**逐档等价**：

| 库的可见性 | 谁能在回收站里看到并恢复 |
|---|---|
| `global` | 所有用户（D43：`can_write_kb` 也返回 True）|
| `workspace` | 该工作区的**成员** |
| `private` | **创建者** |

⚠️ 两个方向都要钉：**看得见却恢复不了**（用户点一下就 403）
与**恢复得了却不在列表里**（用户根本不知道还有它）都是功能不成立。
"""

from __future__ import annotations
from _docs import contract_text

import ast
from pathlib import Path

import pytest
from sqlalchemy.orm import Mapped  # ⚠️ 必须在**模块级**：declarative 会解析注解

ROOT = Path(__file__).resolve().parents[1]
KB_SRC = (ROOT / "services" / "api" / "routers" / "kb.py").read_text(encoding="utf-8")
WS_SRC = (ROOT / "services" / "api" / "routers" / "workspaces.py").read_text(
    encoding="utf-8"
)


def _code_of(fn: ast.AST) -> str:
    """函数体**去掉 docstring** 的源码（docstring 里故意写了反面例子）。"""
    import copy

    node = copy.deepcopy(fn)
    body = getattr(node, "body", [])
    if (
        body and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        node.body = body[1:] or [ast.Pass()]
    return ast.unparse(node)


def _fn(src: str, name: str) -> ast.AST:
    tree = ast.parse(src)
    return next(n for n in tree.body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == name)


def _paths(src: str) -> set[str]:
    out: set[str] = set()
    for node in ast.parse(src).body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and ast.unparse(dec.func).startswith("router."):
                out.update(
                    a.value for a in dec.args
                    if isinstance(a, ast.Constant) and isinstance(a.value, str)
                )
    return out


# ============================================================
# 1. 端点与契约
# ============================================================


def test_both_trash_endpoints_exist():
    assert "/trash" in _paths(KB_SRC), (
        "没有 `GET /kb/trash` —— 看不见自己删过哪些库，D26 的反悔窗口等于没有"
    )
    assert "/trash" in _paths(WS_SRC), (
        "没有 `GET /workspaces/trash` —— 同理，D42 的反悔窗口看不见"
    )


def test_contract_declares_both_trash_endpoints():
    """契约是唯一端点来源（`contract_check.py` 会报"实现了但契约没写"）。"""
    text = contract_text()
    assert "GET    /api/v1/kb/trash" in text, "契约没写 KB 回收站"
    assert "GET    /api/v1/workspaces/trash" in text, "契约没写工作区回收站"


def test_trash_route_is_declared_before_the_path_parameter_routes():
    """🔑 `/trash` 必须声明在 `/{kb_id}` **之前**。

    FastAPI 按声明顺序匹配路由，而 `/{kb_id}` 是 `int` 类型 ——
    `GET /kb/trash` 若排在它后面，会被当成 `kb_id="trash"` → **422**，
    症状是"端点明明写了却 422"，很容易被误判成参数类型写错。
    """
    for src, param in ((KB_SRC, "kb_id"), (WS_SRC, "ws_id")):
        paths = [p for p in _ordered_paths(src) if p.startswith("/")]
        try:
            i_trash = paths.index("/trash")
        except ValueError:
            pytest.fail(f"没有 /trash 路由：{paths}")
        params = [i for i, p in enumerate(paths) if p == f"/{{{param}}}"]
        assert not params or i_trash < params[0], (
            f"/trash 声明在 /{{{param}}} 之后：{paths} —— 会被当成路径参数"
        )


def _ordered_paths(src: str) -> list[str]:
    out: list[str] = []
    for node in ast.parse(src).body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and ast.unparse(dec.func).startswith("router."):
                out.extend(
                    a.value for a in dec.args
                    if isinstance(a, ast.Constant) and isinstance(a.value, str)
                )
    return out


# ============================================================
# 2. 可见性：**唯一**实现，且与恢复判据等价
# ============================================================


def test_kb_trash_reuses_the_single_write_side_implementation():
    """🔑 `GET /kb/trash` 必须调 `auth.writable_kb_clause`，不许自己拼、不许用读侧那份。

    ⚠️ 两个独立的坑：
    ① 自己拼一遍三档可见性 = 给本项目唯一一次真实越权（§0.6）再开一个口子；
    ② 用**读**侧的 `visible_kb_clause` —— 它与 `restore_kb` 的 `can_write_kb`
       差一格（"workspace 可见性但创建者已不是成员"：读得到、改不了），
       症状是"列表里有、点恢复 403"。**这正是第一版的写法**（§5.122）。
    """
    body = _code_of(_fn(KB_SRC, "list_kb_trash"))
    assert "writable_kb_clause(" in body, (
        "GET /kb/trash 没有用写侧的唯一实现 —— 列表与恢复会判得不一样"
    )
    assert "visible_kb_clause(" not in body, (
        "用了**读**侧的可见性条件 —— 与 restore 的 can_write_kb 不等价"
    )
    assert "visibility ==" not in body and "visibility !=" not in body, (
        "回收站自己拼了可见性条件 —— 三档可见性只能有一个实现（§0.6）"
    )
    assert "readable_kb_ids" not in body, (
        "用 ctx.readable_kb_ids 过滤软删的库 —— 那个集合按定义不含软删行，"
        "结果恒为空（与 D125 的 restore 同一堵墙）"
    )


def test_workspace_trash_lists_only_what_the_caller_can_restore():
    """🔑 工作区回收站只列 `owner_id == 我`，且不能用 `workspace_ids` 过滤。

    - 成员**从来没被允许**删工作区 → 给他们一个看得见恢复不了的条目毫无意义
    - 软删的行不在 `ctx.workspace_ids` 里（它由未软删的成员关系推导）
    """
    body = _code_of(_fn(WS_SRC, "list_workspaces_trash"))
    assert "owner_id==ctx.user_id" in body.replace(" ", ""), (
        "工作区回收站没有按 owner 过滤 —— 成员会看到自己恢复不了的条目"
    )
    assert "workspace_ids" not in body, (
        "用 ctx.workspace_ids 过滤软删的工作区 —— 恒为空"
    )


def test_visibility_clause_has_exactly_one_implementation():
    """🔑 生产代码里 `visibility == "global"` 只允许出现在 `auth.py`。

    判据指向**性质**（唯一真相源），不是"某一行不许动"：
    将来谁在第三个文件里写这份判定，这条立刻红。
    """
    offenders: list[str] = []
    for path in sorted((ROOT / "services").rglob("*.py")):
        if "common/auth.py" in path.as_posix():
            continue
        text = path.read_text(encoding="utf-8")
        # 用 AST 排除 docstring/注释里的反面例子（tests/test_retrieval_filters.py
        # 早就因为文本匹配把自己告发过）。
        tree = ast.parse(text, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            for n in ast.walk(node):
                if (isinstance(n, ast.Attribute) and n.attr == "visibility"
                        and isinstance(n.ctx, ast.Load)):
                    for c in node.comparators:
                        if isinstance(c, ast.Constant) and c.value == "global":
                            offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "三档可见性在多个文件里各写一遍 —— 本项目唯一一次真实越权就是这么来的"
        f"（CONVENTIONS §0.6）：{offenders}"
    )


def test_restore_and_trash_agree_on_who_can_write():
    """🔑 列表与恢复的判据必须**同源**：恢复用 `can_write_kb`。

    三档可见性下 `can_write_kb` 与 `visible_kb_clause` 恰好等价
    （global → 所有人；workspace → 成员；private → 创建者），
    所以"列表里有" ⇒ "恢复得动"。这条测试把那个等价关系钉住 ——
    哪天有人给 `can_write_kb` 加了第四档（比如"库成员"），
    列表就会开始出现"看得见却恢复不了"的条目。
    """
    from services.common.auth import UserContext, can_write_kb

    class _Kb:
        def __init__(self, visibility, created_by, workspace_id):  # noqa: ANN001
            self.visibility = visibility
            self.created_by = created_by
            self.workspace_id = workspace_id

    def ctx(user_id, ws_ids):  # noqa: ANN001
        return UserContext(
            user_id=user_id, display_name="u", email="u@t.local", api_key_id=None,
            readable_kb_ids=frozenset(), workspace_ids=frozenset(ws_ids),
        )

    cases = [
        (_Kb("global", 99, 7), 1, frozenset(), True),
        (_Kb("workspace", 99, 7), 1, frozenset({7}), True),
        (_Kb("workspace", 99, 7), 1, frozenset({8}), False),
        (_Kb("private", 1, 7), 1, frozenset({7}), True),
        (_Kb("private", 2, 7), 1, frozenset({7}), False),
    ]
    for kb, uid, ws, want in cases:
        assert can_write_kb(ctx(uid, ws), kb) is want, (
            f"{kb.visibility}/ws={kb.workspace_id} 的写权限判错了"
        )


# ============================================================
# 3. 行为层（真实 PG + 真实 HTTP 语义）
# ============================================================


def _pg_alive() -> bool:
    from sqlalchemy import text

    from services.common.db import sync_session_scope

    try:
        with sync_session_scope() as s:
            s.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


pg = pytest.mark.skipif(not _pg_alive(), reason="Postgres 不在线")


#: 真值表里的工作区：`id` → 是不是软删了（D130 那一维）。
#: ws=7 活着、ws=9 活着、ws=11 已软删。
WS_DELETED = {11: True}


def _truth_rows():
    """三档可见性 × 三种身份 × 工作区是否软删 —— 判据的**真值表**。

    ⚠️ `my_ws=False` 的两行是关键的"差一格"：workspace 可见性但我已不是成员。
    ⚠️ `ws_id=11`（已软删）那一维是 **D130** 加的："删掉工作区"要真的生效 ——
    工作区一软删，它的库读不到也恢复不了（先恢复工作区）。
    """
    rows = []
    for vis in ("global", "workspace", "private"):
        for created_by in (1, 2):          # 1 = 我，2 = 别人
            for ws_id, my_ws in ((7, True), (7, False), (9, False), (11, False)):
                rows.append((vis, created_by, ws_id, my_ws))
    return rows


class _KbRow:
    """`can_write_kb` 只读这三个属性（用替身而不是真 ORM 行：判据不该碰数据库）。"""

    def __init__(self, visibility, created_by, workspace_id):  # noqa: ANN001
        self.visibility = visibility
        self.created_by = created_by
        self.workspace_id = workspace_id


def _candidates(s):
    """在**真实 PG** 上造一张临时候选表（3 档 × 身份的真值表）。

    🔑 为什么绕这一圈：可见性构造器按 `model.visibility` 写（ORM 类的写法），
    而 `sqlalchemy.table()` / Core `Table` 只支持 `.c.列名` ——
    要让**同一份构造器**跑在一张人造表上，就得给它一个 ORM 类。
    于是：裸 DDL 建 TEMP 表（不污染开发库），declarative 类只用来拼查询。
    """
    from sqlalchemy import BigInteger, MetaData, Text, text
    from sqlalchemy.orm import declarative_base, mapped_column

    Base = declarative_base(metadata=MetaData())

    class _Cand(Base):  # noqa: N801 — 测试里的替身
        __tablename__ = "kb_candidates"
        id: Mapped[int] = mapped_column(primary_key=True)
        visibility: Mapped[str] = mapped_column(Text)
        created_by: Mapped[int | None] = mapped_column(BigInteger)
        workspace_id: Mapped[int | None] = mapped_column(BigInteger)

    class _Ws(Base):  # noqa: N801
        """工作区替身。🔑 D130 起可见性判定要查它 —— 用**同一张**候选表
        才能验"工作区软删 → 库不可见"，而不用真库里的工作区。"""
        __tablename__ = "ws_candidates"
        id: Mapped[int] = mapped_column(primary_key=True)
        deleted_at: Mapped[str | None] = mapped_column(Text)

    conn = s.connection()
    conn.execute(text(
        "CREATE TEMP TABLE kb_candidates ("
        "id int primary key, visibility text, created_by bigint, workspace_id bigint)"
    ))
    conn.execute(text("CREATE TEMP TABLE ws_candidates (id int primary key, deleted_at text)"))
    s.execute(
        _Cand.__table__.insert(),
        [
            {"id": i, "visibility": vis, "created_by": cb, "workspace_id": ws}
            for i, (vis, cb, ws, _mine) in enumerate(_truth_rows())
        ],
    )
    s.execute(
        _Ws.__table__.insert(),
        [
            {"id": ws, "deleted_at": "2026-01-01" if WS_DELETED.get(ws) else None}
            for ws in sorted({ws for _v, _c, ws, _m in _truth_rows()})
        ],
    )
    return _Cand, _Ws, conn


@pg
def test_writable_clause_is_equivalent_to_can_write_kb():
    """🔑 `writable_kb_clause`（SQL）与 `can_write_kb`（行）必须**逐格等价**。

    回收站列表用前者、`restore_kb` 用后者 —— 不等价就会出现
    "列表里有、点恢复 403"（D129 第一版踩的就是这个，§5.122）。

    ⚠️ 判据是**真值表**（3 档 × 创建者/非创建者 × 成员/非成员/别的成员），
    SQL 那一侧在真实 PG 上跑，不是"比几个手挑的例子"：
    第一版就是靠两个例子得出"两者等价"的结论，而它**不等价** ——
    `visible_kb_clause` 里 `created_by == 我` 是**独立**的 `or` 分支，
    `can_write_kb` 却是**按 visibility 分派**的。
    """
    from sqlalchemy import select, text

    from services.common.auth import UserContext, can_write_kb, writable_kb_clause
    from services.common.db import sync_session_scope

    rows = _truth_rows()
    with sync_session_scope() as s:
        cand, ws_tbl, conn = _candidates(s)
        try:
            for i, (vis, created_by, ws_id, my_ws) in enumerate(rows):
                ctx = UserContext(
                    user_id=1, display_name="u", email="u@t.local", api_key_id=None,
                    readable_kb_ids=frozenset(),
                    workspace_ids=frozenset({ws_id}) if my_ws else frozenset(),
                )
                sql_ids = {
                    r[0] for r in s.execute(
                        select(cand.id).where(
                            cand.id == i, writable_kb_clause(ctx, model=cand, workspace_model=ws_tbl)
                        )
                    ).all()
                }
                py_ok = can_write_kb(ctx, _KbRow(vis, created_by, ws_id))
                # 🔑 期望值写成 `can_write_kb and 工作区还活着`：
                #   clause 比 can_write_kb **多一条**限制（D130：工作区软删 →
                #   它的库既不可见也不可恢复）。把那一维写成"多一个 and"而不是
                #   混进 can_write_kb 的期望里，是为了让它**单独**有测试
                #   （`test_deleted_workspace_hides_its_kbs_on_both_sides`）。
                want = py_ok and not WS_DELETED.get(ws_id, False)
                assert bool(sql_ids) == want, (
                    f"vis={vis} created_by={created_by} ws={ws_id} 我是成员={my_ws}："
                    f"SQL 版说 {bool(sql_ids)}，期望 {want}"
                    f"（can_write_kb={py_ok}，工作区软删={WS_DELETED.get(ws_id, False)}）"
                )
        finally:
            conn.execute(text("DROP TABLE IF EXISTS kb_candidates"));
            conn.execute(text("DROP TABLE IF EXISTS ws_candidates"))


@pg
def test_read_and_write_clauses_differ_exactly_where_they_should():
    """🔑 钉住"读判定与写判定差**哪一格**" —— 免得下一个人又以为它们一样。

    差的那一格：**workspace 可见性、但创建者已不是成员**。
    读得到（`created_by == 我` 是独立分支）、改不了（`can_write_kb` 按
    visibility 分派）。所以回收站必须用**写**的那一份（D129 第一版用错了）。
    """
    from sqlalchemy import select, text

    from services.common.auth import UserContext, visible_kb_clause, writable_kb_clause
    from services.common.db import sync_session_scope

    with sync_session_scope() as s:
        cand, ws_tbl, conn = _candidates(s)
        try:
            # ⚠️ 我是 ws7 的成员（但**不是** ws9 的）——
            #   不设成员身份的话，"workspace + 我是创建者"整列都变成只读，
            #   差异集合就不是"恰好一格"了（第一版在这里写错过，§5.114）。
            ctx = UserContext(
                user_id=1, display_name="u", email="u@t.local", api_key_id=None,
                readable_kb_ids=frozenset(), workspace_ids=frozenset({7}),
            )
            read_ids = {
                r[0] for r in s.execute(
                    select(cand.id).where(
                        visible_kb_clause(ctx, model=cand, workspace_model=ws_tbl)
                    )
                ).all()
            }
            write_ids = {
                r[0] for r in s.execute(
                    select(cand.id).where(
                        writable_kb_clause(ctx, model=cand, workspace_model=ws_tbl)
                    )
                ).all()
            }
        finally:
            # TEMP 表随连接消失，但显式 DROP 让失败路径也不留痕迹
            conn.execute(text("DROP TABLE IF EXISTS kb_candidates"));
            conn.execute(text("DROP TABLE IF EXISTS ws_candidates"))

    rows = _truth_rows()
    # ⚠️ 只在"工作区还活着"的行上比差异：D130 让软删工作区里的库**两侧都不认**，
    #   那些行本来就不构成"读得到 / 改不了"的差异。
    alive = [i for i, (_v, _c, ws, _m) in enumerate(rows)
             if not WS_DELETED.get(ws, False)]
    mismatch = [
        i for i in alive if (i in read_ids) != (i in write_ids)
    ]
    # 只有"workspace + 我是创建者 + 我不是成员"那一格该不同
    # 🔑 本测试的 ctx 是**固定的**（我只加入了 ws7），所以判据用 ctx 而不是
    #   候选行里的 my_ws 列（那一列在这里没有意义 —— 真值表那一侧才用它）。
    #   workspace 可见性 + 我是创建者 + **我不是那个工作区的成员** → 只读不写。
    my_ws_ids = {7}
    expected_mismatch = [
        i for i, (vis, cb, ws, _mine) in enumerate(rows)
        if vis == "workspace" and cb == 1 and ws not in my_ws_ids
        # ⚠️ 工作区已软删的那些行**不在这里比**（上面已经滤掉了）：
        #   两侧都不认它们，所以不构成"只读不写"的差异。
        and not WS_DELETED.get(ws, False)
    ]
    assert mismatch == expected_mismatch, (
        "读/写判定的差异集合变了："
        f"实际 {[(rows[i]) for i in mismatch]}，"
        f"应只有 {[(rows[i]) for i in expected_mismatch]}"
    )
    # #8 = workspace + 我是创建者 + 我不在 ws9（工作区还活着）
    assert len(expected_mismatch) == 1, (
        f"真值表推导本身写错了：{[(rows[i]) for i in expected_mismatch]}"
    )


@pg
def test_trash_query_shows_deleted_rows_and_hides_live_ones():
    """🔑 回收站**只**列软删的行，且默认按删除时间倒序（最近的先看到）。

    ⚠️ "倒序"不是审美问题：D26 的窗口是 30 天，用户最先要处理的是
    刚删错的那一个。顺序不固定的话，恢复按钮要靠肉眼找。
    """
    import datetime as dt

    from sqlalchemy import select

    from services.common.auth import UserContext, visible_kb_clause
    from services.common.db import sync_session_scope
    from services.common.models import KnowledgeBase, Workspace

    stamp = dt.datetime.now(dt.UTC)
    with sync_session_scope() as s:
        # 🔑 必须挑**工作区还活着**的库：D130 起工作区一软删，
        #   它的库就不在回收站里了（开发库里确实有那种残留）。
        kb_id = s.execute(
            select(KnowledgeBase.id)
            .join(Workspace, Workspace.id == KnowledgeBase.workspace_id)
            .where(Workspace.deleted_at.is_(None))
            .limit(1)
        ).scalar_one_or_none()
        if kb_id is None:
            pytest.skip("库里没有工作区还活着的知识库")
        kb = s.get(KnowledgeBase, kb_id)
        owner = kb.created_by
        ws = kb.workspace_id
        kb.deleted_at = stamp
        s.flush()
        try:
            ctx = UserContext(
                user_id=owner, display_name="u", email="u@t.local", api_key_id=None,
                readable_kb_ids=frozenset({kb_id}), workspace_ids=frozenset({ws}),
            )
            rows = (
                s.execute(
                    select(KnowledgeBase)
                    .where(
                        KnowledgeBase.deleted_at.isnot(None),
                        visible_kb_clause(ctx, model=KnowledgeBase),
                    )
                    .order_by(KnowledgeBase.deleted_at.desc())
                )
            ).scalars().all()
            assert any(r.id == kb_id for r in rows), "软删的库没出现在回收站里"
            assert all(r.deleted_at is not None for r in rows), (
                "回收站里混进了没软删的库"
            )
            stamps = [r.deleted_at for r in rows]
            assert stamps == sorted(stamps, reverse=True), "没有按删除时间倒序"
        finally:
            kb.deleted_at = None
            s.flush()


@pg
def test_deleted_workspace_hides_its_kbs_on_both_sides():
    """🔴 **D130：软删工作区 → 它的库读不到、也恢复不了**（两侧都要）。

    起因是实测（`DELETE /workspaces/{id}` 之后）：

    ```
    GET /kb        → 仍然列出它的库
    GET /kb/{id}   → 仍然 200
    POST /search   → 仍然搜得到正文（命中 1 条，text 是原文）
    GET /kb/trash  → 空的（谁也恢复不了、也看不见）
    ```

    也就是说"删掉工作区"对**保密**毫无作用，而 D42 承诺的是"软删除"。
    ✅ 修法不是改成级联（D42 明确不级联，为了让恢复只有一行），
    而是让可见性判定要求**工作区还活着** —— 一处条件覆盖四条路径。

    ⚠️ 判据是**两侧都试**：读侧（`visible_kb_clause`）与写侧
    （`writable_kb_clause`，= 回收站列表）。只测一侧的话，另一侧仍会漏 ——
    而"能恢复但看不见"同样是不成立的功能。
    """
    import datetime as dt

    from sqlalchemy import select, text as sql_text

    from services.common.auth import UserContext, visible_kb_clause, writable_kb_clause
    from services.common.db import sync_session_scope
    from services.common.models import KnowledgeBase, Workspace

    stamp = dt.datetime.now(dt.UTC)
    with sync_session_scope() as s:
        kb_id = s.execute(
            select(KnowledgeBase.id)
            .join(Workspace, Workspace.id == KnowledgeBase.workspace_id)
            .where(Workspace.deleted_at.is_(None))
            .limit(1)
        ).scalar_one_or_none()
        if kb_id is None:
            pytest.skip("库里没有工作区还活着的知识库")
        kb = s.get(KnowledgeBase, kb_id)
        owner, ws = kb.created_by, kb.workspace_id
        ws_row = s.get(Workspace, ws)
        was_ws = ws_row.deleted_at
        was_kb = kb.deleted_at
        kb.deleted_at = stamp          # 库软删（回收站只列软删行）
        ws_row.deleted_at = stamp      # 工作区软删 = 整个撤走
        s.flush()
        try:
            ctx = UserContext(
                user_id=owner, display_name="u", email="u@t.local", api_key_id=None,
                # 🔑 成员身份**仍然在**（`workspace_members` 没有级联）——
                #   所以这条测的正是"光靠成员集合拦不住"。
                readable_kb_ids=frozenset({kb_id}), workspace_ids=frozenset({ws}),
            )
            read_hit = s.execute(
                select(KnowledgeBase.id).where(
                    KnowledgeBase.id == kb_id,
                    visible_kb_clause(ctx, model=KnowledgeBase),
                )
            ).scalars().all()
            assert not read_hit, (
                "工作区软删后它的库仍被判为**可读** —— "
                "而 readables 集合是所有检索腿的唯一入口（§0.2）"
            )
            write_hit = s.execute(
                select(KnowledgeBase.id).where(
                    KnowledgeBase.id == kb_id,
                    writable_kb_clause(ctx, model=KnowledgeBase),
                )
            ).scalars().all()
            assert not write_hit, (
                "工作区软删后它的库仍被判为**可恢复** —— 恢复端点也会跟着放行"
            )
            # 复原之前，确认"工作区活着时它是可见的"（否则上面两条恒真）
            ws_row.deleted_at = was_ws
            s.flush()
            again = s.execute(
                select(KnowledgeBase.id).where(
                    KnowledgeBase.id == kb_id,
                    visible_kb_clause(ctx, model=KnowledgeBase),
                )
            ).scalars().all()
            assert again == [kb_id], (
                "工作区活着时这个库却不可见 —— 那说明这条判据挡过头了"
            )
        finally:
            kb.deleted_at = was_kb
            s.get(Workspace, ws).deleted_at = was_ws
            s.flush()
        # 顺带钉住"EXISTS 必须是**关联**子查询"（§5.122 的那个坑）：
        # 不 correlate 的话它会渲染成 `FROM workspaces, knowledge_bases`，
        # 与外层那一行无关 —— 只要库里存在任意一行满足条件就全通过。
        sql = str(visible_kb_clause(
            UserContext(user_id=1, display_name="u", email="u@t.local",
                        api_key_id=None, readable_kb_ids=frozenset()),
        ).compile(compile_kwargs={"literal_binds": True}))
        assert "SELECT 1" in sql and "deleted_at IS NULL" in sql, sql
        assert sql.count("FROM workspaces") == 1, (
            "工作区存活判据没有作为**关联**子查询渲染（EXISTS 里不该再 FROM 一遍"
            "knowledge_bases）：" + sql
        )
        del sql_text  # 只是提醒上面那条断言用的是真 SQL 文本


@pg
def test_deleted_workspace_is_not_restorable_before_restore_kb():
    """🔑 `restore_kb` 在工作区还软删时**取不到行** → 403/404（fail-closed）。

    与 `GET /kb/trash` 同源：列表里没有、却能按 id 恢复，是自相矛盾的状态
    （用户会以为"界面坏了"，而其实是他要先恢复工作区）。
    """
    src = (ROOT / "services" / "api" / "routers" / "kb.py").read_text(
        encoding="utf-8"
    )
    tree = __import__("ast").parse(src)
    fn = next(n for n in tree.body
              if isinstance(n, __import__("ast").AsyncFunctionDef)
              and n.name == "restore_kb")
    body = _code_of(fn)
    assert "exists()" in body or "Workspace.deleted_at.is_(None)" in body.replace(
        " ", ""
    ), (
        "restore_kb 没有要求工作区还活着 —— 它会恢复一个用户看不见、"
        "且属于已删工作区的库（D130）"
    )


@pg
def test_a_deleted_workspace_kb_is_not_listed_while_its_workspace_is_deleted():
    """🔑 库在**软删的工作区**里时，既不在回收站、也搜不到（先恢复工作区）。

    🔑 这是**故意的顺序**：工作区一软删，它的库对谁都不可见、也恢复不了
    （D130 让"删工作区"真的生效）。列出来只会让人点一下拿到 403。

    ⚠️ 这里**真的软删工作区行**（不是只把 `ctx.workspace_ids` 清空）：
    D130 起可见性判定会检查工作区自己有没有被软删，只清空成员集合已经不够 ——
    第一版就是这么写的，于是它在 D130 之后**假红**（§5.122）。
    ⚠️ 只对 `visibility == "workspace"` 断言：`private` 的库只认创建者，
    `global` 谁都能恢复（第一版拿任意库断言，把对的实现判成失败，§5.114）。
    """
    import datetime as dt

    from sqlalchemy import select

    from services.common.auth import UserContext, writable_kb_clause
    from services.common.db import sync_session_scope
    from services.common.models import KnowledgeBase, Workspace

    stamp = dt.datetime.now(dt.UTC)
    with sync_session_scope() as s:
        kb_id = s.execute(
            select(KnowledgeBase.id).where(
                KnowledgeBase.deleted_at.is_(None),
                KnowledgeBase.visibility == "workspace",
            ).limit(1)
        ).scalar_one_or_none()
        if kb_id is None:
            pytest.skip("库里没有 workspace 可见性的活知识库")
        kb = s.get(KnowledgeBase, kb_id)
        owner, ws = kb.created_by, kb.workspace_id
        ws_row = s.get(Workspace, ws)
        if ws_row is None:
            pytest.skip("工作区行不在")
        was_ws_deleted_at = ws_row.deleted_at
        kb.deleted_at = stamp          # 库也软删（回收站只列软删的行）
        ws_row.deleted_at = stamp      # 工作区软删 = "整个工作区撤走"
        s.flush()
        try:
            ctx = UserContext(
                user_id=owner, display_name="u", email="u@t.local", api_key_id=None,
                readable_kb_ids=frozenset(), workspace_ids=frozenset({ws}),
            )
            rows = (
                s.execute(
                    select(KnowledgeBase.id).where(
                        KnowledgeBase.id == kb_id,
                        KnowledgeBase.deleted_at.isnot(None),
                        writable_kb_clause(ctx, model=KnowledgeBase),
                    )
                )
            ).scalars().all()
            assert not rows, (
                "工作区已软删时那个库仍出现在回收站 —— "
                "而它此刻谁也恢复不了（点一下就是 403）"
            )
        finally:
            kb.deleted_at = None
            s.get(Workspace, ws).deleted_at = was_ws_deleted_at
            s.flush()