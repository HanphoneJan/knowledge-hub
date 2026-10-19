"""D164/D165：切分策略 + Parent-Child/章节元数据。

- `knowledge_bases.chunk_strategy`（默认 `recursive` = 现行为）
- `chunks.parent_text` / `chunks.section_path`

Revision ID: a1b2c3d4e5f6
Revises: f0a1b2c3d4e5
Create Date: 2026-10-10

⚠️ **手写迁移**：alembic autogenerate **不检测** CHECK 约束的值变更
（PROGRESS §5.26），`ck_kb_chunk_strategy` 必须手写。取值与
`services/ingest/strategies.STRATEGY_NAMES` 必须一致。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "f0a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1) KB 级切分策略。默认 recursive —— 存量库的行为一分不改。
    op.add_column(
        "knowledge_bases",
        sa.Column(
            "chunk_strategy", sa.Text(), nullable=False, server_default="recursive"
        ),
    )
    op.create_check_constraint(
        "ck_kb_chunk_strategy",
        "knowledge_bases",
        "chunk_strategy IN ('fixed','recursive','semantic',"
        "'parent_child','late','contextual')",
    )

    # 2) Parent-Child / 层级元数据。两列都可空 —— 存量 chunk 没有它们，
    #    读取端必须容忍 NULL（`has parent_text` 才用父块上下文）。
    op.add_column("chunks", sa.Column("parent_text", sa.Text(), nullable=True))
    op.add_column("chunks", sa.Column("section_path", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("chunks", "section_path")
    op.drop_column("chunks", "parent_text")
    op.drop_constraint("ck_kb_chunk_strategy", "knowledge_bases", type_="check")
    op.drop_column("knowledge_bases", "chunk_strategy")
