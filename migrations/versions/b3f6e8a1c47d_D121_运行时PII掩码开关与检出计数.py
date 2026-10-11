"""D121：运行时 PII 掩码（按库开关）+ 摄取时存下的检出计数。

Revision ID: b3f6e8a1c47d
Revises: e5a7c3d91f02
Create Date: 2026-10-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b3f6e8a1c47d"
down_revision: str | None = "e5a7c3d91f02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 🔴 `server_default="false"` 且**不回填成 true** —— 默认关闭是隐私决策：
    #   掩码不可逆，开着它会让"搜我自己的手机号"失效（D121）。
    op.add_column(
        "knowledge_bases",
        sa.Column(
            "pii_mask", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    # `pii_hits`：JSONB，可空。
    # NULL = 没跑过检测（未摄取 / 纯扫描件没有文本）；
    # `{}` / `{"total": 0, "by_type": {}}` = 跑了且零命中。
    # 报告端点必须能区分这两者 —— "没检测过"和"检测过、干净"是相反的结论。
    op.add_column(
        "documents",
        sa.Column(
            "pii_hits",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("documents", "pii_hits")
    op.drop_column("knowledge_bases", "pii_mask")