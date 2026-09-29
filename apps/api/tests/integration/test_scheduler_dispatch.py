"""Scheduler dispatch: all-store tasks resolve at dispatch time."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from app.db import SessionLocal
from app.models import JobStatus, MonitorTask, ScanJob, ScanJobStore, Store
from app.services.scheduler import dispatch_due_tasks

from .helpers import make_store, make_style, make_tenant


def test_all_store_task_expands_to_active_stores_at_dispatch() -> None:
    with SessionLocal() as db:
        tenant = make_tenant(db)
        make_store(db, code="S1", region_id="R1")
        make_store(db, code="S2", region_id="R2")
        make_style(db, code="DN3707-202")
        task = MonitorTask(
            tenant_id=tenant.id,
            name="全国任务",
            store_codes=[],
            style_codes=["DN3707-202"],
            all_stores=True,
            is_active=True,
            next_run_at=datetime.now(UTC),
        )
        db.add(task)
        db.commit()
        task_id = task.id

    with SessionLocal() as db:
        dispatched = dispatch_due_tasks(db)
        db.commit()
        assert dispatched == 1

    with SessionLocal() as db:
        job = db.execute(select(ScanJob).where(ScanJob.task_id == task_id)).scalar_one()
        assert sorted(job.store_codes) == ["S1", "S2"]
        rows = db.execute(select(ScanJobStore).where(ScanJobStore.job_id == job.id)).scalars().all()
        assert len(rows) == 2
        assert job.kind == "MONITOR"

        # A second tick must not stack another active scan for the same task.
        task = db.get(MonitorTask, task_id)
        task.next_run_at = datetime.now(UTC)
        task.cadence_seconds = 300
        db.commit()
    with SessionLocal() as db:
        assert dispatch_due_tasks(db) == 0
        db.commit()

    with SessionLocal() as db:
        active = db.execute(
            select(ScanJob).where(
                ScanJob.task_id == task_id,
                ScanJob.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value]),
            )
        ).scalars().all()
        assert len(active) == 1
        assert db.execute(select(Store)).scalars().all() is not None
