"""Per-user DingTalk robot credentials.

Revision ID: 0009_dingtalk_robot
Revises: 0008_login_lockout
Create Date: 2026-10-02

钉钉机器人改成客户自己填：AppKey/AppSecret 存进 ``dingtalk_robot``（密钥加密），
API 进程按账号各连一条 Stream，会话靠“@ 机器人”自动绑定；一次性绑定码
（``dingtalk_link_code``）不再需要，删掉。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0009_dingtalk_robot"
down_revision = "0008_login_lockout"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dingtalk_robot",
        sa.Column("owner_user_id", sa.UUID(), nullable=False),
        sa.Column("robot_code", sa.String(length=64), nullable=False),
        sa.Column("robot_secret_enc", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["app_user.id"], name="fk_dingtalk_robot_owner", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("owner_user_id"),
    )
    op.drop_table("dingtalk_link_code")


def downgrade() -> None:
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
    op.drop_table("dingtalk_robot")
