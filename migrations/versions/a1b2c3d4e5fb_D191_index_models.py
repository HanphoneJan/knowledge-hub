"""D191：记录每条向量腿**实际用于建索引**的嵌入模型（Embedding 一致性守卫）。

Revision ID: a1b2c3d4e5fb
Revises: a1b2c3d4e5fa
Create Date: 2026-10-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5fb"
down_revision: str | None = "a1b2c3d4e5fa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 新表，主键是 collection 名；行数恒为个位数（dense / visual 各一行）。
    # ⚠️ **不回填**：空表 = "还不知道谁建的索引" —— 查询守卫对空表**放行**
    #   （见 `index_models.text_model_mismatch`），下一次入库 / 重建会写进来。
    #   回填成"当前配置的模型"是**错的**：那等于把未知当成已知，
    #   而这正是本表要防的失败模式。
    op.create_table(
        "index_models",
        sa.Column("collection", sa.Text(), primary_key=True, nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("dim", sa.Integer(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        # ⚠️ 不加 `server_default` —— 与全表一致用 Python 侧 `utcnow`（§1.1），
        #   加了会让 `alembic check` 报模型与库漂移（D128）。
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("index_models")
