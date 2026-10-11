"""knowledge_bases: rerank_model（per-KB 启用 Reranker，阶段 3）

Revision ID: b7e2c1d94a10
Revises: abe89931c6f2
Create Date: 2026-10-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7e2c1d94a10"
down_revision: str | None = "abe89931c6f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ⚠️ **可空，且空 = 不启用**，不用 NOT NULL DEFAULT ''：
    #   "未设置"与"显式关闭"在业务上不是一回事（空 → 走全局默认模型）。
    op.add_column(
        "knowledge_bases",
        sa.Column("rerank_model", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("knowledge_bases", "rerank_model")