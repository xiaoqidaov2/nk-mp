"""Turn active monitor tasks into queued scans, respecting cadence.

Dispatch is personal: the job's owner is the task creator. A task whose owner
is missing, disabled, or has no usable Nike binding simply skips this round
(the cadence keeps moving); the platform never falls back to a shared default
account, and the user's ``is_active`` intent is never changed here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import JobStatus, MonitorTask, ScanJob, Store, Tenant, User
from app.services.credentials import CredentialError, CredentialService
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


def dispatch_due_tasks(session: Session, settings: Settings) -> int:
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
        if not task.style_codes or task.created_by is None:
            task.next_run_at = datetime.now(UTC) + timedelta(seconds=task.cadence_seconds)
            continue

        user = session.execute(
            select(User).where(User.id == task.created_by)
        ).scalar_one_or_none()
        tenant = session.execute(
            select(Tenant).where(Tenant.id == task.tenant_id)
        ).scalar_one_or_none()
        if (
            user is None
            or not user.is_active
            or user.tenant_id != task.tenant_id
            or tenant is None
            or not tenant.is_active
        ):
            task.next_run_at = datetime.now(UTC) + timedelta(seconds=task.cadence_seconds)
            continue

        try:
            binding = CredentialService(settings, task.created_by).binding()
        except CredentialError:
            # No fallback credential: resume automatically once the user
            # re-connects, on the next cadence.
            task.next_run_at = datetime.now(UTC) + timedelta(seconds=task.cadence_seconds)
            continue

        store_codes = list(task.store_codes or [])
        if not store_codes:
            # "All stores" is resolved at dispatch time so newly synced
            # stores are covered without editing every task.
            store_codes = list(
                session.execute(
                    select(Store.store_code)
                    .where(
                        Store.owner_user_id == task.created_by,
                        Store.is_active.is_(True),
                    )
                    .order_by(Store.store_code)
                ).scalars()
            )
        if not store_codes:
            task.next_run_at = datetime.now(UTC) + timedelta(seconds=task.cadence_seconds)
            continue
        enqueue_job(
            session,
            tenant_id=task.tenant_id,
            store_codes=store_codes,
            style_codes=list(task.style_codes),
            task_id=task.id,
            kind="MONITOR",
            owner_user_id=task.created_by,
            credential_binding=binding,
        )
        task.next_run_at = datetime.now(UTC) + timedelta(seconds=task.cadence_seconds)
        dispatched += 1
    session.flush()
    return dispatched
