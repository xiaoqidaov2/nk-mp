"""Job leasing, execution and checkpoints."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    JobStatus,
    MonitorTask,
    ScanJob,
    ScanJobStore,
    Store,
    StoreJobStatus,
    Tenant,
)
from app.nike.client import NikeClient, NikeTransportError
from app.nike.errors import ErrorCode, ScanError
from app.services.credentials import CredentialNotConfigured, CredentialService
from app.services.observations import mark_failed_job_event, persist_observations
from app.services.probe import probe_store
from app.services.quota import QuotaGuard


def enqueue_job(
    session: Session,
    tenant_id,
    store_codes: list[str],
    style_codes: list[str],
    task_id=None,
    requested_by=None,
    kind: str = "SCAN",
) -> ScanJob:
    job = ScanJob(
        tenant_id=tenant_id,
        task_id=task_id,
        kind=kind,
        status=JobStatus.QUEUED.value,
        store_codes=list(store_codes),
        style_codes=list(style_codes),
        total_stores=len(store_codes),
        requested_by=requested_by,
    )
    session.add(job)
    session.flush()

    stores = {
        store.store_code: store
        for store in session.execute(
            select(Store).where(Store.store_code.in_(store_codes))
        ).scalars()
    }
    for store_code in store_codes:
        store = stores.get(store_code)
        if store is None:
            continue
        session.add(ScanJobStore(job_id=job.id, store_id=store.id))
    session.flush()
    return job


def lease_next_job(session: Session, settings: Settings) -> ScanJob | None:
    now = datetime.now(UTC)
    job = session.execute(
        select(ScanJob)
        .where(
            ScanJob.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value]),
            (ScanJob.lease_expires_at.is_(None)) | (ScanJob.lease_expires_at < now),
        )
        .order_by(ScanJob.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()
    if job is None:
        return None
    job.status = JobStatus.RUNNING.value
    job.attempt += 1
    job.started_at = job.started_at or now
    job.heartbeat_at = now
    job.lease_expires_at = now + timedelta(seconds=settings.worker_lease_seconds)
    session.flush()
    return job


def heartbeat(session: Session, job: ScanJob, settings: Settings) -> None:
    now = datetime.now(UTC)
    session.execute(
        update(ScanJob)
        .where(ScanJob.id == job.id)
        .values(
            heartbeat_at=now,
            lease_expires_at=now + timedelta(seconds=settings.worker_lease_seconds),
        )
    )
    session.flush()


def run_job(session: Session, settings: Settings, job: ScanJob) -> None:
    credentials = CredentialService(settings, session)
    try:
        token = credentials.access_token()
    except CredentialNotConfigured as exc:
        _finalize(session, settings, job, JobStatus.FAILED.value, str(exc))
        return

    client = NikeClient(settings, token, token_provider=credentials.access_token)
    try:
        tenant = session.execute(
            select(Tenant).where(Tenant.id == job.tenant_id)
        ).scalar_one_or_none()
        tenant_budget = tenant.daily_review_budget if tenant else 5000
        quota = QuotaGuard(job.tenant_id, settings.daily_review_budget, tenant_budget)

        rows = session.execute(
            select(ScanJobStore).where(ScanJobStore.job_id == job.id)
        ).scalars().all()
        stores = {
            store.id: store
            for store in session.execute(
                select(Store).where(Store.id.in_([row.store_id for row in rows]))
            ).scalars()
        }

        for row in rows:
            store = stores.get(row.store_id)
            if store is None:
                row.status = StoreJobStatus.FAILED.value
                row.error_code = ErrorCode.STRUCTURE_MISSING.value
                row.message = "store not found"
                row.finished_at = datetime.now(UTC)
                continue
            if row.status == StoreJobStatus.SUCCEEDED.value:
                continue

            heartbeat(session, job, settings)
            row.status = StoreJobStatus.RUNNING.value
            row.attempt += 1
            row.started_at = datetime.now(UTC)
            session.flush()

            try:
                result = probe_store(
                    session, client, settings, quota, store, job.style_codes, job.id
                )
            except (ScanError, NikeTransportError) as exc:
                code = (
                    exc.code.value
                    if isinstance(exc, ScanError)
                    else ErrorCode.TRANSPORT_ERROR.value
                )
                _fail_store_row(session, job, row, store, code, str(exc))
                continue
            except Exception as exc:  # noqa: BLE001 - never lose a store silently
                _fail_store_row(
                    session, job, row, store, ErrorCode.UNEXPECTED_STATUS.value, str(exc)
                )
                continue

            row.finished_at = datetime.now(UTC)
            if not result.trusted:
                row.status = StoreJobStatus.UNKNOWN.value
                row.error_code = result.error_code
                row.message = result.message[:300]
                mark_failed_job_event(
                    session, job.tenant_id, job, store.id, result.error_code or "UNKNOWN"
                )
                session.flush()
                continue

            summary = persist_observations(session, job.tenant_id, result.observations)
            row.status = StoreJobStatus.SUCCEEDED.value
            row.error_code = None
            row.message = ""
            row.observations_written = len(result.observations)
            row.events_written = (
                summary.restock + summary.sold_out + summary.price_change + summary.new_arrival
            )
            session.flush()

        _finalize(session, settings, job, None, "")
    finally:
        client.close()


def _fail_store_row(session, job, row, store, code: str, message: str) -> None:
    row.status = StoreJobStatus.FAILED.value
    row.error_code = code
    row.message = message[:300]
    row.finished_at = datetime.now(UTC)
    mark_failed_job_event(session, job.tenant_id, job, store.id, code)
    session.flush()


def _finalize(
    session: Session,
    settings: Settings,
    job: ScanJob,
    forced_status: str | None,
    error_summary: str,
) -> None:
    rows = session.execute(
        select(ScanJobStore).where(ScanJobStore.job_id == job.id)
    ).scalars().all()
    succeeded = sum(1 for row in rows if row.status == StoreJobStatus.SUCCEEDED.value)
    failed = sum(1 for row in rows if row.status == StoreJobStatus.FAILED.value)
    unknown = sum(1 for row in rows if row.status == StoreJobStatus.UNKNOWN.value)
    pending = sum(1 for row in rows if row.status == StoreJobStatus.PENDING.value)

    job.done_stores = succeeded
    job.failed_stores = failed
    job.unknown_stores = unknown
    if error_summary:
        job.error_summary = error_summary[:500]

    if forced_status:
        job.status = forced_status
    elif pending == 0 and failed == 0 and unknown == 0:
        job.status = JobStatus.SUCCEEDED.value
    elif succeeded > 0:
        job.status = JobStatus.PARTIAL.value
    else:
        job.status = JobStatus.FAILED.value

    job.finished_at = datetime.now(UTC)
    job.lease_expires_at = None
    job.heartbeat_at = datetime.now(UTC)
    session.flush()

    if job.task_id:
        task = session.execute(
            select(MonitorTask).where(MonitorTask.id == job.task_id)
        ).scalar_one_or_none()
        if task is not None:
            task.last_run_at = datetime.now(UTC)
            task.next_run_at = datetime.now(UTC) + timedelta(seconds=task.cadence_seconds)
            session.flush()


def requeue_failed_stores(session: Session, job: ScanJob) -> int:
    rows = session.execute(
        select(ScanJobStore).where(
            ScanJobStore.job_id == job.id, ScanJobStore.status == StoreJobStatus.FAILED.value
        )
    ).scalars().all()
    for row in rows:
        row.status = StoreJobStatus.PENDING.value
        row.error_code = None
        row.message = ""
    job.status = JobStatus.QUEUED.value
    job.finished_at = None
    job.lease_expires_at = None
    job.attempt = 0
    session.flush()
    return len(rows)
