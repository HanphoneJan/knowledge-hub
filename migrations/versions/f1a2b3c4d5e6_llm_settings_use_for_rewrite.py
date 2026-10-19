"""llm_settings: use_for_rewrite（查询改写是否走外部 LLM）

Revision ID: f1a2b3c4d5e6
Revises: a1b2c3d4e5fc
Create Date: 2026-10-15

D171 的细粒度开关原来只有 `use_for_ask` / `use_for_contextual`；查询改写
（文章 §Query Rewrite）是第三个"可以外传检索 query"的功能，加一列。

⚠️ 手写迁移：`autogenerate` 对"加 NOT NULL 列"不生成 `server_default`，
存量表（已有一行 llm_settings）会因 NULL 违反约束而失败 —— 所以带
`server_default='true'`，升级后立刻摘掉（理由同 §1.4：留着会让"漏填"静默成 true）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: str | None = "a1b2c3d4e5fc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "llm_settings",
        sa.Column(
            "use_for_rewrite", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )
    op.alter_column("llm_settings", "use_for_rewrite", server_default=None)


def downgrade() -> None:
    op.drop_column("llm_settings", "use_for_rewrite")
