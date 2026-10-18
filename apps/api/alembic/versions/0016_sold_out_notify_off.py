"""售罄提醒默认关闭。

Revision ID: 0016_sold_out_notify_off
Revises: 0015_multi_account_multinode
Create Date: 2026-10-15

2026-10-15 客户要求「售空不要通知」：`monitor_task.notify_on_sold_out` 的**代码默认**改为
``False``（新房客的任务不再提醒售罄），这里把**存量任务**一并关掉，避免升级后老任务继续推送。
变化流水里的售罄记录不受影响（只是不再推送）；需要恢复的任务在页面上单独打开。

``downgrade`` 把任务恢复为提醒售罄（旧默认 True）：原本单独关掉过的任务无法区分，
按旧默认统一恢复，语义与"回退到旧版本"一致。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0016_sold_out_notify_off"
down_revision = "0015_multi_account_multinode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text("UPDATE monitor_task SET notify_on_sold_out = false WHERE notify_on_sold_out")
    )


def downgrade() -> None:
    op.execute(
        sa.text("UPDATE monitor_task SET notify_on_sold_out = true WHERE NOT notify_on_sold_out")
    )
