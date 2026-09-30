"""owner-scoped catalogue: stores/styles/structures belong to the binding that fetched them

Revision ID: 0003_owner_catalog
Revises: 0002_personal_nike
Create Date: 2026-09-30

Hand-written revision (same policy as 0001/0002). Until now the store catalogue,
style metadata and GTIN structures were global rows filled by whichever ops
account ran the sync. The product rule is the opposite: anything fetched through
a user's Nike binding belongs to that user only and is merely cached for them.
This revision adds the ownership column and owner-scoped uniqueness, and
backfills existing rows to the user most likely to have fetched them (earliest
user with an ACTIVE binding, ops roles first; otherwise the earliest user).
Rows that cannot be attributed are dropped rather than leaked to a shared pool.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0003_owner_catalog"
down_revision = "0002_personal_nike"
branch_labels = None
depends_on = None

OWNER_BACKFILL = """
WITH owner AS (
    SELECT u.id
    FROM app_user u
    WHERE u.id IN (
        SELECT c.owner_user_id FROM credential c
        WHERE c.status = 'ACTIVE' AND c.owner_user_id IS NOT NULL
    )
    ORDER BY (u.role IN ('ops_admin', 'ops_operator')) DESC, u.created_at ASC
    LIMIT 1
), fallback AS (
    SELECT u.id FROM app_user u ORDER BY u.created_at ASC LIMIT 1
), chosen AS (
    SELECT id FROM owner UNION ALL SELECT id FROM fallback LIMIT 1
)
UPDATE {table} SET owner_user_id = (SELECT id FROM chosen) WHERE owner_user_id IS NULL
"""

TABLES = ("store", "style", "store_region_alias", "gtin_structure")


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("owner_user_id", sa.UUID(), nullable=True))
        op.create_foreign_key(
            f"fk_{table}_owner_user",
            table,
            "app_user",
            ["owner_user_id"],
            ["id"],
            ondelete="CASCADE",
        )

    # 回填既有（此前为全局共享）的目录数据；无法归属的删除，不留在公共池里。
    for table in TABLES:
        op.execute(OWNER_BACKFILL.format(table=table))
        op.execute(f"DELETE FROM {table} WHERE owner_user_id IS NULL")
        op.alter_column(table, "owner_user_id", nullable=False)

    op.execute("ALTER TABLE store DROP CONSTRAINT IF EXISTS store_store_code_key")
    op.execute("ALTER TABLE style DROP CONSTRAINT IF EXISTS style_style_code_key")
    op.execute(
        "ALTER TABLE store_region_alias DROP CONSTRAINT IF EXISTS uq_store_region_alias_region"
    )
    op.execute(
        "ALTER TABLE gtin_structure DROP CONSTRAINT IF EXISTS uq_gtin_structure_store_style"
    )
    op.create_unique_constraint("uq_store_owner_code", "store", ["owner_user_id", "store_code"])
    op.create_unique_constraint("uq_style_owner_code", "style", ["owner_user_id", "style_code"])
    op.create_unique_constraint(
        "uq_store_region_alias_owner_region",
        "store_region_alias",
        ["owner_user_id", "region_id"],
    )
    op.create_unique_constraint(
        "uq_gtin_structure_owner_store_style",
        "gtin_structure",
        ["owner_user_id", "store_id", "style_id"],
    )


def downgrade() -> None:
    # 回退到全局目录：同一 code 只保留最早的一行，其余删除（否则旧唯一约束无法建立）。
    op.execute(
        "DELETE FROM store s USING store t"
        " WHERE s.store_code = t.store_code AND s.id > t.id"
    )
    op.execute(
        "DELETE FROM style s USING style t"
        " WHERE s.style_code = t.style_code AND s.id > t.id"
    )
    op.execute(
        "DELETE FROM store_region_alias a USING store_region_alias b"
        " WHERE a.region_id = b.region_id AND a.id > b.id"
    )
    op.execute(
        "DELETE FROM gtin_structure a USING gtin_structure b"
        " WHERE a.store_id = b.store_id AND a.style_id = b.style_id AND a.id > b.id"
    )
    op.drop_constraint("uq_store_owner_code", "store", type_="unique")
    op.drop_constraint("uq_style_owner_code", "style", type_="unique")
    op.drop_constraint("uq_store_region_alias_owner_region", "store_region_alias", type_="unique")
    op.drop_constraint("uq_gtin_structure_owner_store_style", "gtin_structure", type_="unique")
    op.create_unique_constraint("store_store_code_key", "store", ["store_code"])
    op.create_unique_constraint("style_style_code_key", "style", ["style_code"])
    op.create_unique_constraint("uq_store_region_alias_region", "store_region_alias", ["region_id"])
    op.create_unique_constraint(
        "uq_gtin_structure_store_style", "gtin_structure", ["store_id", "style_id"]
    )
    for table in TABLES:
        op.drop_constraint(f"fk_{table}_owner_user", table, type_="foreignkey")
        op.drop_column(table, "owner_user_id")
