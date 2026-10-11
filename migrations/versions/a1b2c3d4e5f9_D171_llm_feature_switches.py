"""D171：`llm_settings` 加细粒度功能开关（use_for_ask / use_for_contextual）。

Revision ID: a1b2c3d4e5f9
Revises: a1b2c3d4e5f8
Create Date: 2026-10-11

🔑 `usable()` 只回答"凭据齐不齐"；"某个功能要不要外传"由这两列决定。
默认 TRUE（与 D170 的行为一致：切到 openai_compat 就是想要它干活）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f9"
down_revision: str | None = "a1b2c3d4e5f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "llm_settings",
        sa.Column(
            "use_for_ask", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
    )
    op.add_column(
        "llm_settings",
        sa.Column(
            "use_for_contextual",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    op.drop_column("llm_settings", "use_for_contextual")
    op.drop_column("llm_settings", "use_for_ask")
