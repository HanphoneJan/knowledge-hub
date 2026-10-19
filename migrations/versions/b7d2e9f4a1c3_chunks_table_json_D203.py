"""chunks: table_json（表格的结构化网格，D203）

Revision ID: b7d2e9f4a1c3
Revises: f1a2b3c4d5e6
Create Date: 2026-10-19

文章 §表格要求"表格结构化"：Markdown 文本人读尚可，机器读不到行列关系。
`content_norm` 仍是 Markdown（不改渲染），另加一列存
`{"header": [...], "rows": [[...]], "row_count": n, "col_count": m}`。

⚠️ **可空**：非表格 chunk（正文 / 扫描页 / chat）为 NULL，不设默认值
（写了 `server_default` 反而会让"漏填"静默成空对象）。
旧点的表格在重建索引前 `table_json` 为 NULL —— 与 D199 的 `updated_at`
同一种"重摄取欠账"，前端需容忍字段缺失。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7d2e9f4a1c3"
down_revision: str | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chunks",
        sa.Column("table_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chunks", "table_json")
