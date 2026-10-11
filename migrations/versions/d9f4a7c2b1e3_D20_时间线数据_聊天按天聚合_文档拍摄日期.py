"""D20 时间线的数据基础：`documents.taken_at` + `chat_day_stats` 表。

Revision ID: d9f4a7c2b1e3
Revises: c4d81a05f7be
Create Date: 2026-10-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d9f4a7c2b1e3"
down_revision: str | None = "c4d81a05f7be"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- documents.taken_at：照片的**事件日期** ----
    # ⚠️ 不能用 `created_at`：那是"导入时间"，而照片墙/纪念日要的是
    #   "这天发生了什么"。23 张照片一次导入完，created_at 全是同一时刻，
    #   时间线会退化成"一天 23 张"。
    op.add_column("documents", sa.Column("taken_at", sa.Date(), nullable=True))

    op.create_table(
        "chat_day_stats",
        sa.Column("kb_id", sa.BigInteger(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("types", postgresql.JSONB(astext_type=sa.Text()), nullable=False,
                  server_default="{}"),
        sa.Column("speakers", postgresql.JSONB(astext_type=sa.Text()), nullable=False,
                  server_default="{}"),
        sa.Column("highlights", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default="[]"),
        sa.Column("docs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["kb_id"], ["knowledge_bases.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("kb_id", "day"),
    )
    # ⚠️ `PRIMARY KEY (kb_id, day)` 已经能服务"单库 + 区间"这个查询，
    #   但时间线要按 `kb_id` 单独过滤（不指定区间时列全部月），
    #   补一个显式索引让那条路径也不走全表扫。
    op.create_index("ix_chat_day_stat_kb_day", "chat_day_stats",
                    ["kb_id", "day"])


def downgrade() -> None:
    op.drop_index("ix_chat_day_stat_kb_day", table_name="chat_day_stats")
    op.drop_table("chat_day_stats")
    op.drop_column("documents", "taken_at")