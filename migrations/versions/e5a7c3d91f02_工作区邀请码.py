"""工作区邀请码表（D4 · M1）。

Revision ID: e5a7c3d91f02
Revises: d9f4a7c2b1e3
Create Date: 2026-10-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5a7c3d91f02"
down_revision: str | None = "d9f4a7c2b1e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace_invites",
        # ⚠️ 复合唯一：一个工作区里不允许两个"前 4 位相同"的码 ——
        #   否则列表里两行看起来一模一样，而人只能靠前缀认它。
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("code_prefix", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False, server_default="member"),
        sa.Column("max_uses", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("redeemed_by", sa.BigInteger(), nullable=True),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["redeemed_by"], ["users.id"],
                                ondelete="SET NULL"),
        # 🔑 `used <= max_uses` 在**数据库层**兜住并发兑换：
        #   应用层"先读 used 再写"在两个请求同时进来时会双双通过。
        sa.CheckConstraint("used <= max_uses", name="ck_invite_used"),
        # 邀请码不能授予 owner：否则"转发一个码"= 交出整个工作区
        sa.CheckConstraint("role = 'member'", name="ck_invite_role"),
        sa.CheckConstraint("max_uses BETWEEN 1 AND 50", name="ck_invite_max_uses"),
        sa.UniqueConstraint("workspace_id", "code_prefix",
                            name="uq_invite_ws_prefix"),
    )
    op.create_index("ix_workspace_invites_code_prefix", "workspace_invites",
                    ["code_prefix"])


def downgrade() -> None:
    op.drop_index("ix_workspace_invites_code_prefix", table_name="workspace_invites")
    op.drop_table("workspace_invites")