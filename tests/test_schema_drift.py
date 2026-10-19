"""🔴 D128：迁移与模型不许漂移（CONVENTIONS §1.4「`alembic check` 必须报
**No new upgrade operations**」）。

## 为什么把它变成测试而不是文档里的一句话

`alembic check` 红着的时候，**没人说得清该改哪一侧**：

- `chat_day_stats`：模型多声明了一个与**复合主键**同列的 `UniqueConstraint`
  → 报「要**加**一个 `uq_chat_day_stat`」。照 autogenerate 生成迁移就会
  **多加一个与主键重复的索引**。
- `workspace_invites`：库里**有** `uq_invite_ws_prefix`、模型**没声明**
  → 报「要**删**它`。照做的话同一个工作区就能有两个同前缀的邀请码，
  而兑换是"按前缀找那条" → 症状是"输入我自己的码提示已被使用"。

⚠️ 两个方向都只有一边是对的：**以数据库为准去改模型的声明**
（迁移已经建出来的结构就是"这个部署的事实"，而 autogenerate 只会
把它翻译成一条破坏性的迁移）。D128 两处都是这么修的，**没有新增迁移**。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import UniqueConstraint

from services.common.models import Base

ROOT = Path(__file__).resolve().parents[1]


def _pg_alive() -> bool:
    from sqlalchemy import select, text

    from services.common.db import sync_session_scope

    try:
        with sync_session_scope() as s:
            s.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


pg = pytest.mark.skipif(not _pg_alive(), reason="Postgres 不在线")


def _tables():
    return {t.name: t for t in Base.metadata.sorted_tables}


def test_no_unique_constraint_duplicates_a_primary_key():
    """🔑 任何 UniqueConstraint 的列集合都**不得**与该表主键的列集合相同。

    ⚠️ 判据指向**性质**（"主键已经保证唯一，同列的 UniqueConstraint
    是纯冗余"），不是指向 `chat_day_stats` 这一张表 ——
    将来任何一张表写出这种声明，这条都会立刻红。

    后果不是"多一个约束"那么轻：它让 `alembic check` 报「要加约束」，
    而 autogenerate 生成的迁移会真的**多加一个索引** ——
    写 Migration 的人只会看到"模型说有，那加上吧"。
    """
    redundant: list[str] = []
    for name, table in _tables().items():
        pk_cols = tuple(c.name for c in table.primary_key.columns)
        if not pk_cols:
            continue
        for con in table.constraints:
            if isinstance(con, UniqueConstraint) and tuple(
                c.name for c in con.columns
            ) == pk_cols:
                redundant.append(f"{name}.{con.name}")
    assert not redundant, (
        f"这些 UniqueConstraint 与主键同列，是冗余声明：{redundant}。"
        "删掉模型里的那行（数据库侧的约束已经由主键提供）"
    )


def test_models_declare_the_unique_constraints_the_database_has():
    """🔑 邀请码的 `(workspace_id, code_prefix)` 唯一约束必须在模型里声明。

    ⚠️ 它是"生成时保证工作区内唯一"那条规则的**兜底**：`code_prefix` 只有
    4 位，而兑换是"按前缀找那条"，所以没有它就会出现"我自己的码提示
    已被使用"（D120 的注释里写过这条不变量）。

    判据是**集合相等**：既不许漏声明（→ `alembic check` 想删它），
    也不许多声明别的同列约束。
    """
    from services.common.models import WorkspaceInvite

    uniques = {
        con.name: tuple(c.name for c in con.columns)
        for con in WorkspaceInvite.__table_args__
        if isinstance(con, UniqueConstraint)
    }
    assert uniques.get("uq_invite_ws_prefix") == ("workspace_id", "code_prefix"), (
        f"模型没声明 (workspace_id, code_prefix) 的唯一约束：{uniques}。"
        "库里有它（迁移建的），漏声明会让 alembic check 想**删掉**它"
    )


@pg
def test_alembic_check_reports_no_new_upgrade_operations():
    """🔑 `alembic check` 必须报 `No new upgrade operations detected`。

    ⚠️ 为什么值得跑一次真命令而不是比较模型与库的元数据：
    `alembic check` 是**唯一**同时看过"迁移建出来的库"与"模型声明"的东西，
    CONVENTIONS §1.4 已经把它定为硬要求。手写元数据比较只能覆盖
    想到的那几类（约束、列、索引），而 autogenerate 的覆盖面更广。

    ⚠️ 判据是它的**成功输出**，不是退出码的某种猜测：
    alembic 的失败输出里也会出现 `New upgrade operations`，
    只判子串会把红当绿。
    """
    p = subprocess.run(
        [sys.executable, "-m", "alembic", "check"],
        cwd=ROOT, capture_output=True, text=True, timeout=180,
    )
    out = (p.stdout or "") + (p.stderr or "")
    assert "No new upgrade operations detected" in out, (
        "alembic check 报有新的升级操作（模型与迁移漂移了）：\n"
        + out[-1500:]
    )


@pg
def test_invite_prefix_uniqueness_is_enforced_by_the_database():
    """🔑 真库上那条唯一约束**真的在**（不只是模型声明了）。

    ⚠️ 这一条是**并发**的不变量：两个并发的"生成邀请码"请求都可能算出
    同一个前缀，应用层的"查一下有没有用过"在并发下会双双通过 ——
    只有数据库能兜住（同 D120 对 `used <= max_uses` 的说法）。
    """
    from sqlalchemy import text

    from services.common.db import sync_session_scope

    with sync_session_scope() as s:
        names = [
            r[0] for r in s.execute(text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'workspace_invites'::regclass "
                "AND contype = 'u'"
            )).all()
        ]
    assert "uq_invite_ws_prefix" in names, (
        f"库里没有 uq_invite_ws_prefix（现有的唯一约束：{names}）—— "
        "同工作区可以出现两个同前缀的邀请码"
    )


@pg
def test_chat_day_stats_uniqueness_is_the_primary_key():
    """🔑 `(kb_id, day)` 的唯一性由**主键**提供（UPSERT 的冲突键就是它）。

    ⚠️ 判据是"主键覆盖这两列"，不是"有没有某个 uq_* 约束"：
    唯一性这件事由主键提供就够了，而模型里再写一个同列的
    UniqueConstraint 只会让 `alembic check` 红（D128 修的就是这个）。
    """
    from sqlalchemy import text

    from services.common.db import sync_session_scope

    with sync_session_scope() as s:
        pk = [
            r[0] for r in s.execute(text(
                "SELECT a.attname FROM pg_constraint c "
                "JOIN pg_attribute a ON a.attrelid = c.conrelid "
                "AND a.attnum = ANY(c.conkey) "
                "WHERE c.conrelid = 'chat_day_stats'::regclass "
                "AND c.contype = 'p' ORDER BY a.attnum"
            )).all()
        ]
    assert pk == ["kb_id", "day"], f"chat_day_stats 的主键是 {pk}，不是 (kb_id, day)"