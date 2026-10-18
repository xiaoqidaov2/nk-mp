"""一用户多账号 + 多节点监控。

Revision ID: 0015_multi_account_multinode
Revises: 0014_job_multi_task
Create Date: 2026-10-18

三件事：

* **一用户多账号**：删掉 ``uq_credential_owner_user``（一个平台用户只能绑一个 Nike
  账号），改为 ``(owner_user_id, status, cooldown_until)`` 普通索引供派发选号查询；
  ``uq_credential_phone_active``（同一 Nike 手机号不能被两个用户绑）保持不变。
* **bootstrap 计划所有权**：``owner_bootstrap.leased_by``（多 worker 下租约归属，
  与 ``scan_job.leased_by`` 同构）。
* **监控心跳**：``service_heartbeat``（scheduler / worker / monitor 实例最近心跳，
  监控服务实时核对，不做状态落库）。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0015_multi_account_multinode"
down_revision = "0014_job_multi_task"
branch_labels = None
depends_on = None

MULTI_ACCOUNT_ERROR = "存在多账号用户，无法回退：请先合并账号"


def upgrade() -> None:
    op.drop_constraint("uq_credential_owner_user", "credential", type_="unique")
    op.create_index(
        "ix_credential_owner_status_cooldown",
        "credential",
        ["owner_user_id", "status", "cooldown_until"],
        unique=False,
    )
    op.add_column("owner_bootstrap", sa.Column("leased_by", sa.String(96), nullable=True))
    op.add_column(
        "credential", sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_table(
        "service_heartbeat",
        sa.Column("service", sa.String(32), nullable=False),
        sa.Column("instance_id", sa.String(96), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.PrimaryKeyConstraint("service", "instance_id", name="pk_service_heartbeat"),
    )


def downgrade() -> None:
    # 多账号行无法塞回唯一约束：不静默删数据，先让操作者合并账号。
    duplicates = op.get_bind().execute(
        sa.text(
            "SELECT count(*) FROM ("
            " SELECT owner_user_id FROM credential"
            " WHERE owner_user_id IS NOT NULL"
            " GROUP BY owner_user_id HAVING count(*) > 1"
            ") AS multi"
        )
    ).scalar_one()
    if duplicates:
        raise RuntimeError(MULTI_ACCOUNT_ERROR)
    op.drop_table("service_heartbeat")
    op.drop_column("owner_bootstrap", "leased_by")
    op.drop_column("credential", "last_used_at")
    op.drop_index("ix_credential_owner_status_cooldown", table_name="credential")
    op.create_unique_constraint("uq_credential_owner_user", "credential", ["owner_user_id"])
