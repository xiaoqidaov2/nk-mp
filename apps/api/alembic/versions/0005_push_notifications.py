"""push notifications: channels and the delivery outbox

Revision ID: 0005_push_notifications
Revises: 0004_owner_bootstrap
Create Date: 2026-09-30

Hand-written revision on purpose (same policy as 0001-0004): every DDL step is
explicit. A push channel belongs to exactly one user and stores its Apprise URL
encrypted with the platform credential key; a fingerprint deduplicates equal
targets per owner. ``change_event.job_id`` records which scan job detected an
event, so a finished job can queue one aggregated delivery per enabled channel
without guessing. Historical events keep ``job_id`` NULL: there is no reliable
source to backfill from and an unattributed event must not notify anyone.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0005_push_notifications"
down_revision = "0004_owner_bootstrap"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------ change_event
    op.add_column("change_event", sa.Column("job_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_change_event_job",
        "change_event",
        "scan_job",
        ["job_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_change_event_job_event", "change_event", ["job_id", "event_type"])

    # ------------------------------------------------------ notification_channel
    op.create_table(
        "notification_channel",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("owner_user_id", sa.UUID(), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("target_enc", sa.Text(), nullable=False),
        sa.Column("target_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("target_hint", sa.String(length=120), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_ok_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=48), nullable=True),
        sa.Column("last_error", sa.String(length=300), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["app_user.id"],
            name="fk_notification_channel_owner_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_user_id", "target_fingerprint", name="uq_notification_channel_target"
        ),
    )

    # ----------------------------------------------------- notification_delivery
    op.create_table(
        "notification_delivery",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("owner_user_id", sa.UUID(), nullable=False),
        sa.Column("channel_id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=48), nullable=True),
        sa.Column("last_error", sa.String(length=300), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["notification_channel.id"],
            name="fk_notification_delivery_channel",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["scan_job.id"], name="fk_notification_delivery_job", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["app_user.id"],
            name="fk_notification_delivery_owner_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["monitor_task.id"],
            name="fk_notification_delivery_task",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "channel_id", name="uq_notification_job_channel"),
    )
    op.create_index(
        "ix_notification_delivery_state_ready",
        "notification_delivery",
        ["state", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_notification_delivery_state_ready", table_name="notification_delivery")
    op.drop_table("notification_delivery")
    op.drop_table("notification_channel")
    op.drop_index("ix_change_event_job_event", table_name="change_event")
    op.drop_constraint("fk_change_event_job", "change_event", type_="foreignkey")
    op.drop_column("change_event", "job_id")
