"""合并采集：一个作业覆盖多个任务。

Revision ID: 0014_job_multi_task
Revises: 0013_observation_retention
Create Date: 2026-10-14

背景：可用性请求一次可带 **50 个 GTIN（实测硬上限）且跨款式算数**；而客户是"1 款 1 任务"，
31 个任务各自派作业 → 每店 31 个请求。合并后每店 `ceil(Σ尺码/50)` ≈ 12 个请求（2.6× 减少）。

为此：

* ``scan_job.task_ids``（JSONB 数组）：一个作业可以覆盖多个任务；单任务作业保持
  ``task_id`` 不变（向后兼容），合并作业 ``task_id IS NULL`` + ``task_ids`` 有值。
  事件与推送仍**按任务归属**（各自的开关键与标题）。
* 推送去重从"每 (作业, 渠道) 一条"改为"每 (作业, 渠道, 任务) 一条"：
  删掉 ``uq_notification_job_channel``，建普通索引 ``ix_notification_delivery_job_channel``，
  去重判定收敛到代码（同一作业的入队是单线程收尾，不需要数据库兜底）。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0014_job_multi_task"
down_revision = "0013_observation_retention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scan_job",
        sa.Column(
            "task_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.drop_constraint("uq_notification_job_channel", "notification_delivery", type_="unique")
    op.create_index(
        "ix_notification_delivery_job_channel",
        "notification_delivery",
        ["job_id", "channel_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_notification_delivery_job_channel", table_name="notification_delivery")
    op.create_unique_constraint(
        "uq_notification_job_channel", "notification_delivery", ["job_id", "channel_id"]
    )
    op.drop_column("scan_job", "task_ids")
