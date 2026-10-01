"""Keep delivery history when a channel is deleted.

The card promises "历史记录保留" on delete; switch the delivery → channel
foreign key from CASCADE to SET NULL so old rows survive without a channel.

Revision ID: 0007_notification_history
Revises: 0006_dingtalk_bot
Create Date: 2026-10-02
"""

from __future__ import annotations

from alembic import op

revision = "0007_notification_history"
down_revision = "0006_dingtalk_bot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "fk_notification_delivery_channel", "notification_delivery", type_="foreignkey"
    )
    op.alter_column("notification_delivery", "channel_id", nullable=True)
    op.create_foreign_key(
        "fk_notification_delivery_channel",
        "notification_delivery",
        "notification_channel",
        ["channel_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    # Rows that lost their channel cannot be restored to the old constraint;
    # they were already unreachable output, so drop them on rollback.
    op.execute("DELETE FROM notification_delivery WHERE channel_id IS NULL")
    op.drop_constraint(
        "fk_notification_delivery_channel", "notification_delivery", type_="foreignkey"
    )
    op.alter_column("notification_delivery", "channel_id", nullable=False)
    op.create_foreign_key(
        "fk_notification_delivery_channel",
        "notification_delivery",
        "notification_channel",
        ["channel_id"],
        ["id"],
        ondelete="CASCADE",
    )
