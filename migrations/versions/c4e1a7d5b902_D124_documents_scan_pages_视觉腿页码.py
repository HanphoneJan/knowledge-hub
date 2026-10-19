"""D124：`documents.scan_pages` —— 视觉腿（页级）在 PG 侧的真相来源。

Revision ID: c4e1a7d5b902
Revises: b3f6e8a1c47d
Create Date: 2026-10-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c4e1a7d5b902"
down_revision: str | None = "b3f6e8a1c47d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 🔴 **可空、且不回填**：
    #   - 回填不了：页码是解析 PDF 时才知道的（哪些页没有文本层），
    #     存量行不重新解析就没有这个事实 —— 猜一个值等于造假数据。
    #   - NULL 与 `[]` 是**两个意思**：NULL = "不知道"（老文档），
    #     `[]` = "纯文本文档，本来就不该有页级向量"。
    #     对账遇到 NULL 必须跳过，而不是当成"应该有 0 个点"。
    op.add_column(
        "documents",
        sa.Column(
            "scan_pages",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("documents", "scan_pages")
