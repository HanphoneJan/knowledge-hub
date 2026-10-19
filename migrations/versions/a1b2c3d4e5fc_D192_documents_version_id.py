"""D192：`documents.version_id`（同一 `(kb_id, file_name)` 的版本号）。

Revision ID: a1b2c3d4e5fc
Revises: a1b2c3d4e5fb
Create Date: 2026-10-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5fc"
down_revision: str | None = "a1b2c3d4e5fb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ⚠️ 加 NOT NULL 列必须带 `server_default`（§1.4：存量表有数据时
    #   裸 `ADD COLUMN … NOT NULL` 会直接失败），升级完**立刻摘掉**
    #   —— 留着会让"漏填 version_id"悄悄存成 1，而不是在写入时暴露。
    # 存量文档都是"第一版"（历史里就没有版本号），所以回填 1 是**准确**的，
    # 不是权宜值。
    op.add_column(
        "documents",
        sa.Column("version_id", sa.Integer(), nullable=False, server_default="1"),
    )
    op.alter_column("documents", "version_id", server_default=None)


def downgrade() -> None:
    op.drop_column("documents", "version_id")
