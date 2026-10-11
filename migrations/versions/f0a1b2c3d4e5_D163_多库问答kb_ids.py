"""D163：多库问答 —— `chat_sessions` 加 `kb_ids`，`kb_id` 转可空。

Revision ID: f0a1b2c3d4e5
Revises: c4e1a7d5b902
Create Date: 2026-10-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f0a1b2c3d4e5"
down_revision: str | None = "c4e1a7d5b902"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1) 新的检索范围列。`None` = 全部有权限的库（与 `/search` 的 kb_ids=null 同义）。
    op.add_column(
        "chat_sessions",
        sa.Column("kb_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    # 2) 单库会话仍然只认一个 kb ⇒ kb_id 必须可空（多库会话没有唯一 kb）。
    op.alter_column("chat_sessions", "kb_id", existing_type=sa.BigInteger(), nullable=True)
    # 3) 回填：存量会话都是单库的 ⇒ kb_ids = [kb_id]。
    #    ⚠️ 必须回填：不回填的话老会话续问时 `kb_ids IS NULL` 会被当成
    #    "全部有权限的库"，于是**追问会突然跨库检索**（召回范围悄悄变大）。
    op.execute("UPDATE chat_sessions SET kb_ids = jsonb_build_array(kb_id) WHERE kb_id IS NOT NULL")


def downgrade() -> None:
    # 回滚前把 NULL 的 kb_id 用范围里的第一个补回来，否则 SET NOT NULL 会失败。
    op.execute(
        "UPDATE chat_sessions SET kb_id = (kb_ids->>0)::bigint "
        "WHERE kb_id IS NULL AND kb_ids IS NOT NULL AND jsonb_array_length(kb_ids) > 0"
    )
    op.alter_column("chat_sessions", "kb_id", existing_type=sa.BigInteger(), nullable=False)
    op.drop_column("chat_sessions", "kb_ids")
