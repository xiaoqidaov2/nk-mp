"""任务级尺码选择：monitor_task/size_labels 与 scan_job/size_labels。

Revision ID: 0012_task_size_labels
Revises: 0011_cadence_cooldown
Create Date: 2026-10-07

* ``monitor_task.size_labels``：任务级尺码选择，空列表 = 全部尺码（与
  ``store_codes`` 空=全部门店同构）。
* ``scan_job.size_labels``：入队时从任务冻结，探针据此过滤；空列表 = 全部。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0012_task_size_labels"
down_revision = "0011_cadence_cooldown"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "monitor_task",
        sa.Column(
            "size_labels",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "scan_job",
        sa.Column(
            "size_labels",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("scan_job", "size_labels")
    op.drop_column("monitor_task", "size_labels")