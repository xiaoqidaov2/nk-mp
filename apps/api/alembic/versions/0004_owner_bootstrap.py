"""binding-time preload plan per owner

Revision ID: 0004_owner_bootstrap
Revises: 0003_owner_catalog
Create Date: 2026-09-30

A successful Nike binding queues a preload for that owner only: the store
catalogue is fetched with the owner's own token and cached for them. Progress is
exposed to the user (ring progress in the UI), so it lives in this table.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0004_owner_bootstrap"
down_revision = "0003_owner_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "owner_bootstrap",
        sa.Column("owner_user_id", sa.UUID(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("progress", sa.Float(), nullable=False),
        sa.Column("stage", sa.String(length=80), nullable=False),
        sa.Column("store_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=48), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["app_user.id"],
            name="fk_owner_bootstrap_owner_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("owner_user_id"),
    )


def downgrade() -> None:
    op.drop_table("owner_bootstrap")
