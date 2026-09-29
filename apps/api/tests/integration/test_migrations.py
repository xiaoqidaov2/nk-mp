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
