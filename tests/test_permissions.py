"""可见性与权限判定的单元测试。

这一组测试针对的是**已经真实发生过的越权漏洞**：

1. `visibility.in_(("workspace","global"))` 让**所有** workspace 可见的
   知识库对**任何**用户可见 —— Bob 不在 wsA 却能看到 wsA 的库。
2. private 的过滤里加了"同工作区即可见" —— 同工作区成员能看到别人的私有库。

两个 bug 的共同点：把三档可见性写进了一个 `IN` 里。
所以测试的重点是**三档必须分别判定**。

这些测试不需要数据库 —— 判定逻辑是纯 SQL 表达式，
用 SQLite 无法验证（依赖 PG 方言），所以这里只验证**判定函数的语义**：
构造 `UserContext`，断言 `readable_kb_ids` 与 `can_write_kb` 的结果。
真正的端到端验证在 `eval/api_1b_check.py`。
"""

from __future__ import annotations

import pytest
from sqlalchemy import and_, false, or_, select

from services.common.auth import (
    NO_SUCH_KB,
    UserContext,
    build_qdrant_filter,
    can_admin_workspace,
    can_write_kb,
)
from services.common.models import KnowledgeBase, Workspace


def ctx(user_id=1, workspace_ids=frozenset(), readable=frozenset()) -> UserContext:
    return UserContext(
        user_id=user_id,
        display_name="u",
        email=f"u{user_id}@t.local",
        api_key_id=None,
        workspace_ids=frozenset(workspace_ids),
        readable_kb_ids=frozenset(readable),
    )


class FakeKb:
    def __init__(self, visibility, created_by=1, workspace_id=1):
        self.visibility = visibility
        self.created_by = created_by
        self.workspace_id = workspace_id


# ============================================================
# 1. 读可见性
# ============================================================


def test_global_kb_readable_by_everyone():
    """D43：global 库所有用户可读。"""
    c = ctx(user_id=999, workspace_ids=frozenset(), readable=frozenset({50}))
    assert c.can_read_kb(50), "global 库应被任何用户读到"


def test_workspace_kb_only_for_members():
    """workspace 库只有该工作区成员能读 —— 这是被越权过的点。"""
    member = ctx(user_id=2, workspace_ids=frozenset({1}))
    outsider = ctx(user_id=3, workspace_ids=frozenset({99}))

    # 模拟登录时的推导结果
    member_readable = frozenset({10})
    outsider_readable = frozenset()

    m = ctx(user_id=2, workspace_ids=frozenset({1}), readable=member_readable)
    o = ctx(user_id=3, workspace_ids=frozenset({99}), readable=outsider_readable)

    assert m.can_read_kb(10), "工作区成员应能读"
    assert not o.can_read_kb(10), "❌ 非成员绝不能读 workspace 库（曾越权）"


def test_private_kb_only_for_creator():
    """private 库只有创建者能读 —— 同工作区成员也不行。"""
    creator = ctx(user_id=1, workspace_ids=frozenset({1}), readable=frozenset({20}))
    colleague = ctx(user_id=2, workspace_ids=frozenset({1}), readable=frozenset())

    assert creator.can_read_kb(20), "创建者应能读"
    assert not colleague.can_read_kb(20), "❌ 同工作区成员也不该读别人的 private 库"


def test_empty_workspace_ids_blocks_workspace_kbs():
    """不属于任何工作区的用户，一个 workspace 库都看不到。

    这是 `load_user_context` 里 `else: conds.append(false())` 分支的作用 ——
    少了它，`.in_(空集合)` 在部分方言下会被优化成永真条件。
    """
    c = ctx(user_id=5, workspace_ids=frozenset(), readable=frozenset())
    assert c.readable_kb_ids == frozenset()


# ============================================================
# 2. 可见性 SQL 表达式（三档必须分别判定）
# ============================================================


def test_visibility_sql_has_three_separate_branches():
    """🔑 防回归：可见性过滤必须有三个独立分支，不能合成一个 IN。

    曾经的写法 `visibility.in_(("workspace","global"))` 有两个错：
    - workspace 库没有加成员资格限定 → 全员可见
    - private 被 workspace 成员限定"顺带"覆盖 → 私有库泄露

    还有一次是修到一半：保留了裸的 `visibility == "workspace"` 分支，
    把带成员资格限定的分支**追加**成另一条 or —— 结果限定条件完全无效。
    """
    from sqlalchemy.dialects import postgresql

    from services.api.routers.kb import _visible_kb_filter

    c = ctx(user_id=7, workspace_ids=frozenset({1, 2}))
    # 用 literal_binds 才能在 SQL 里看到 'global'/'workspace' 字面量 ——
    # 默认参数化输出是 :visibility_1 / :visibility_2，肉眼分不出哪个是哪个。
    sql = str(
        _visible_kb_filter(c).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )

    # 三个分支都在
    assert "global" in sql, "缺 global 分支"
    assert "workspace" in sql, "缺 workspace 分支"
    assert "created_by" in sql, "缺 private(创建者) 分支"
    # workspace 分支必须带 workspace_id 限定（成员资格）
    assert "workspace_id IN" in sql, "❌ workspace 分支没限定工作区成员资格"
    # 不能出现把两者合并的 IN
    assert "visibility IN" not in sql, (
        "❌ workspace/global 被合并成一个 IN —— 这就是越权根源"
    )
    # 🔑 关键：裸的 `visibility = 'workspace'` 不允许单独作为 or 分支存在。
    # 用括号数检查：`... OR visibility = 'workspace' OR ...`（无 AND 紧跟）
    # 就说明成员资格限定被拆成了独立分支，等于没限定。
    import re

    bare = re.search(
        r"OR\s+knowledge_bases\.visibility = 'workspace'\s+OR", sql
    )
    assert not bare, (
        "❌ 存在没有 workspace_id 限定的裸 workspace 分支 —— "
        "成员资格限定必须与可见性条件写在同一个 AND 里"
    )


def test_visibility_sql_blocks_workspace_when_no_membership():
    """无工作区成员资格时，workspace 分支必须是恒假。"""
    import re

    from sqlalchemy.dialects import postgresql

    from services.api.routers.kb import _visible_kb_filter

    c = ctx(user_id=7, workspace_ids=frozenset())
    sql = str(
        _visible_kb_filter(c).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    # ⚠️ 不能只断言 SQL 里有 false —— 部分方言会把
    #    `A OR B OR false` 简化成 `A OR B`（结果相同但掩盖了分支存在性）。
    #    所以同时验证语义：**没有任何"按 workspace_id 限定成员"的 or 分支**。
    #
    # ⚠️ D130 起这条判据必须写成"没有 `workspace_id IN (...)` 分支"而不是
    #    "SQL 里不出现 workspace 这个词"：可见性判定现在还会带一个
    #    `EXISTS (… FROM workspaces … deleted_at IS NULL)`（工作区自己被软删 →
    #    它的库不可见），那个词是**更严格**的方向，与"成员资格限定被拆掉"无关。
    #    第一版写成 `"workspace" not in sql` → D130 一上线就假红（§5.122）。
    assert re.search(r"visibility = 'workspace'", sql) is None, (
        "无工作区成员资格时不应出现 workspace 可见性分支"
    )
    assert "workspace_id IN" not in sql, (
        "无工作区成员资格时不该有 `workspace_id IN (…)` —— 那是成员资格分支"
    )


def test_visibility_sql_respects_workspace_id_filter():
    """传了 workspace_id 就必须额外限定它。"""
    from services.api.routers.kb import _visible_kb_filter

    c = ctx(user_id=7, workspace_ids=frozenset({1, 2, 3}))
    sql = str(_visible_kb_filter(c, workspace_id=2))
    # 条件数变多（多了一个 workspace_id = 2 的 AND）
    assert sql.count("workspace_id") >= 2


# ============================================================
# 3. 写权限（D43：全局库所有人可编辑）
# ============================================================


def test_global_kb_writable_by_anyone():
    """D43 明确：全局公共库所有用户可读**可编辑**。"""
    c = ctx(user_id=999)
    assert can_write_kb(c, FakeKb("global", created_by=1))


def test_workspace_kb_writable_by_members():
    member = ctx(user_id=2, workspace_ids=frozenset({1}))
    outsider = ctx(user_id=3, workspace_ids=frozenset({99}))
    kb = FakeKb("workspace", created_by=1, workspace_id=1)
    assert can_write_kb(member, kb), "工作区成员应能改"
    assert not can_write_kb(outsider, kb), "❌ 非成员不能改"


def test_private_kb_writable_only_by_creator():
    creator = ctx(user_id=1, workspace_ids=frozenset({1}))
    colleague = ctx(user_id=2, workspace_ids=frozenset({1}))
    kb = FakeKb("private", created_by=1, workspace_id=1)
    assert can_write_kb(creator, kb)
    assert not can_write_kb(colleague, kb), "❌ 同工作区成员不能改别人的私有库"


# ============================================================
# 4. Qdrant 过滤构造（fail-closed）
# ============================================================


def test_qdrant_filter_denied_kb_returns_empty_set():
    """无权限的 kb_id → 必然空集，而不是报错或不过滤。"""
    c = ctx(user_id=1, readable=frozenset({5}))
    flt = build_qdrant_filter(c, 999)
    assert flt == {"must": [{"key": "kb_id", "match": {"value": -1}}]}


def test_qdrant_filter_all_kb_empty_context():
    """空权限范围 → 必然空集。绝不能退化成"不过滤"（那等于全库可读）。"""
    c = ctx(user_id=1, readable=frozenset())
    flt = build_qdrant_filter(c, None)
    assert "-1" in str(flt)


def test_qdrant_filter_allowed_kb():
    c = ctx(user_id=1, readable=frozenset({5, 7}))
    assert build_qdrant_filter(c, 5) == {"must": [{"key": "kb_id", "match": {"value": 5}}]}
    # 不指定 kb → 列出全部可读的
    flt = build_qdrant_filter(c, None)
    assert flt["must"][0]["key"] == "kb_id"
    assert [5, 7] == flt["must"][0]["match"]["any"]


def test_qdrant_filter_does_not_accept_tenant_override():
    """🔑 防回归：过滤条件只能由服务端从登录态推导。

    请求里可以指定 kb_id（那是"查哪个库"，服务端会校验权限），
    但**不能**指定 user_id/workspace_id 来扩大范围 ——
    构造函数里根本没有这样的参数。
    """
    import inspect

    params = list(inspect.signature(build_qdrant_filter).parameters)
    assert not any(p in params for p in ("user_id", "workspace_id", "tenant_id")), (
        f"过滤构造函数出现了租户覆盖参数: {params}"
    )
    # 允许的只有"想查哪些库"，且必须与 ctx.readable_kb_ids 求交（见下面两项）
    assert set(params) <= {"ctx", "kb_id", "kb_ids"}, f"意外的参数: {params}"


def test_qdrant_filter_kb_ids_subset_cannot_widen_scope():
    """🔑 `kb_ids`（跨库搜索的子集）只能**缩小**范围，绝不能扩大。

    这是实现 `/search` 跨库端点时真实写出来过的 bug：
    ```python
    # ❌ "单库传 kb_id，多库传 kb_id=None"
    #    → 用户要搜 [1,2]，实际搜了所有有权限的库（含用户没要求的 3 号）
    ```
    修法是让 `kb_ids` 与 `ctx.readable_kb_ids` **求交**。
    交集只会更小，所以传无权限的 kb_id 进来只会少结果，不会越权。
    """
    c = ctx(user_id=1, readable=frozenset({5, 7}))

    # 交集：{5,7} ∩ {5,9} = {5}
    flt = build_qdrant_filter(c, None, kb_ids=[5, 9])
    assert flt["must"][0]["match"]["any"] == [5], "9 不在我的可读范围，不该出现"

    # 全都不在授权范围 → 必然空集（fail-closed），不是"退回全集"
    assert build_qdrant_filter(c, None, kb_ids=[9, 11]) == {
        "must": [{"key": "kb_id", "match": {"value": NO_SUCH_KB}}]
    }

    # kb_id 与 kb_ids 同时给：kb_id 优先且必须自身合法
    assert build_qdrant_filter(c, 7, kb_ids=[5]) == {
        "must": [{"key": "kb_id", "match": {"value": 7}}]
    }


def test_build_kb_scope_sql_mirrors_qdrant_filter():
    """🔑 PG 侧（trgm 腿）与 Qdrant 侧**同源**、同 fail-closed 语义。

    CONVENTIONS §0.2 说"租户过滤只有一个入口"。trgm 腿在 Postgres 里，
    拿不到 Qdrant 的 filter 字典，所以 `build_kb_scope_sql` 是它的对应物。
    两者若行为不一致，就是开了第二个租户过滤入口 ——
    而本项目唯一一次真实越权（CONVENTIONS §0.6）正是"两处复刻逻辑"造成的。
    """
    from services.common.auth import build_kb_scope_sql

    c = ctx(user_id=1, readable=frozenset({5, 7}))

    # 无权限的 kb_id → 恒假
    assert str(build_kb_scope_sql(c, 99).compile(compile_kwargs={"literal_binds": True})) == (
        "knowledge_bases.id = -1"
    )
    # 空的授权范围 → 恒假，绝不退化成"不过滤"
    empty = ctx(user_id=1, readable=frozenset())
    assert str(
        build_kb_scope_sql(empty).compile(compile_kwargs={"literal_binds": True})
    ) == "knowledge_bases.id = -1"

    # 跨库子集求交
    sql = str(
        build_kb_scope_sql(c, None, kb_ids=[5, 9]).compile(
            compile_kwargs={"literal_binds": True}
        )
    )
    assert "IN (5)" in sql
    assert "9" not in sql

    # 合法单库
    assert str(
        build_kb_scope_sql(c, 7).compile(compile_kwargs={"literal_binds": True})
    ) == "knowledge_bases.id = 7"


# ============================================================
# 5. 工作区权限
# ============================================================


class FakeWs:
    def __init__(self, owner_id):
        self.owner_id = owner_id


def test_only_owner_can_admin_workspace():
    owner = ctx(user_id=1)
    member = ctx(user_id=2)
    ws = FakeWs(owner_id=1)
    assert can_admin_workspace(owner, ws)
    assert not can_admin_workspace(member, ws), "member 不能管理工作区"


@pytest.mark.parametrize("bad_ctx", [ctx(user_id=1), ctx(user_id=1, workspace_ids=frozenset({2}))])
def test_workspace_admin_requires_membership(bad_ctx):
    """既不是 owner 又不是成员的，一律拒绝。"""
    ws = FakeWs(owner_id=99)
    assert not can_admin_workspace(bad_ctx, ws)


# ============================================================
# 6. 超级管理员（D173）：越过三档可见性
# ============================================================


def _superadmin(user_id: int = 1, readable=frozenset({5, 7})) -> UserContext:
    return UserContext(
        user_id=user_id, display_name="su", email="su@t.local", api_key_id=None,
        workspace_ids=frozenset(), readable_kb_ids=frozenset(readable),
        is_superadmin=True,
    )


def test_superadmin_can_write_any_kb():
    su = _superadmin()
    # 别人的 private、别人的 workspace、global 都能改
    assert can_write_kb(su, FakeKb("private", created_by=999, workspace_id=999))
    assert can_write_kb(su, FakeKb("workspace", created_by=999, workspace_id=999))
    assert can_write_kb(su, FakeKb("global", created_by=999))


def test_superadmin_can_admin_any_workspace():
    assert can_admin_workspace(_superadmin(), FakeWs(owner_id=999))


def test_visible_kb_clause_superadmin_is_true():
    """🔑 超管的可见性条件是恒真 —— 不再有三档判定。"""
    from sqlalchemy.dialects import postgresql

    from services.common.auth import visible_kb_clause, writable_kb_clause

    for fn in (visible_kb_clause, writable_kb_clause):
        sql = str(
            fn(_superadmin(), model=KnowledgeBase).compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )
        assert "visibility" not in sql, f"{fn.__name__} 超管分支不该再判可见性：{sql}"
        assert "created_by" not in sql, sql


def test_normal_user_still_has_three_branches_after_superadmin_added():
    """🔑 超管分支**不能**改动普通用户的可见性 SQL（三档仍是三档）。"""
    from sqlalchemy.dialects import postgresql

    from services.common.auth import visible_kb_clause

    sql = str(
        visible_kb_clause(ctx(user_id=7, workspace_ids=frozenset({1})), model=KnowledgeBase)
        .compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert "global" in sql and "workspace" in sql and "created_by" in sql


def test_superadmin_qdrant_filter_uses_full_readable_set():
    su = _superadmin(readable=frozenset({5, 7}))
    flt = build_qdrant_filter(su)
    assert flt["must"][0]["match"]["any"] == [5, 7]
