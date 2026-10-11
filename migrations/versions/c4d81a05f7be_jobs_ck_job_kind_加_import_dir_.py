"""jobs: kind 约束加 `import_dir`（D48）

Revision ID: c4d81a05f7be
Revises: b7e2c1d94a10
Create Date: 2026-10-03
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c4d81a05f7be"
down_revision: str | None = "b7e2c1d94a10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ⚠️ 必须同时改**模型里的约束文本**（`models.py::ck_job_kind`），
    #   否则下次 autogenerate 会认为约束变了、又生成一条来回改的迁移。
    op.drop_constraint("ck_job_kind", "jobs", type_="check")
    op.create_check_constraint(
        "ck_job_kind",
        "jobs",
        "kind IN ('ingest','embed_visual','thumbnail','pdf_preview',"
        "'cleanup','import_chat','import_dir','reindex')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_job_kind", "jobs", type_="check")
    op.create_check_constraint(
        "ck_job_kind",
        "jobs",
        "kind IN ('ingest','embed_visual','thumbnail','pdf_preview',"
        "'cleanup','import_chat','reindex')",
    )