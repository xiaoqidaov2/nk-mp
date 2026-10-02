"""Password login lockout.

Track failed password attempts on app_user and lock the account for a short
window once the configured threshold is hit, mirroring the SMS verify limit.

Revision ID: 0008_login_lockout
Revises: 0007_notification_history
Create Date: 2026-10-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_login_lockout"
down_revision = "0007_notification_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "app_user",
        sa.Column("failed_login_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "app_user", sa.Column("login_locked_until", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("app_user", "login_locked_until")
    op.drop_column("app_user", "failed_login_attempts")
