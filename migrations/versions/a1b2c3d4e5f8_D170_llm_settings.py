"""D170：`llm_settings` —— 外部 LLM 配置（全局单例，页面可配）。

Revision ID: a1b2c3d4e5f8
Revises: a1b2c3d4e5f7
Create Date: 2026-10-10

⚠️ 手写迁移（CHECK 约束 autogenerate 不检测，PROGRESS §5.26）。
⚠️ **不插默认行**：没有行 = 抽取式（零外传），这正是隐私默认。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1b2c3d4e5f8"
down_revision: str | None = "a1b2c3d4e5f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_settings",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column(
            "provider", sa.Text(), nullable=False, server_default="extractive"
        ),
        sa.Column("base_url", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("api_key", sa.Text(), nullable=True),
        sa.Column(
            "extra_headers",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "temperature", sa.Float(), nullable=False, server_default="0.3"
        ),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "updated_by",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "provider IN ('extractive','openai_compat')", name="ck_llm_provider"
        ),
    )


def downgrade() -> None:
    op.drop_table("llm_settings")
