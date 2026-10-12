"""30 秒频率档 + 账号级冷却 + 失败门店自动补跑。

Revision ID: 0011_cadence_cooldown
Revises: 0010_system_notices
Create Date: 2026-10-05

* ``credential`` 记录账号级冷却（额度用尽或被上游风控命中）：调度器在冷却
  窗口内不派发，到点自动恢复；连续命中风控按阶梯延长（services/throttle.py）。
* ``scan_job`` 记录补跑轮次与最早可租约时间：失败门店在冷却窗口结束后先补跑，
  不会被下一轮全量检查饿死。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0011_cadence_cooldown"
down_revision = "0010_system_notices"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "credential", sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "credential",
        sa.Column("cooldown_level", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("credential", sa.Column("cooldown_reason", sa.String(length=32), nullable=True))
    op.add_column(
        "credential", sa.Column("cooldown_notified_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("scan_job", sa.Column("not_before", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "scan_job",
        sa.Column("retry_round", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("scan_job", "retry_round")
    op.drop_column("scan_job", "not_before")
    op.drop_column("credential", "cooldown_notified_at")
    op.drop_column("credential", "cooldown_reason")
    op.drop_column("credential", "cooldown_level")
    op.drop_column("credential", "cooldown_until")
