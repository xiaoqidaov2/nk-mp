"""personal nike credentials and self-service phone auth

Revision ID: 0002_personal_nike
Revises: 0001_initial
Create Date: 2026-09-29

Hand-written revision on purpose (same policy as 0001): every DDL step and
every data backfill is explicit. Personal ownership is introduced without
guessing: legacy credentials keep their ciphertext but stay owner-less and
UNASSIGNED, historical jobs are terminated rather than attributed to a wrong
account, and observations only inherit an owner from their own job.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0002_personal_nike"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

ACTIVE_JOB_STATES = "('QUEUED','RUNNING')"
ACTIVE_ATTEMPT_STATES = "('SENDING','CODE_SENT','VERIFYING')"
MIGRATION_ERROR = "CREDENTIAL_OWNER_MIGRATION_REQUIRED"


def upgrade() -> None:
    # ---------------------------------------------------------------- credential
    op.add_column("credential", sa.Column("owner_user_id", sa.UUID(), nullable=True))
    op.add_column(
        "credential",
        sa.Column("binding_generation", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column("credential", sa.Column("last_error_code", sa.String(length=48), nullable=True))
    op.add_column("credential", sa.Column("phone_masked", sa.String(length=32), nullable=True))
    op.add_column("credential", sa.Column("phone_hash", sa.String(length=64), nullable=True))
    op.create_foreign_key(
        "fk_credential_owner_user", "credential", "app_user",
        ["owner_user_id"], ["id"], ondelete="CASCADE",
    )
    op.create_unique_constraint("uq_credential_owner_user", "credential", ["owner_user_id"])
    op.create_index(
        "uq_credential_phone_active",
        "credential",
        ["phone_hash"],
        unique=True,
        postgresql_where=sa.text("phone_hash IS NOT NULL AND status <> 'REVOKED'"),
    )
    # Legacy credentials keep their ciphertext untouched but must not look
    # active: nobody can prove which user they belong to.
    op.execute("UPDATE credential SET status = 'UNASSIGNED'")
    op.drop_constraint("credential_account_key_key", "credential", type_="unique")
    op.drop_column("credential", "account_key")

    # -------------------------------------------------------------- monitor_task
    op.drop_constraint("uq_monitor_task_tenant_name", "monitor_task", type_="unique")
    op.create_unique_constraint(
        "uq_monitor_task_owner_name", "monitor_task", ["created_by", "name"]
    )
    # Tasks without a proven owner can no longer run; keep the rows for the
    # owners to inspect, never attribute them to somebody else.
    op.execute(
        """
        UPDATE monitor_task SET is_active = false, next_run_at = NULL
        WHERE is_active = true AND (
            created_by IS NULL
            OR NOT EXISTS (
                SELECT 1 FROM app_user u
                WHERE u.id = monitor_task.created_by
                  AND u.is_active = true
                  AND u.tenant_id = monitor_task.tenant_id
            )
        )
        """
    )

    # ------------------------------------------------------------------ scan_job
    op.add_column("scan_job", sa.Column("owner_user_id", sa.UUID(), nullable=True))
    op.add_column("scan_job", sa.Column("credential_id", sa.UUID(), nullable=True))
    op.add_column("scan_job", sa.Column("credential_generation", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_scan_job_owner_user", "scan_job", "app_user",
        ["owner_user_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_scan_job_credential", "scan_job", "credential",
        ["credential_id"], ["id"], ondelete="SET NULL",
    )
    # Backfill: candidate A = requested_by, candidate B = task.created_by only
    # when the task belongs to the same tenant. Adopt only when exactly one
    # candidate is valid, or both are valid and identical.
    op.execute(
        """
        UPDATE scan_job j SET owner_user_id = src.owner
        FROM (
            SELECT j2.id,
                CASE
                    WHEN a.uid IS NOT NULL AND (b.uid IS NULL OR b.uid = a.uid) THEN a.uid
                    WHEN a.uid IS NULL AND b.uid IS NOT NULL THEN b.uid
                    ELSE NULL
                END AS owner
            FROM scan_job j2
            LEFT JOIN LATERAL (
                SELECT u.id AS uid FROM app_user u JOIN tenant t ON t.id = u.tenant_id
                WHERE u.id = j2.requested_by AND u.is_active = true
                  AND t.is_active = true AND u.tenant_id = j2.tenant_id
            ) a ON true
            LEFT JOIN LATERAL (
                SELECT u.id AS uid FROM app_user u JOIN tenant t ON t.id = u.tenant_id
                WHERE u.id = (
                    SELECT mt.created_by FROM monitor_task mt
                    WHERE mt.id = j2.task_id AND mt.tenant_id = j2.tenant_id
                )
                AND u.is_active = true AND t.is_active = true AND u.tenant_id = j2.tenant_id
            ) b ON true
        ) src
        WHERE j.id = src.id AND j.owner_user_id IS NULL
        """
    )
    # Historical in-flight jobs cannot be re-bound to an account safely.
    op.execute(
        f"""
        UPDATE scan_job_store s
        SET status = 'UNKNOWN', error_code = '{MIGRATION_ERROR}', finished_at = now()
        FROM scan_job j
        WHERE s.job_id = j.id
          AND j.status IN {ACTIVE_JOB_STATES}
          AND s.status IN ('PENDING','RUNNING')
        """
    )
    op.execute(
        f"""
        UPDATE scan_job
        SET status = 'CANCELLED', lease_expires_at = NULL, leased_by = NULL,
            heartbeat_at = NULL, finished_at = now(), error_summary = '{MIGRATION_ERROR}'
        WHERE status IN {ACTIVE_JOB_STATES}
        """
    )

    # --------------------------------------------------------------- observation
    op.add_column("observation", sa.Column("owner_user_id", sa.UUID(), nullable=True))
    op.add_column("observation", sa.Column("credential_id", sa.UUID(), nullable=True))
    op.add_column("observation", sa.Column("credential_generation", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_observation_owner_user", "observation", "app_user",
        ["owner_user_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_observation_credential", "observation", "credential",
        ["credential_id"], ["id"], ondelete="SET NULL",
    )
    op.execute(
        """
        UPDATE observation o SET owner_user_id = j.owner_user_id
        FROM scan_job j
        WHERE o.job_id = j.id AND j.owner_user_id IS NOT NULL AND o.owner_user_id IS NULL
        """
    )

    # -------------------------------------------------------------- change_event
    op.add_column("change_event", sa.Column("owner_user_id", sa.UUID(), nullable=True))
    op.add_column("change_event", sa.Column("credential_id", sa.UUID(), nullable=True))
    op.add_column("change_event", sa.Column("credential_generation", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_change_event_owner_user", "change_event", "app_user",
        ["owner_user_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_change_event_credential", "change_event", "credential",
        ["credential_id"], ["id"], ondelete="SET NULL",
    )
    op.execute(
        """
        UPDATE change_event e
        SET owner_user_id = src.owner,
            tenant_id = COALESCE(e.tenant_id, src.tenant_owner)
        FROM (
            SELECT e2.id,
                CASE
                    WHEN bo.owner_user_id IS NOT NULL
                         AND bo.owner_user_id = ao.owner_user_id
                    THEN bo.owner_user_id
                END AS owner,
                (SELECT u.tenant_id FROM app_user u WHERE u.id = bo.owner_user_id) AS tenant_owner
            FROM change_event e2
            LEFT JOIN observation bo ON bo.id = e2.before_observation_id
            LEFT JOIN observation ao ON ao.id = e2.after_observation_id
        ) src
        WHERE e.id = src.id
          AND src.owner IS NOT NULL
          AND (e.tenant_id IS NULL OR e.tenant_id = src.tenant_owner)
        """
    )

    # -------------------------------------------------------- observation_latest
    op.execute("CREATE SEQUENCE observation_latest_id_seq")
    op.add_column("observation_latest", sa.Column("id", sa.BigInteger(), nullable=True))
    op.add_column("observation_latest", sa.Column("owner_user_id", sa.UUID(), nullable=True))
    op.add_column("observation_latest", sa.Column("credential_id", sa.UUID(), nullable=True))
    op.add_column(
        "observation_latest", sa.Column("credential_generation", sa.Integer(), nullable=True)
    )
    op.execute("UPDATE observation_latest SET id = nextval('observation_latest_id_seq') WHERE id IS NULL")
    op.alter_column(
        "observation_latest",
        "id",
        nullable=False,
        server_default=sa.text("nextval('observation_latest_id_seq')"),
    )
    op.execute("ALTER SEQUENCE observation_latest_id_seq OWNED BY observation_latest.id")
    op.drop_constraint("observation_latest_pkey", "observation_latest", type_="primary")
    op.create_primary_key("observation_latest_pkey", "observation_latest", ["id"])
    op.create_unique_constraint(
        "uq_observation_latest_owner_store_style_size",
        "observation_latest",
        ["owner_user_id", "store_id", "style_id", "size_label"],
    )
    op.create_foreign_key(
        "fk_observation_latest_owner_user", "observation_latest", "app_user",
        ["owner_user_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_observation_latest_credential", "observation_latest", "credential",
        ["credential_id"], ["id"], ondelete="SET NULL",
    )
    op.execute(
        """
        UPDATE observation_latest l SET owner_user_id = o.owner_user_id
        FROM observation o
        WHERE o.id = l.last_observation_id AND o.owner_user_id IS NOT NULL
        """
    )

    # ----------------------------------------------------------- nike_auth_attempt
    op.create_table(
        "nike_auth_attempt",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("owner_user_id", sa.UUID(), nullable=True),
        sa.Column("auth_session_id", sa.UUID(), nullable=True),
        sa.Column("client_request_id", sa.UUID(), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("phone_enc", sa.Text(), nullable=False),
        sa.Column("phone_masked", sa.String(length=32), nullable=False),
        sa.Column("phone_hash", sa.String(length=64), nullable=False),
        sa.Column("ip_hash", sa.String(length=64), nullable=False),
        sa.Column("provider_context_enc", sa.Text(), nullable=False),
        sa.Column("base_credential_id", sa.UUID(), nullable=True),
        sa.Column("base_generation", sa.Integer(), server_default="0", nullable=False),
        sa.Column("result_credential_id", sa.UUID(), nullable=True),
        sa.Column("result_generation", sa.Integer(), nullable=True),
        sa.Column("verify_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resend_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("operation_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("operation_deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=48), nullable=True),
        sa.ForeignKeyConstraint(["owner_user_id"], ["app_user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["auth_session_id"], ["auth_session.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["base_credential_id"], ["credential.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["result_credential_id"], ["credential.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_user_id", "client_request_id", name="uq_nike_auth_attempt_request"),
    )
    op.create_index(
        "uq_nike_auth_attempt_owner_active",
        "nike_auth_attempt",
        ["owner_user_id"],
        unique=True,
        postgresql_where=sa.text(f"state IN {ACTIVE_ATTEMPT_STATES}"),
    )
    op.create_index(
        "uq_nike_auth_attempt_phone_active",
        "nike_auth_attempt",
        ["phone_hash"],
        unique=True,
        postgresql_where=sa.text(f"state IN {ACTIVE_ATTEMPT_STATES}"),
    )

    # ------------------------------------------------------------ nike_auth_limit
    op.create_table(
        "nike_auth_limit",
        sa.Column("owner_user_id", sa.UUID(), nullable=False),
        sa.Column("verify_window_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verify_attempts_in_window", sa.Integer(), server_default="0", nullable=False),
        sa.Column("verify_locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["owner_user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("owner_user_id"),
    )


def downgrade() -> None:
    # Personal data cannot be represented by the legacy single-account schema.
    # Refuse before touching anything; the migration transaction rolls back.
    conn = op.get_bind()
    unsafe = conn.execute(
        sa.text(
            """
            SELECT
                EXISTS (SELECT 1 FROM credential)
                OR EXISTS (SELECT 1 FROM nike_auth_attempt)
                OR EXISTS (SELECT 1 FROM nike_auth_limit)
                OR EXISTS (SELECT 1 FROM scan_job WHERE owner_user_id IS NOT NULL)
                OR EXISTS (SELECT 1 FROM observation WHERE owner_user_id IS NOT NULL)
                OR EXISTS (SELECT 1 FROM observation_latest WHERE owner_user_id IS NOT NULL)
                OR EXISTS (SELECT 1 FROM change_event WHERE owner_user_id IS NOT NULL)
                OR EXISTS (
                    SELECT 1 FROM observation_latest
                    GROUP BY store_id, style_id, size_label HAVING count(*) > 1
                )
            """
        )
    ).scalar()
    if unsafe:
        raise RuntimeError("PERSONAL_DATA_DOWNGRADE_UNSAFE")

    op.drop_table("nike_auth_limit")
    op.drop_index("uq_nike_auth_attempt_phone_active", table_name="nike_auth_attempt")
    op.drop_index("uq_nike_auth_attempt_owner_active", table_name="nike_auth_attempt")
    op.drop_table("nike_auth_attempt")

    op.drop_constraint(
        "uq_observation_latest_owner_store_style_size", "observation_latest", type_="unique"
    )
    op.drop_constraint("fk_observation_latest_owner_user", "observation_latest", type_="foreignkey")
    op.drop_constraint("fk_observation_latest_credential", "observation_latest", type_="foreignkey")
    op.drop_constraint("observation_latest_pkey", "observation_latest", type_="primary")
    op.create_primary_key(
        "observation_latest_pkey", "observation_latest", ["store_id", "style_id", "size_label"]
    )
    op.alter_column("observation_latest", "id", server_default=None)
    op.drop_column("observation_latest", "id")
    op.execute("DROP SEQUENCE IF EXISTS observation_latest_id_seq")
    op.drop_column("observation_latest", "credential_generation")
    op.drop_column("observation_latest", "credential_id")
    op.drop_column("observation_latest", "owner_user_id")

    op.drop_constraint("fk_change_event_owner_user", "change_event", type_="foreignkey")
    op.drop_constraint("fk_change_event_credential", "change_event", type_="foreignkey")
    op.drop_column("change_event", "credential_generation")
    op.drop_column("change_event", "credential_id")
    op.drop_column("change_event", "owner_user_id")

    op.drop_constraint("fk_observation_owner_user", "observation", type_="foreignkey")
    op.drop_constraint("fk_observation_credential", "observation", type_="foreignkey")
    op.drop_column("observation", "credential_generation")
    op.drop_column("observation", "credential_id")
    op.drop_column("observation", "owner_user_id")

    op.drop_constraint("fk_scan_job_owner_user", "scan_job", type_="foreignkey")
    op.drop_constraint("fk_scan_job_credential", "scan_job", type_="foreignkey")
    op.drop_column("scan_job", "credential_generation")
    op.drop_column("scan_job", "credential_id")
    op.drop_column("scan_job", "owner_user_id")

    op.drop_constraint("uq_monitor_task_owner_name", "monitor_task", type_="unique")
    op.create_unique_constraint(
        "uq_monitor_task_tenant_name", "monitor_task", ["tenant_id", "name"]
    )

    op.drop_index("uq_credential_phone_active", table_name="credential")
    op.drop_constraint("uq_credential_owner_user", "credential", type_="unique")
    op.drop_constraint("fk_credential_owner_user", "credential", type_="foreignkey")
    op.add_column(
        "credential",
        sa.Column("account_key", sa.String(length=64), nullable=False, server_default="default"),
    )
    op.alter_column("credential", "account_key", server_default=None)
    op.create_unique_constraint(None, "credential", ["account_key"])
    op.drop_column("credential", "phone_hash")
    op.drop_column("credential", "phone_masked")
    op.drop_column("credential", "last_error_code")
    op.drop_column("credential", "binding_generation")
    op.drop_column("credential", "owner_user_id")
