"""T09: migrations must be reproducible and drift-free.

Runs on a throwaway database created from the scratch server so the schema
history is exercised end to end: empty database -> upgrade -> downgrade ->
upgrade again, plus a metadata drift check.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from alembic import command
from app.bootstrap import alembic_config
from app.config import get_settings
from app.db import Base

from .conftest import TEST_DB_URL


def _temp_database() -> str:
    url = make_url(TEST_DB_URL)
    name = f"nike_mp_mig_{uuid.uuid4().hex[:8]}"
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    return name


def _drop_database(name: str) -> None:
    url = make_url(TEST_DB_URL)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture
def temp_database():
    name = _temp_database()
    url = make_url(TEST_DB_URL).set(database=name).render_as_string(hide_password=False)
    previous = os.environ.get("NIKE_MP_DATABASE_URL")
    os.environ["NIKE_MP_DATABASE_URL"] = url
    get_settings.cache_clear()
    try:
        yield name, url
    finally:
        if previous is not None:
            os.environ["NIKE_MP_DATABASE_URL"] = previous
        else:
            os.environ.pop("NIKE_MP_DATABASE_URL", None)
        get_settings.cache_clear()
        _drop_database(name)


def _tables(engine) -> set[str]:
    return set(inspect(engine).get_table_names()) - {"alembic_version"}


def _head_revision() -> str:
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def test_upgrade_downgrade_upgrade_and_metadata_drift(temp_database) -> None:
    name, url = temp_database
    config = alembic_config()
    engine = create_engine(url)

    # 1. Empty database: upgrade creates the full schema.
    command.upgrade(config, "head")
    assert _tables(engine) == {table.name for table in Base.metadata.sorted_tables}

    # 2. Repeated upgrade is a no-op.
    command.upgrade(config, "head")

    # 3. The schema must match the SQLAlchemy metadata (no drift).
    inspector = inspect(engine)
    for table in Base.metadata.sorted_tables:
        db_columns = {col["name"]: col["nullable"] for col in inspector.get_columns(table.name)}
        model_columns = {col.name: col.nullable for col in table.columns}
        assert db_columns.keys() == model_columns.keys(), f"column set drift on {table.name}"
        for column, nullable in model_columns.items():
            assert db_columns[column] == nullable, f"nullability drift on {table.name}.{column}"
        db_fks = {
            (tuple(fk["constrained_columns"]), fk["referred_table"])
            for fk in inspector.get_foreign_keys(table.name)
        }
        model_fks = {
            ((fk.parent.name,), fk.column.table.name) for fk in table.foreign_keys
        }
        assert db_fks == model_fks, f"foreign key drift on {table.name}"

    # 4. Downgrade removes every application table.
    command.downgrade(config, "base")
    assert _tables(engine) == set()

    # 5. And it can be rebuilt from scratch on the same database.
    command.upgrade(config, "head")
    assert _tables(engine) == {table.name for table in Base.metadata.sorted_tables}
    engine.dispose()
    assert name  # keep the fixture variable referenced


MIGRATION_ERROR = "CREDENTIAL_OWNER_MIGRATION_REQUIRED"


def _seed_0001(engine) -> dict:
    """Insert 0001-era rows whose owner is provable, ambiguous or absent."""
    ids = {
        key: uuid.uuid4()
        for key in (
            "tenant1",
            "tenant2",
            "user1",
            "user4",
            "user_inactive",
            "user2",
            "task_a",
            "task4",
            "orphan_task",
            "store",
            "style",
            "job_a",
            "job_b",
            "job_same",
            "job_diff",
            "job_invalid",
            "job_cross",
            "job_b_only",
            "job_a_only",
            "job_active",
            "credential",
        )
    }
    with engine.begin() as conn:
        def run(sql: str, **params) -> None:
            conn.execute(text(sql), params)

        run(
            "INSERT INTO tenant (id, slug, name, is_platform, is_active, daily_review_budget)"
            " VALUES (:id, :slug, :name, false, true, 5000)",
            id=ids["tenant1"], slug="t1", name="租户一",
        )
        run(
            "INSERT INTO tenant (id, slug, name, is_platform, is_active, daily_review_budget)"
            " VALUES (:id, :slug, :name, false, true, 5000)",
            id=ids["tenant2"], slug="t2", name="租户二",
        )
        for key, email, tenant_key, active in (
            ("user1", "u1@example.com", "tenant1", True),
            ("user4", "u4@example.com", "tenant1", True),
            ("user_inactive", "ui@example.com", "tenant1", False),
            ("user2", "u2@example.com", "tenant2", True),
        ):
            run(
                "INSERT INTO app_user (id, tenant_id, email, display_name, password_hash,"
                " role, is_active) VALUES (:id, :tenant_id, :email, :email, 'x', 'tenant_admin', :active)",
                id=ids[key], tenant_id=ids[tenant_key], email=email, active=active,
            )
        run(
            "INSERT INTO store (id, store_code, display_name, address, concept, region_id, is_active)"
            " VALUES (:id, 'S001', '一店', '', '', 'R1', true)",
            id=ids["store"],
        )
        run(
            "INSERT INTO style (id, style_code, title, image_url)"
            " VALUES (:id, 'DN3707-202', '测试货号', '')",
            id=ids["style"],
        )

        def task(key: str, name: str, created_by) -> None:
            run(
                "INSERT INTO monitor_task (id, tenant_id, name, store_codes, style_codes,"
                " all_stores, cadence_seconds, is_active, notify_on_restock, notify_on_sold_out,"
                " notify_on_price_change, next_run_at, created_by)"
                " VALUES (:id, :tenant_id, :name, '[]'::jsonb, '[\"DN3707-202\"]'::jsonb,"
                " true, 1800, true, true, true, true, now(), :created_by)",
                id=ids[key], tenant_id=ids["tenant1"], name=name, created_by=created_by,
            )

        task("task_a", "任务A", ids["user1"])
        task("task4", "任务四", ids["user4"])
        task("orphan_task", "孤儿任务", None)

        def job(key: str, *, requested_by, task_id, status="SUCCEEDED") -> None:
            run(
                "INSERT INTO scan_job (id, tenant_id, task_id, kind, status, store_codes,"
                " style_codes, total_stores, done_stores, failed_stores, unknown_stores, attempt,"
                " requested_by, error_summary)"
                " VALUES (:id, :tenant_id, :task_id, 'SCAN', :status, '[\"S001\"]'::jsonb,"
                " '[\"DN3707-202\"]'::jsonb, 1, 0, 0, 0, 0, :requested_by, '')",
                id=ids[key], tenant_id=ids["tenant1"], task_id=task_id,
                status=status, requested_by=requested_by,
            )

        job("job_a", requested_by=ids["user1"], task_id=None)
        job("job_b", requested_by=None, task_id=ids["task_a"])
        job("job_same", requested_by=ids["user1"], task_id=ids["task_a"])
        job("job_diff", requested_by=ids["user1"], task_id=ids["task4"])
        job("job_invalid", requested_by=ids["user_inactive"], task_id=None)
        job("job_cross", requested_by=ids["user2"], task_id=None)
        job("job_b_only", requested_by=ids["user_inactive"], task_id=ids["task_a"])
        job("job_a_only", requested_by=ids["user1"], task_id=ids["orphan_task"])

        run(
            "INSERT INTO scan_job (id, tenant_id, task_id, kind, status, store_codes,"
            " style_codes, total_stores, done_stores, failed_stores, unknown_stores, attempt,"
            " lease_expires_at, heartbeat_at, leased_by, requested_by, error_summary, started_at)"
            " VALUES (:id, :tenant_id, NULL, 'SCAN', 'RUNNING', '[\"S001\"]'::jsonb,"
            " '[\"DN3707-202\"]'::jsonb, 1, 0, 0, 0, 0, now() + interval '5 min', now(),"
            " 'workerA', :requested_by, '', now())",
            id=ids["job_active"], tenant_id=ids["tenant1"], requested_by=ids["user1"],
        )
        run(
            "INSERT INTO scan_job_store (job_id, store_id, status, attempt, message,"
            " observations_written, events_written)"
            " VALUES (:job_id, :store_id, 'PENDING', 0, '', 0, 0)",
            job_id=ids["job_active"], store_id=ids["store"],
        )

        obs_owned = conn.execute(
            text(
                "INSERT INTO observation (store_id, style_id, size_label, gtin, availability,"
                " trust, price_status, promotions, job_id, checked_at)"
                " VALUES (:store_id, :style_id, '42', 'G1', 'IN_STOCK', 'TRUSTED', 'MISSING',"
                " '[]'::jsonb, :job_id, now()) RETURNING id"
            ),
            {"store_id": ids["store"], "style_id": ids["style"], "job_id": ids["job_a"]},
        ).scalar_one()
        obs_orphan = conn.execute(
            text(
                "INSERT INTO observation (store_id, style_id, size_label, gtin, availability,"
                " trust, price_status, promotions, job_id, checked_at)"
                " VALUES (:store_id, :style_id, '43', 'G2', 'IN_STOCK', 'TRUSTED', 'MISSING',"
                " '[]'::jsonb, :job_id, now()) RETURNING id"
            ),
            {"store_id": ids["store"], "style_id": ids["style"], "job_id": ids["job_diff"]},
        ).scalar_one()

        for size, last_observation in (("42", obs_owned), ("43", obs_orphan)):
            run(
                "INSERT INTO observation_latest (store_id, style_id, size_label, gtin,"
                " availability, trust, price_status, promotions, baseline_ready,"
                " last_observation_id, checked_at)"
                " VALUES (:store_id, :style_id, :size, 'G1', 'IN_STOCK', 'TRUSTED', 'MISSING',"
                " '[]'::jsonb, true, :last_observation_id, now())",
                store_id=ids["store"], style_id=ids["style"], size=size,
                last_observation_id=last_observation,
            )

        run(
            "INSERT INTO credential (id, account_key, client_id, access_token_enc,"
            " refresh_token_enc, status, last_error)"
            " VALUES (:id, 'default', 'client', 'legacy-access', 'legacy-refresh', 'ACTIVE', '')",
            id=ids["credential"],
        )
        ids["obs_owned"] = obs_owned
        ids["obs_orphan"] = obs_orphan
    return ids


def test_personal_migration_backfills_owners_and_terminates_old_jobs(temp_database) -> None:
    name, url = temp_database
    config = alembic_config()
    engine = create_engine(url)

    command.upgrade(config, "0001_initial")
    ids = _seed_0001(engine)
    command.upgrade(config, "head")

    with engine.connect() as conn:
        # Legacy credential keeps its ciphertext but stays owner-less.
        credential = conn.execute(
            text("SELECT owner_user_id, binding_generation, status FROM credential")
        ).one()
        assert credential.owner_user_id is None
        assert credential.binding_generation == 1
        assert credential.status == "UNASSIGNED"
        assert "account_key" not in {col["name"] for col in inspect(engine).get_columns("credential")}

        owners = {
            row.id: row.owner_user_id
            for row in conn.execute(text("SELECT id, owner_user_id FROM scan_job")).all()
        }
        assert owners[ids["job_a"]] == ids["user1"]  # candidate A
        assert owners[ids["job_b"]] == ids["user1"]  # candidate B
        assert owners[ids["job_same"]] == ids["user1"]  # both valid, identical
        assert owners[ids["job_diff"]] is None  # both valid, different -> never guess
        assert owners[ids["job_invalid"]] is None  # inactive user
        assert owners[ids["job_cross"]] is None  # user from another tenant
        assert owners[ids["job_b_only"]] == ids["user1"]  # only B is valid
        assert owners[ids["job_a_only"]] == ids["user1"]  # only A is valid
        assert owners[ids["job_active"]] == ids["user1"]

        # Historical in-flight work is terminated, not attributed.
        active = conn.execute(
            text(
                "SELECT status, error_summary, lease_expires_at, leased_by FROM scan_job"
                " WHERE id = :id"
            ),
            {"id": ids["job_active"]},
        ).one()
        assert active.status == "CANCELLED"
        assert active.error_summary == MIGRATION_ERROR
        assert active.lease_expires_at is None and active.leased_by is None
        store_row = conn.execute(
            text("SELECT status, error_code FROM scan_job_store WHERE job_id = :id"),
            {"id": ids["job_active"]},
        ).one()
        assert store_row.status == "UNKNOWN"
        assert store_row.error_code == MIGRATION_ERROR

        # Orphan tasks are deactivated; owned tasks keep running.
        orphan = conn.execute(
            text("SELECT is_active, next_run_at FROM monitor_task WHERE id = :id"),
            {"id": ids["orphan_task"]},
        ).one()
        assert orphan.is_active is False and orphan.next_run_at is None
        owned_task = conn.execute(
            text("SELECT is_active FROM monitor_task WHERE id = :id"),
            {"id": ids["task_a"]},
        ).one()
        assert owned_task.is_active is True

        # Observations inherit an owner only from their own job.
        observations = {
            row.id: row.owner_user_id
            for row in conn.execute(text("SELECT id, owner_user_id FROM observation")).all()
        }
        assert observations[ids["obs_owned"]] == ids["user1"]
        assert observations[ids["obs_orphan"]] is None

        # Latest-state rows get a surrogate key and inherit the owner.
        latest = {
            row.size_label: (row.id, row.owner_user_id)
            for row in conn.execute(
                text("SELECT id, owner_user_id, size_label FROM observation_latest")
            ).all()
        }
        assert latest["42"][0] is not None and latest["42"][1] == ids["user1"]
        assert latest["43"][0] is not None and latest["43"][1] is None
        # The new surrogate key has a working server default.
        new_id = conn.execute(
            text(
                "INSERT INTO observation_latest (owner_user_id, store_id, style_id, size_label,"
                " gtin, availability, trust, price_status, promotions, baseline_ready, checked_at)"
                " VALUES (NULL, :store_id, :style_id, '44', 'G3', 'IN_STOCK', 'TRUSTED',"
                " 'MISSING', '[]'::jsonb, true, now()) RETURNING id"
            ),
            {"store_id": ids["store"], "style_id": ids["style"]},
        ).scalar_one()
        assert new_id is not None and new_id > 0

    engine.dispose()
    assert name
    del url


@pytest.mark.parametrize("kind", ["legacy_credential", "personal_job", "auth_limit"])
def test_downgrade_is_refused_when_personal_data_exists(temp_database, kind: str) -> None:
    name, url = temp_database
    config = alembic_config()
    engine = create_engine(url)
    command.upgrade(config, "head")

    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenant (id, slug, name, is_platform, is_active, daily_review_budget)"
                " VALUES (:id, 'x', '租户', false, true, 5000)"
            ),
            {"id": tenant_id},
        )
        conn.execute(
            text(
                "INSERT INTO app_user (id, tenant_id, email, display_name, password_hash, role,"
                " is_active) VALUES (:id, :tenant, 'x@example.com', 'x', 'x', 'tenant_admin', true)"
            ),
            {"id": user_id, "tenant": tenant_id},
        )
        if kind == "legacy_credential":
            conn.execute(
                text(
                    "INSERT INTO credential (id, owner_user_id, client_id, access_token_enc,"
                    " refresh_token_enc, status, last_error)"
                    " VALUES (:id, NULL, 'c', 'a', 'r', 'UNASSIGNED', '')"
                ),
                {"id": uuid.uuid4()},
            )
        elif kind == "personal_job":
            conn.execute(
                text(
                    "INSERT INTO scan_job (id, tenant_id, kind, status, store_codes, style_codes,"
                    " total_stores, done_stores, failed_stores, unknown_stores, attempt,"
                    " owner_user_id, error_summary)"
                    " VALUES (:id, :tenant, 'SCAN', 'SUCCEEDED', '[]'::jsonb, '[]'::jsonb, 0, 0, 0,"
                    " 0, 0, :owner, '')"
                ),
                {"id": uuid.uuid4(), "tenant": tenant_id, "owner": user_id},
            )
        else:
            conn.execute(
                text("INSERT INTO nike_auth_limit (owner_user_id) VALUES (:owner)"),
                {"owner": user_id},
            )

    with pytest.raises(RuntimeError, match="PERSONAL_DATA_DOWNGRADE_UNSAFE"):
        command.downgrade(config, "base")

    # 事务性 DDL：拒绝发生在任何 DDL 之前，数据库仍停在当前 head。
    assert _tables(engine) == {table.name for table in Base.metadata.sorted_tables}
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert version == _head_revision()
    engine.dispose()
    assert name
