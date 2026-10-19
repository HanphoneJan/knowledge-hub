"""D173/D174：`users` 加全局角色 `role` 与唯一昵称 `nickname`。

Revision ID: a1b2c3d4e5fa
Revises: a1b2c3d4e5f9
Create Date: 2026-10-11

- `role`：`user`（默认）| `superadmin`。**全局**身份，与工作区的 owner/member 无关。
- `nickname`：唯一、可空（存量用户没有；UNIQUE 允许多个 NULL），可作登录名。

⚠️ 手写迁移（CHECK 约束 autogenerate 不检测，PROGRESS §5.26）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5fa"
down_revision: str | None = "a1b2c3d4e5f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("role", sa.Text(), nullable=False, server_default="user"),
    )
    op.create_check_constraint(
        "ck_user_role", "users", "role IN ('user','superadmin')"
    )
    op.add_column("users", sa.Column("nickname", sa.Text(), nullable=True))
    # 唯一昵称。⚠️ Postgres 的 UNIQUE 允许多个 NULL ⇒ 存量用户不受影响。
    op.create_unique_constraint("uq_users_nickname", "users", ["nickname"])


def downgrade() -> None:
    op.drop_constraint("uq_users_nickname", "users", type_="unique")
    op.drop_column("users", "nickname")
    op.drop_constraint("ck_user_role", "users", type_="check")
    op.drop_column("users", "role")
