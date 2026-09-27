"""Turn active monitor tasks into queued scans, respecting cadence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import JobStatus, MonitorTask, ScanJob
from app.services.jobs import enqueue_job


def due_tasks(session: Session, limit: int = 50) -> list[MonitorTask]:
    now = datetime.now(UTC)
    return list(
        session.execute(
            select(MonitorTask)
            .where(
                MonitorTask.is_active.is_(True),
                (MonitorTask.next_run_at.is_(None)) | (MonitorTask.next_run_at <= now),
            )
            .order_by(MonitorTask.next_run_at.nullsfirst())
            .limit(limit)
        ).scalars()
    )


def dispatch_due_tasks(session: Session) -> int:
    dispatched = 0
    for task in due_tasks(session):
        active = session.execute(
            select(ScanJob).where(
                ScanJob.task_id == task.id,
                ScanJob.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value]),
            )
        ).scalars().first()
        if active is not None:
            # Keep the cadence moving without stacking duplicate scans.
            task.next_run_at = datetime.now(UTC) + timedelta(seconds=task.cadence_seconds)
            continue
        if not task.store_codes or not task.style_codes:
            task.next_run_at = datetime.now(UTC) + timedelta(seconds=task.cadence_seconds)
            continue
        enqueue_job(
            session,
            tenant_id=task.tenant_id,
            store_codes=list(task.store_codes),
            style_codes=list(task.style_codes),
            task_id=task.id,
            kind="MONITOR",
        )
        task.next_run_at = datetime.now(UTC) + timedelta(seconds=task.cadence_seconds)
        dispatched += 1
    session.flush()
    return dispatched
