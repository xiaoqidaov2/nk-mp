"""T03: job checkpoints, lease ownership and restart recovery.

Uses a real PostgreSQL session and a scripted upstream client; no HTTP.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import (
    JobStatus,
    Observation,
    ObservationLatest,
    ScanJob,
    ScanJobStore,
    Store,
    StoreJobStatus,
)
from app.nike.types import PriceInfo, ReviewBatch, ReviewItem, SizeEntry, SizeStructure
from app.services import jobs as jobs_module
from app.services.credentials import CredentialService
from app.services.jobs import enqueue_job, lease_next_job, requeue_failed_stores, run_job

from .helpers import make_store, make_style, make_tenant


class FakeNikeClient:
    """Scripted upstream client; behaviour keyed by region id."""

    behaviours: dict[str, str] = {}
    calls: list[tuple[str, bool]] = []

    def __init__(self, settings, access_token, token_provider=None):  # noqa: ANN001
        self.settings = settings
        self.access_token = access_token
        self.token_provider = token_provider
        self.transaction_listener = None

    def fetch_structure(self, store_code: str, style_code: str) -> SizeStructure:
        return SizeStructure(
            store_code, style_code, "OK", "测试标题", (SizeEntry("42", f"G-{store_code}"),)
        )

    def review(self, gtins, region_id, with_price=False):  # noqa: ANN001
        type(self).calls.append((region_id, with_price))
        behaviour = self.behaviours.get(region_id, "ok")
        if behaviour == "crash":
            raise KeyboardInterrupt("simulated process abort")
        if behaviour == "takeover":
            # Another worker steals the lease while this one is probing.
            with SessionLocal() as db:
                job = db.execute(select(ScanJob)).scalars().first()
                job.leased_by = "workerB"
                job.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
                db.commit()
            behaviour = "ok"
        if behaviour == "untrusted":
            return ReviewBatch(
                items={gtin: ReviewItem(gtin=gtin, availability="UNKNOWN") for gtin in gtins},
                trusted=False,
                error_code="EMPTY_COMPLETED",
                message="simulated empty completion",
            )
        price = PriceInfo(total_price=99.0, price=99.0) if with_price else None
        return ReviewBatch(
            items={
                gtin: ReviewItem(gtin=gtin, availability="IN_STOCK", price=price)
                for gtin in gtins
            },
            trusted=True,
        )

    def close(self) -> None:
        return None


@pytest.fixture
def seeded():
    settings = get_settings()
    with SessionLocal() as db:
        tenant = make_tenant(db, budget=100000)
        make_store(db, code="S1", name="一店", region_id="R1")
        make_store(db, code="S2", name="二店", region_id="R2")
        make_style(db, code="DN3707-202")
        CredentialService(settings, db).store_tokens("access-token", "refresh-token")
        db.commit()
        return {"tenant_id": tenant.id}


def _enqueue(tenant_id) -> str:  # noqa: ANN001
    with SessionLocal() as db:
        job = enqueue_job(
            db, tenant_id=tenant_id, store_codes=["S1", "S2"], style_codes=["DN3707-202"]
        )
        db.commit()
        return job.id


def _lease(job_id, worker_id: str = "workerA") -> str:
    settings = get_settings()
    with SessionLocal() as db:
        leased = lease_next_job(db, settings, worker_id)
        assert leased is not None and leased.id == job_id
        db.commit()
    return job_id


def _run(job_id, worker_id: str = "workerA") -> None:
    settings = get_settings()
    with SessionLocal() as db:
        job = db.get(ScanJob, job_id)
        assert job is not None
        run_job(db, settings, job, worker_id)
        # The worker main loop commits after a successful run.
        db.commit()


def _store_id(code: str):
    with SessionLocal() as db:
        return db.execute(select(Store.id).where(Store.store_code == code)).scalar_one()


def _rows(job_id) -> dict:
    with SessionLocal() as db:
        return {
            row.store_id: row
            for row in db.execute(
                select(ScanJobStore).where(ScanJobStore.job_id == job_id)
            ).scalars()
        }


def test_completed_store_is_committed_before_a_crash(seeded, monkeypatch) -> None:
    """Regression: per-unit checkpoints must survive a process abort."""
    monkeypatch.setattr(jobs_module, "NikeClient", FakeNikeClient)
    FakeNikeClient.calls = []
    FakeNikeClient.behaviours = {"R1": "ok", "R2": "crash"}

    job_id = _lease(_enqueue(seeded["tenant_id"]))
    with pytest.raises(KeyboardInterrupt):
        _run(job_id)

    store1, store2 = _store_id("S1"), _store_id("S2")
    rows = _rows(job_id)
    assert rows[store1].status == StoreJobStatus.SUCCEEDED.value
    assert rows[store2].status == StoreJobStatus.PENDING.value

    with SessionLocal() as db:
        observations = db.execute(select(Observation)).scalars().all()
        assert len(observations) == 1
        assert observations[0].availability == "IN_STOCK"
        assert observations[0].job_id is not None
        latest = db.execute(select(ObservationLatest)).scalars().all()
        assert len(latest) == 1
    with SessionLocal() as db:
        job = db.get(ScanJob, job_id)
        assert job.status == JobStatus.RUNNING.value
        assert job.finished_at is None


def test_stolen_lease_stops_the_old_worker(seeded, monkeypatch) -> None:
    """Regression: a worker that lost its lease must not write results."""
    monkeypatch.setattr(jobs_module, "NikeClient", FakeNikeClient)
    FakeNikeClient.calls = []
    FakeNikeClient.behaviours = {"R1": "takeover", "R2": "ok"}

    job_id = _lease(_enqueue(seeded["tenant_id"]))
    _run(job_id)

    store1, store2 = _store_id("S1"), _store_id("S2")
    rows = _rows(job_id)
    assert rows[store1].status == StoreJobStatus.PENDING.value
    assert rows[store2].status == StoreJobStatus.PENDING.value
    with SessionLocal() as db:
        job = db.get(ScanJob, job_id)
        assert job.leased_by == "workerB"
        assert job.status == JobStatus.RUNNING.value
        assert job.finished_at is None
        assert db.execute(select(Observation)).scalars().all() == []


def test_only_one_worker_can_hold_the_lease(seeded) -> None:
    settings = get_settings()
    job_id = _enqueue(seeded["tenant_id"])

    db1, db2 = SessionLocal(), SessionLocal()
    try:
        first = lease_next_job(db1, settings, "workerA")
        assert first is not None and first.id == job_id
        # Not yet committed: the row lock is held, and a committed fresh lease
        # must not be stealable either.
        assert lease_next_job(db2, settings, "workerB") is None
        db1.commit()
        assert lease_next_job(db2, settings, "workerB") is None

        with SessionLocal() as db:
            stale = db.get(ScanJob, job_id)
            stale.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            db.commit()

        second = lease_next_job(db2, settings, "workerB")
        assert second is not None and second.id == job_id
        assert second.leased_by == "workerB"
        assert second.attempt == 2
        db2.commit()
    finally:
        db1.close()
        db2.close()


def test_retry_requeues_only_incomplete_stores(seeded, monkeypatch) -> None:
    monkeypatch.setattr(jobs_module, "NikeClient", FakeNikeClient)
    FakeNikeClient.calls = []
    FakeNikeClient.behaviours = {"R2": "untrusted"}

    job_id = _lease(_enqueue(seeded["tenant_id"]))
    _run(job_id)

    store1, store2 = _store_id("S1"), _store_id("S2")
    rows = _rows(job_id)
    assert rows[store1].status == StoreJobStatus.SUCCEEDED.value
    assert rows[store2].status == StoreJobStatus.UNKNOWN.value

    with SessionLocal() as db:
        job = db.get(ScanJob, job_id)
        assert job.status == JobStatus.PARTIAL.value
        assert requeue_failed_stores(db, job) == 1
        db.commit()

    FakeNikeClient.behaviours = {"R2": "ok"}
    r1_calls = sum(1 for region, _ in FakeNikeClient.calls if region == "R1")
    _lease(job_id)
    _run(job_id)

    rows = _rows(job_id)
    assert rows[store2].status == StoreJobStatus.SUCCEEDED.value
    assert (
        sum(1 for region, _ in FakeNikeClient.calls if region == "R1") == r1_calls
    ), "the already-succeeded store must not be probed again"
    with SessionLocal() as db:
        job = db.get(ScanJob, job_id)
        assert job.status == JobStatus.SUCCEEDED.value
        assert job.finished_at is not None
