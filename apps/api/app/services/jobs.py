"""Job leasing, execution and checkpoints.

Each store unit is committed independently as soon as it finishes, so a crash
or a stolen lease never rolls back work that already succeeded. A worker may
only keep working (and finalising) while it still owns the job lease.

Jobs are personal: the owner user and the credential binding are frozen at
enqueue time. A worker never derives the owner from ``requested_by`` or the
task, and a job whose binding was replaced or revoked stops instead of
silently switching accounts.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

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
    User,
)
from app.nike.client import NikeClient, NikeTransportError
from app.nike.errors import ErrorCode, ScanError
from app.services import push
from app.services.credentials import (
    CredentialBinding,
    CredentialError,
    CredentialRequired,
    CredentialService,
)
from app.services.observations import mark_failed_job_event, persist_observations
from app.services.probe import probe_store
from app.services.quota import QuotaGuard

logger = logging.getLogger("nike_mp.jobs")

ORPHAN_JOB_CODE = "NIKE_CREDENTIAL_REQUIRED"


class LeaseLost(RuntimeError):
    """Raised when another worker has taken over this job's lease."""


def enqueue_job(
    session: Session,
    tenant_id,
    store_codes: list[str],
    style_codes: list[str],
    task_id=None,
    requested_by=None,
    kind: str = "SCAN",
    *,
    owner_user_id: UUID,
    credential_binding: CredentialBinding,
) -> ScanJob:
    user = session.execute(select(User).where(User.id == owner_user_id)).scalar_one_or_none()
    if user is None or not user.is_active or user.tenant_id != tenant_id:
        raise CredentialRequired("需要先连接 Nike 账号")
    if credential_binding.owner_user_id != owner_user_id:
        raise CredentialRequired("需要先连接 Nike 账号")
    job = ScanJob(
        tenant_id=tenant_id,
        task_id=task_id,
        kind=kind,
        status=JobStatus.QUEUED.value,
        store_codes=list(store_codes),
        style_codes=list(style_codes),
        total_stores=len(store_codes),
        requested_by=requested_by,
        owner_user_id=owner_user_id,
        credential_id=credential_binding.credential_id,
        credential_generation=credential_binding.generation,
    )
    session.add(job)
    session.flush()

    stores = {
        store.store_code: store
        for store in session.execute(
            select(Store).where(
                Store.owner_user_id == owner_user_id, Store.store_code.in_(store_codes)
            )
        ).scalars()
    }
    for store_code in store_codes:
        store = stores.get(store_code)
        if store is None:
            continue
        session.add(ScanJobStore(job_id=job.id, store_id=store.id))
    session.flush()
    return job


def lease_next_job(session: Session, settings: Settings, worker_id: str | None = None) -> ScanJob | None:
    now = datetime.now(UTC)
    assignable = (
        ScanJob.owner_user_id.is_not(None),
        ScanJob.credential_id.is_not(None),
        ScanJob.credential_generation.is_not(None),
    )
    job = session.execute(
        select(ScanJob)
        .where(
            ScanJob.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value]),
            (ScanJob.lease_expires_at.is_(None)) | (ScanJob.lease_expires_at < now),
            *assignable,
        )
        .order_by(ScanJob.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()
    if job is None:
        # Defensive: a job without a frozen binding can never run. Terminate
        # it instead of leaving it in the queue forever.
        orphans = session.execute(
            select(ScanJob).where(
                ScanJob.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value]),
                (ScanJob.lease_expires_at.is_(None)) | (ScanJob.lease_expires_at < now),
                ~(
                    ScanJob.owner_user_id.is_not(None)
                    & ScanJob.credential_id.is_not(None)
                    & ScanJob.credential_generation.is_not(None)
                ),
            ).limit(20)
        ).scalars().all()
        for orphan in orphans:
            _finalize(session, settings, orphan, JobStatus.FAILED.value, ORPHAN_JOB_CODE, None)
        if orphans:
            logger.warning("terminated %s job(s) without a frozen credential binding", len(orphans))
        return None
    job.status = JobStatus.RUNNING.value
    job.attempt += 1
    job.started_at = job.started_at or now
    job.heartbeat_at = now
    job.lease_expires_at = now + timedelta(seconds=settings.worker_lease_seconds)
    job.leased_by = worker_id
    session.flush()
    return job


def heartbeat(session: Session, job: ScanJob, settings: Settings, worker_id: str | None = None) -> None:
    now = datetime.now(UTC)
    stmt = update(ScanJob).where(ScanJob.id == job.id)
    if worker_id is not None:
        stmt = stmt.where(ScanJob.leased_by == worker_id)
    result = session.execute(
        stmt.values(
            heartbeat_at=now,
            lease_expires_at=now + timedelta(seconds=settings.worker_lease_seconds),
        )
    )
    if worker_id is not None and result.rowcount == 0:
        raise LeaseLost(f"lease for job {job.id} was taken over by another worker")
    session.flush()


def _lease_held(session: Session, job: ScanJob, worker_id: str | None) -> bool:
    """Guard result writes: a worker that lost the lease must write nothing."""
    if worker_id is None:
        return True
    current = session.execute(
        select(ScanJob.leased_by).where(ScanJob.id == job.id)
    ).scalar_one_or_none()
    return current == worker_id


def _auth_code(exc: ScanError) -> str:
    message = (exc.message or "").strip()
    if message.startswith("NIKE_"):
        return message
    return "NIKE_CREDENTIAL_REQUIRED"


def run_job(session: Session, settings: Settings, job: ScanJob, worker_id: str | None = None) -> None:
    if (
        job.owner_user_id is None
        or job.credential_id is None
        or job.credential_generation is None
    ):
        _finalize(session, settings, job, JobStatus.FAILED.value, ORPHAN_JOB_CODE, worker_id)
        return

    binding = CredentialBinding(
        owner_user_id=job.owner_user_id,
        credential_id=job.credential_id,
        generation=job.credential_generation,
    )
    credentials = CredentialService(settings, job.owner_user_id)
    try:
        credentials.assert_binding(binding)
        token = credentials.access_token(binding=binding)
    except CredentialError as exc:
        # No upstream request is issued when the credential is not usable.
        _finalize(session, settings, job, JobStatus.FAILED.value, exc.code, worker_id)
        return

    client = NikeClient(
        settings,
        token,
        token_provider=credentials.token_provider(binding),
        credential_guard=lambda: credentials.assert_binding(binding),
    )
    try:
        tenant = session.execute(
            select(Tenant).where(Tenant.id == job.tenant_id)
        ).scalar_one_or_none()
        tenant_budget = tenant.daily_review_budget if tenant else 5000
        quota = QuotaGuard(
            job.tenant_id,
            settings.daily_review_budget,
            tenant_budget,
            settings.quota_hourly_cap,
            settings.quota_hourly_slow_line,
        )

        max_gap_seconds = settings.baseline_max_gap_seconds
        if job.task_id:
            task = session.execute(
                select(MonitorTask).where(MonitorTask.id == job.task_id)
            ).scalar_one_or_none()
            if task is not None:
                # Invariant: the baseline window must always span at least
                # three full cadences, otherwise a degraded cadence could
                # silently suppress every event.
                max_gap_seconds = max(max_gap_seconds, 3 * task.cadence_seconds)

        rows = session.execute(
            select(ScanJobStore)
            .where(ScanJobStore.job_id == job.id)
            .order_by(ScanJobStore.id)
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
                session.commit()
                continue
            if row.status == StoreJobStatus.SUCCEEDED.value:
                continue

            try:
                heartbeat(session, job, settings, worker_id)
            except LeaseLost:
                session.rollback()
                logger.warning("job %s lease lost before store %s", job.id, store.store_code)
                return
            # Persist the renewed lease before the (possibly slow) probe so
            # other workers can see this job is still alive.
            session.commit()

            row.status = StoreJobStatus.RUNNING.value
            row.attempt += 1
            row.started_at = datetime.now(UTC)
            session.flush()

            try:
                result = probe_store(
                    session, client, settings, quota, store, job.style_codes, job.id
                )
            except LeaseLost:
                logger.warning("job %s lease lost while probing %s", job.id, store.store_code)
                return
            except (ScanError, NikeTransportError) as exc:
                code = (
                    exc.code.value
                    if isinstance(exc, ScanError)
                    else ErrorCode.TRANSPORT_ERROR.value
                )
                if not _lease_held(session, job, worker_id):
                    session.rollback()
                    logger.warning(
                        "job %s lease lost; discarding failure for %s",
                        job.id,
                        store.store_code,
                    )
                    return
                if isinstance(exc, ScanError) and exc.code == ErrorCode.AUTH_REQUIRED:
                    _abort_auth_failure(session, settings, job, rows, stores, _auth_code(exc), worker_id)
                    return
                _fail_store_row(session, job, row, store, code, str(exc))
                session.commit()
                continue
            except Exception as exc:  # noqa: BLE001 - never lose a store silently
                if not _lease_held(session, job, worker_id):
                    session.rollback()
                    logger.warning(
                        "job %s lease lost; discarding failure for %s",
                        job.id,
                        store.store_code,
                    )
                    return
                _fail_store_row(
                    session, job, row, store, ErrorCode.UNEXPECTED_STATUS.value, str(exc)
                )
                session.commit()
                continue

            # The probe may have taken long enough for the lease to be stolen;
            # results must not be written by a worker that no longer owns it.
            if not _lease_held(session, job, worker_id):
                session.rollback()
                logger.warning(
                    "job %s lease lost; discarding probe result for %s",
                    job.id,
                    store.store_code,
                )
                return
            row.finished_at = datetime.now(UTC)
            if not result.trusted:
                row.status = StoreJobStatus.UNKNOWN.value
                row.error_code = result.error_code
                row.message = result.message[:300]
                mark_failed_job_event(
                    session, job.tenant_id, job, store.id, result.error_code or "UNKNOWN"
                )
                session.commit()
                continue

            summary = persist_observations(
                session,
                result.observations,
                binding=binding,
                max_gap_seconds=max_gap_seconds,
                suppress_structures=result.structure_changes,
            )
            row.status = StoreJobStatus.SUCCEEDED.value
            row.error_code = None
            row.message = ""
            row.observations_written = len(result.observations)
            row.events_written = (
                summary.restock + summary.sold_out + summary.price_change + summary.new_arrival
            )
            session.commit()

        _finalize(session, settings, job, None, "", worker_id)
    finally:
        client.close()


def _abort_auth_failure(
    session: Session,
    settings: Settings,
    job: ScanJob,
    rows: list[ScanJobStore],
    stores: dict,
    code: str,
    worker_id: str | None,
) -> None:
    """Terminate a job whose credential became unusable mid-run.

    Successful store checkpoints stay; every unfinished store is failed with
    the stable code and the job itself ends as FAILED with that code. No
    further upstream request is made for this job.
    """
    now = datetime.now(UTC)
    for row in rows:
        if row.status == StoreJobStatus.SUCCEEDED.value:
            continue
        row.status = StoreJobStatus.FAILED.value
        row.error_code = code
        row.message = code
        row.finished_at = now
        store = stores.get(row.store_id)
        if store is not None:
            mark_failed_job_event(session, job.tenant_id, job, store.id, code)
    _finalize(session, settings, job, JobStatus.FAILED.value, code, worker_id)


def _fail_store_row(session, job, row, store, code: str, message: str) -> None:
    row.status = StoreJobStatus.FAILED.value
    row.error_code = code
    row.message = message[:300]
    row.finished_at = datetime.now(UTC)
    mark_failed_job_event(session, job.tenant_id, job, store.id, code)
    session.flush()


def _finalize(
    session: Session,
    settings: Settings | None,
    job: ScanJob,
    forced_status: str | None,
    error_summary: str,
    worker_id: str | None = None,
) -> None:
    if worker_id is not None:
        fresh = session.execute(
            select(ScanJob).where(ScanJob.id == job.id).with_for_update()
        ).scalar_one_or_none()
        if fresh is None or fresh.leased_by != worker_id:
            raise LeaseLost(f"job {job.id} is no longer owned by this worker")

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
    job.leased_by = None
    session.flush()

    if job.task_id:
        task = session.execute(
            select(MonitorTask).where(MonitorTask.id == job.task_id)
        ).scalar_one_or_none()
        if task is not None:
            task.last_run_at = datetime.now(UTC)
            task.next_run_at = datetime.now(UTC) + timedelta(seconds=task.cadence_seconds)
            session.flush()

    # 检查结束后按任务的提醒开关聚合推送；提交由调用方负责。
    if settings is not None:
        push.enqueue_for_job(session, settings, job)


def requeue_failed_stores(session: Session, job: ScanJob) -> int:
    """Requeue failed and data-incomplete stores for another attempt.

    The frozen credential binding is kept; the caller must first verify the
    binding is still current (a replaced account has to start a new scan).
    """
    rows = session.execute(
        select(ScanJobStore).where(
            ScanJobStore.job_id == job.id,
            ScanJobStore.status.in_(
                [StoreJobStatus.FAILED.value, StoreJobStatus.UNKNOWN.value]
            ),
        )
    ).scalars().all()
    for row in rows:
        row.status = StoreJobStatus.PENDING.value
        row.error_code = None
        row.message = ""
    job.status = JobStatus.QUEUED.value
    job.finished_at = None
    job.lease_expires_at = None
    job.leased_by = None
    job.attempt = 0
    session.flush()
    return len(rows)
