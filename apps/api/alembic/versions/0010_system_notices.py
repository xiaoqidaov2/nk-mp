"""Allow job-less system notifications (re-verification notices).

Revision ID: 0010_system_notices
Revises: 0009_dingtalk_robot
Create Date: 2026-10-04

``notification_delivery.job_id`` becomes nullable so account-level notices
(e.g. "Nike 连接需要重新验证" after a permanent renewal rejection) reuse the
same outbox, retry policy and delivery history as scan notifications.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0010_system_notices"
down_revision = "0009_dingtalk_robot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("notification_delivery", "job_id", existing_type=sa.UUID(), nullable=True)


def downgrade() -> None:
    op.execute("DELETE FROM notification_delivery WHERE job_id IS NULL")
    op.alter_column("notification_delivery", "job_id", existing_type=sa.UUID(), nullable=False)
