"""观测历史保留：每个 (owner, 门店, 货号, 尺码) 只留最近 N 次，并加支撑索引。

Revision ID: 0013_observation_retention
Revises: 0012_task_size_labels
Create Date: 2026-10-10

背景：监控间隔小（30 秒~10 分钟）× 147 店 × 多货号，`observation` 一天能涨几百万行
（2026-10-10 实测 752 万行 / 1.8 GB）。历史只用于回溯，保留最近 30 次足够，于是：

* 新增索引 ``ix_observation_owner_key_checked``：
  ``(owner_user_id, store_id, style_id, size_label, checked_at DESC)``，
  让"按 key 取最近 N 条"的窗口删除走索引（不然每次清理都要全表排序）。
* 数据清理本身不在这里做（可能几百万行），交给 ``app.cli prune-observations``
  分批执行；线上由作业收尾时的按 owner 轻量清理维持。
"""

from __future__ import annotations

from alembic import op

revision = "0013_observation_retention"
down_revision = "0012_task_size_labels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_observation_owner_key_checked",
        "observation",
        ["owner_user_id", "store_id", "style_id", "size_label", "checked_at"],
        unique=False,
        postgresql_ops={"checked_at": "DESC NULLS LAST"},
    )


def downgrade() -> None:
    op.drop_index("ix_observation_owner_key_checked", table_name="observation")
