"""钉钉机器人：绑定会话（账号↔会话）与关键字命令。

Revision ID: 0006_dingtalk_bot
Revises: 0005_push_notifications
Create Date: 2026-10-01

平台自己持有钉钉企业内部应用机器人：用户在钉钉里把绑定码发给机器人，即把
该会话与该平台账号绑在一起（``dingtalk_bot_link``）。之后提醒推到这个会话，
关键字命令也用这个账号的鉴权和数据回答。绑定码一次性、短有效期，单独一张表。
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0006_dingtalk_bot"
down_revision = "0005_push_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dingtalk_link_code",
        sa.Column("owner_user_id", sa.UUID(), nullable=False),
        sa.Column("code", sa.String(length=12), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["app_user.id"], name="fk_dingtalk_link_code_owner", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("owner_user_id"),
    )

    op.create_table(
        "dingtalk_bot_link",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("owner_user_id", sa.UUID(), nullable=False),
        sa.Column("dingtalk_user_id", sa.String(length=64), nullable=False),
        sa.Column("dingtalk_corp_id", sa.String(length=64), nullable=False),
        sa.Column("conversation_id", sa.String(length=128), nullable=False),
        sa.Column("conversation_type", sa.Integer(), nullable=False),
        sa.Column("conversation_title", sa.String(length=160), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["app_user.id"], name="fk_dingtalk_bot_link_owner", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", name="uq_dingtalk_bot_link_conversation"),
    )


def downgrade() -> None:
    op.drop_table("dingtalk_bot_link")
    op.drop_table("dingtalk_link_code")
