"""jobs: ck_job_kind 改为 ingest（合并 parse/chunk/embed）

Revision ID: abe89931c6f2
Revises: 4b8f6faa8b7d
Create Date: 2026-10-03

⚠️ **这是手工写的迁移** —— `alembic revision --autogenerate` 不会检测
CHECK 约束的变更（autogenerate 只比对外键/索引/列，约束表达式不在其中）。

实测踩到：模型里 `ck_job_kind` 已经改成 `ingest` 了，autogenerate 却报
"Detected: ..." 里没有这一项，生成出来的迁移是空的，
于是 `INSERT INTO jobs (kind='ingest')` 被老约束挡下：

```
asyncpg.exceptions.CheckViolationError:
  new row for relation "jobs" violates check constraint "ck_job_kind"
```

**所以改 CHECK 约束必须手工写迁移。** 改完用这条验证：
```sql
SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='ck_job_kind';
```

背景：文本腿摄取的三步（parse → chunk → embed）实测加起来不到 1 秒
（解析 ~40ms/页、编码 88ms/chunk），拆成三个任务只是多三次事务，
而且进度回传要跨三个任务拼装。所以合并成 `ingest` 一个 kind。
视觉腿（colSmol 66s/页）仍然是独立的 `embed_visual`。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "abe89931c6f2"
down_revision: str | None = "4b8f6faa8b7d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_KINDS = (
    "parse", "chunk", "embed", "embed_visual", "thumbnail",
    "pdf_preview", "cleanup", "import_chat", "reindex",
)
NEW_KINDS = (
    "ingest", "embed_visual", "thumbnail", "pdf_preview",
    "cleanup", "import_chat", "reindex",
)


def upgrade() -> None:
    # ⚠️ 先把在队列里的旧 kind 迁到新 kind，否则改约束时会撞"已有数据违反新约束"
    op.execute("UPDATE jobs SET kind = 'ingest' WHERE kind IN ('parse','chunk','embed')")
    op.drop_constraint(op.f("ck_job_kind"), "jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_job_kind"), "jobs", sa.text("kind IN ('ingest','embed_visual',"
                                             "'thumbnail','pdf_preview','cleanup',"
                                             "'import_chat','reindex')")
    )


def downgrade() -> None:
    # 降级时无法把一个 ingest 任务拆回三步，只能标成 pending 让重跑
    op.execute("UPDATE jobs SET status = 'pending' WHERE kind = 'ingest'")
    op.drop_constraint(op.f("ck_job_kind"), "jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_job_kind"), "jobs", sa.text(
            "kind IN ('parse','chunk','embed','embed_visual','thumbnail',"
            "'pdf_preview','cleanup','import_chat','reindex')"
        )
    )
