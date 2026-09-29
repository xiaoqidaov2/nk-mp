"""PostgreSQL integration fixtures.

These tests require ``NIKE_MP_TEST_DATABASE_URL`` to point at a scratch
database; otherwise the whole directory is skipped. They must never run
against the configured runtime database.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

TEST_DB_URL = os.environ.get("NIKE_MP_TEST_DATABASE_URL", "")


def _database_reachable(url: str) -> bool:
    if not url:
        return False
    try:
        from sqlalchemy import create_engine

        engine = create_engine(url, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:  # noqa: BLE001 - unavailable scratch database means "skip"
        return False


if not _database_reachable(TEST_DB_URL):
    collect_ignore_glob = ["test_*.py"]


@pytest.fixture(scope="session", autouse=True)
def _schema():
    from app.bootstrap import create_schema

    create_schema()


@pytest.fixture(autouse=True)
def _clean_tables():
    from app.db import Base, engine

    tables = ", ".join(f'"{table.name}"' for table in reversed(Base.metadata.sorted_tables))
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
