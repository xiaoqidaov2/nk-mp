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
import threading
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import SessionLocal
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
from app.services import push, throttle
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

#: 门店并发的硬上限。实测（2026-10-06）：405 拦截率在 4→16 并发（23→94 次/秒）下稳定在
#: 57–63%，不随速率升级；4 并发即 8.4 次/秒通过（≈3 万/小时），16 并发 33.2 次/秒（≈12 万/小时）。
MAX_STORE_CONCURRENCY = 32

#: 需要账号级冷却的失败码：额度用尽或被上游限速，剩余门店不再逐店空转。
LIMITED_CODES = (ErrorCode.BUDGET_EXHAUSTED.value, ErrorCode.THROTTLED.value)
#: 凭证类失败码不能触发自动补跑（补跑前必须先让用户重新验证）。
CREDENTIAL_CODES = (ErrorCode.AUTH_REQUIRED.value, ORPHAN_JOB_CODE)


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
    size_labels: list[str] | None = None,
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
        size_labels=list(size_labels or []),
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
            (ScanJob.not_before.is_(None)) | (ScanJob.not_before <= now),
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


@dataclass(frozen=True)
class _JobRef:
    """线程安全的作业快照：跨线程只传值，绝不共享 ORM 对象/session。"""

    id: UUID
    tenant_id: UUID
    owner_user_id: UUID | None
    credential_id: UUID | None
    credential_generation: int | None
    style_codes: list[str] = field(default_factory=list)
    size_labels: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _StoreOutcome:
    """一个门店探测的结果；由协调者决定收尾动作。"""

    kind: str  # "done" | "lease_lost" | "limited" | "auth"
    code: str = ""


def _make_client(
    settings: Settings, credentials: CredentialService, binding: CredentialBinding
) -> NikeClient:
    """为当前线程新建一个上游客户端（transaction_listener 属于单个 client）。"""
    return NikeClient(
        settings,
        credentials.access_token(binding=binding),
        token_provider=credentials.token_provider(binding),
        credential_guard=lambda: credentials.assert_binding(binding),
    )


def _run_store(
    settings: Settings,
    job: _JobRef,
    session: Session,
    client: NikeClient,
    quota: QuotaGuard,
    binding: CredentialBinding,
    max_gap_seconds: int,
    store: Store,
    row: ScanJobStore,
    worker_id: str | None,
) -> _StoreOutcome:
    """探测一个门店并落检查点；session 归调用方所有。"""
    if not _lease_held(session, job, worker_id):
        session.rollback()
        logger.warning("job %s lease lost; discarding probe for %s", job.id, store.store_code)
        return _StoreOutcome("lease_lost")

    row.status = StoreJobStatus.RUNNING.value
    row.attempt += 1
    row.started_at = datetime.now(UTC)
    session.flush()

    try:
        result = probe_store(
            session,
            client,
            settings,
            quota,
            store,
            job.style_codes,
            job.id,
            size_labels=list(job.size_labels or []),
        )
    except LeaseLost:
        logger.warning("job %s lease lost while probing %s", job.id, store.store_code)
        return _StoreOutcome("lease_lost")
    except (ScanError, NikeTransportError) as exc:
        code = exc.code.value if isinstance(exc, ScanError) else ErrorCode.TRANSPORT_ERROR.value
        if not _lease_held(session, job, worker_id):
            session.rollback()
            logger.warning("job %s lease lost; discarding failure for %s", job.id, store.store_code)
            return _StoreOutcome("lease_lost")
        if isinstance(exc, ScanError) and exc.code == ErrorCode.AUTH_REQUIRED:
            session.rollback()
            return _StoreOutcome("auth", _auth_code(exc))
        if isinstance(exc, ScanError) and exc.code.value in LIMITED_CODES:
            # 额度/限速：剩余门店不再逐店空转，整轮冷却后自动补跑。
            session.rollback()
            return _StoreOutcome("limited", code)
        _fail_store_row(session, job, row, store, code, str(exc))
        session.commit()
        return _StoreOutcome("done", code)
    except Exception as exc:  # noqa: BLE001 - never lose a store silently
        if not _lease_held(session, job, worker_id):
            session.rollback()
            logger.warning("job %s lease lost; discarding failure for %s", job.id, store.store_code)
            return _StoreOutcome("lease_lost")
        _fail_store_row(session, job, row, store, ErrorCode.UNEXPECTED_STATUS.value, str(exc))
        session.commit()
        return _StoreOutcome("done", ErrorCode.UNEXPECTED_STATUS.value)

    # The probe may have taken long enough for the lease to be stolen;
    # results must not be written by a worker that no longer owns it.
    if not _lease_held(session, job, worker_id):
        session.rollback()
        logger.warning("job %s lease lost; discarding probe result for %s", job.id, store.store_code)
        return _StoreOutcome("lease_lost")
    row.finished_at = datetime.now(UTC)
    if not result.trusted:
        if result.error_code in LIMITED_CODES:
            session.rollback()
            return _StoreOutcome("limited", result.error_code or "")
        row.status = StoreJobStatus.UNKNOWN.value
        row.error_code = result.error_code
        row.message = result.message[:300]
        mark_failed_job_event(session, job.tenant_id, job, store.id, result.error_code or "UNKNOWN")
        session.commit()
        return _StoreOutcome("done", result.error_code or "")

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
    return _StoreOutcome("done")


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

        concurrency = max(1, min(int(settings.store_concurrency), MAX_STORE_CONCURRENCY))
        if concurrency > 1:
            # 门店级并发：每店独立 session/client；本函数只做协调（心跳、统一中止、收尾）。
            _run_stores_concurrently(
                settings,
                job,
                session,
                credentials,
                rows,
                stores,
                quota,
                binding,
                max_gap_seconds,
                worker_id,
                concurrency,
            )
            return

        waf_failures = 0
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

            outcome = _run_store(
                settings,
                _job_ref(job),
                session,
                client,
                quota,
                binding,
                max_gap_seconds,
                store,
                row,
                worker_id,
            )
            if outcome.kind == "lease_lost":
                return
            if outcome.kind == "auth":
                _abort_auth_failure(session, settings, job, rows, stores, outcome.code, worker_id)
                return
            if outcome.kind == "limited":
                _abort_limited(session, settings, job, rows, stores, outcome.code, quota, worker_id)
                return
            if outcome.code == ErrorCode.WAF_CHALLENGE.value:
                waf_failures += 1
                if (
                    settings.waf_storm_store_limit
                    and waf_failures >= settings.waf_storm_store_limit
                ):
                    # 405 挑战风暴：上游在连续拦我们，整轮结束并进入冷却，
                    # 剩余门店由补跑在窗口结束后处理，别把一小时耗在墙上。
                    logger.warning(
                        "job %s: %s store(s) exhausted the WAF challenge; aborting the round",
                        job.id,
                        waf_failures,
                    )
                    _abort_limited(
                        session,
                        settings,
                        job,
                        rows,
                        stores,
                        ErrorCode.WAF_CHALLENGE.value,
                        quota,
                        worker_id,
                        reason=throttle.THROTTLED_REASON,
                    )
                    return

        _finalize(session, settings, job, None, "", worker_id)
    finally:
        client.close()


def _job_ref(job: ScanJob) -> _JobRef:
    """跨线程只传值：ORM 对象与 session 绝不进入工作线程。"""
    return _JobRef(
        id=job.id,
        tenant_id=job.tenant_id,
        owner_user_id=job.owner_user_id,
        credential_id=job.credential_id,
        credential_generation=job.credential_generation,
        style_codes=list(job.style_codes or []),
        size_labels=list(job.size_labels or []),
    )


def _mark_store_crashed(job_ref: _JobRef, store_id) -> None:
    """工作线程自身崩溃时兜底：把门店标成 FAILED，交给补跑。"""
    with SessionLocal() as crash_session:
        row = crash_session.execute(
            select(ScanJobStore).where(
                ScanJobStore.job_id == job_ref.id, ScanJobStore.store_id == store_id
            )
        ).scalar_one_or_none()
        if row is None or row.status in (
            StoreJobStatus.SUCCEEDED.value,
            StoreJobStatus.FAILED.value,
        ):
            return
        row.status = StoreJobStatus.FAILED.value
        row.error_code = ErrorCode.UNEXPECTED_STATUS.value
        row.message = "store probe crashed"
        row.finished_at = datetime.now(UTC)
        crash_session.commit()


def _run_stores_concurrently(
    settings: Settings,
    job: ScanJob,
    session: Session,
    credentials: CredentialService,
    rows: list[ScanJobStore],
    stores: dict,
    quota: QuotaGuard,
    binding: CredentialBinding,
    max_gap_seconds: int,
    worker_id: str | None,
    concurrency: int,
) -> None:
    """门店级并发执行：协调者负责心跳、统一中止与收尾。

    每个门店在自己的线程 / session / 上游客户端里跑；某个门店命中额度或凭证墙时
    立刻停止提交新门店，等已在跑的门店收尾后用一次 ``_abort_*`` 统一结束——已成功
    的检查点保持不动，未完成的门店统一标 FAILED 并交给补跑。
    """
    job_ref = _job_ref(job)
    pending: list = []
    for row in rows:
        store = stores.get(row.store_id)
        if store is None:
            row.status = StoreJobStatus.FAILED.value
            row.error_code = ErrorCode.STRUCTURE_MISSING.value
            row.message = "store not found"
            row.finished_at = datetime.now(UTC)
        elif row.status != StoreJobStatus.SUCCEEDED.value:
            pending.append(row.store_id)
    session.commit()

    stop = threading.Event()
    lease_lost = threading.Event()
    outcomes: list[_StoreOutcome] = []

    def heartbeat_loop() -> None:
        interval = max(5.0, settings.worker_lease_seconds / 3)
        while not stop.wait(interval):
            try:
                with SessionLocal() as hb_session:
                    fresh = hb_session.get(ScanJob, job_ref.id)
                    if fresh is None:
                        lease_lost.set()
                        stop.set()
                        return
                    heartbeat(hb_session, fresh, settings, worker_id)
                    hb_session.commit()
            except LeaseLost:
                lease_lost.set()
                stop.set()
                return
            except Exception:  # noqa: BLE001 - 心跳失败不能拖垮整轮
                logger.exception("job %s heartbeat failed", job_ref.id)

    def probe(store_id) -> _StoreOutcome:
        thread_session = SessionLocal()
        client = _make_client(settings, credentials, binding)
        try:
            row = thread_session.execute(
                select(ScanJobStore).where(
                    ScanJobStore.job_id == job_ref.id, ScanJobStore.store_id == store_id
                )
            ).scalar_one_or_none()
            store = thread_session.get(Store, store_id)
            if row is None or store is None or row.status == StoreJobStatus.SUCCEEDED.value:
                return _StoreOutcome("done")
            return _run_store(
                settings,
                job_ref,
                thread_session,
                client,
                quota,
                binding,
                max_gap_seconds,
                store,
                row,
                worker_id,
            )
        finally:
            client.close()
            thread_session.close()

    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="probe") as pool:
        heart = threading.Thread(target=heartbeat_loop, name="job-heartbeat", daemon=True)
        heart.start()
        try:
            futures: dict = {}
            for store_id in pending:
                if stop.is_set():
                    break
                futures[pool.submit(probe, store_id)] = store_id
            for future in as_completed(futures):
                store_id = futures[future]
                try:
                    outcome = future.result()
                except CancelledError:
                    continue
                except Exception:  # noqa: BLE001 - 单店崩溃不能拖垮整轮
                    logger.exception("job %s store %s probe crashed", job_ref.id, store_id)
                    _mark_store_crashed(job_ref, store_id)
                    outcomes.append(_StoreOutcome("failed"))
                    continue
                outcomes.append(outcome)
                if outcome.kind in ("limited", "auth", "lease_lost"):
                    # 已在跑的门店让它收尾，未开始的直接取消。
                    stop.set()
                    for other in futures:
                        other.cancel()
        finally:
            stop.set()
            heart.join(timeout=5.0)

    if lease_lost.is_set():
        session.rollback()
        logger.warning("job %s lease lost during parallel probing", job_ref.id)
        return
    auth_failure = next((item for item in outcomes if item.kind == "auth"), None)
    if auth_failure is not None:
        _abort_auth_failure(session, settings, job, rows, stores, auth_failure.code, worker_id)
        return
    limited = next((item for item in outcomes if item.kind == "limited"), None)
    if limited is not None:
        _abort_limited(session, settings, job, rows, stores, limited.code, quota, worker_id)
        return
    waf_failures = sum(
        1 for item in outcomes if item.code == ErrorCode.WAF_CHALLENGE.value
    )
    if settings.waf_storm_store_limit and waf_failures >= settings.waf_storm_store_limit:
        logger.warning(
            "job %s: %s store(s) exhausted the WAF challenge; aborting the round",
            job_ref.id,
            waf_failures,
        )
        _abort_limited(
            session,
            settings,
            job,
            rows,
            stores,
            ErrorCode.WAF_CHALLENGE.value,
            quota,
            worker_id,
            reason=throttle.THROTTLED_REASON,
        )
        return
    _finalize(session, settings, job, None, "", worker_id)


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
    # 并发路径下门店由各自 session 提交：先丢弃本地缓存，按数据库里的最新状态收尾。
    session.expire_all()
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


def _abort_limited(
    session: Session,
    settings: Settings,
    job: ScanJob,
    rows: list[ScanJobStore],
    stores: dict,
    code: str,
    quota: QuotaGuard,
    worker_id: str | None,
    *,
    reason: str | None = None,
) -> None:
    """Stop a job whose account hit its budget or upstream rate limiting.

    Successful checkpoints stay; every unfinished store is failed with the
    stable code and the job ends as FAILED. The account enters a cooldown so
    the scheduler pauses its tasks until the window reopens (and the failed
    stores are retried first, see ``_finalize``).
    """
    # 并发路径下门店由各自 session 提交：先丢弃本地缓存，按数据库里的最新状态收尾，
    # 否则会把已经成功的门店误标成 FAILED。
    session.expire_all()
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
    if job.owner_user_id is not None:
        if reason is None:
            reason = (
                throttle.THROTTLED_REASON
                if code == ErrorCode.THROTTLED.value
                else (quota.last_rejection or throttle.HOURLY_REASON)
            )
        resume_at = quota.resume_at() if reason in throttle.BUDGET_REASONS else None
        throttle.enter(
            session, settings, job.owner_user_id, reason, resume_at=resume_at
        )
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

    # 并发路径下门店检查点由各自 session 提交；本 session 里可能还留着开始时的
    # 旧状态（SQLAlchemy 默认不刷新 identity map），先丢弃缓存再统计。
    session.expire_all()
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

    cooldown = throttle.current(session, job.owner_user_id)
    if settings is not None and job.status == JobStatus.SUCCEEDED.value:
        # 整轮全部成功说明账号已恢复：重置风控阶梯；仍在生效的窗口不提前解除。
        throttle.clear(session, job.owner_user_id)

    task = None
    if job.task_id:
        task = session.execute(
            select(MonitorTask).where(MonitorTask.id == job.task_id)
        ).scalar_one_or_none()
        if task is not None:
            now = datetime.now(UTC)
            next_run_at = now + timedelta(seconds=task.cadence_seconds)
            if cooldown is not None and cooldown.until > next_run_at:
                # 冷却窗口内的任务不派发；到点由调度器自动继续。
                next_run_at = cooldown.until
            task.last_run_at = now
            task.next_run_at = next_run_at
            session.flush()

    # 检查结束后按任务的提醒开关聚合推送；提交由调用方负责。
    if settings is not None:
        push.enqueue_for_job(session, settings, job)
        _schedule_retry(session, settings, job, task, cooldown)


def _is_credential_code(code: str | None) -> bool:
    """凭证/授权类失败不能自动补跑：补跑前必须先让用户重新验证。"""
    text = code or ""
    return text.startswith("NIKE_") or text in CREDENTIAL_CODES


def _schedule_retry(
    session: Session,
    settings: Settings,
    job: ScanJob,
    task: MonitorTask | None,
    cooldown: throttle.Cooldown | None,
) -> ScanJob | None:
    """Queue one补跑 job for the stores this round did not deliver.

    A retry job (instead of requeueing the original one) keeps the failed
    round in the check history, avoids swallowing its push notifications, and
    naturally runs *before* the next full round once its ``not_before``
    (cooldown window or a short delay) passes. Bounded by
    ``job_max_attempts`` retry rounds per task.
    """
    if (
        task is None
        or not task.is_active
        or job.owner_user_id is None
        or job.credential_id is None
        or job.credential_generation is None
        or job.retry_round >= settings.job_max_attempts
    ):
        return None
    rows = session.execute(
        select(ScanJobStore).where(
            ScanJobStore.job_id == job.id,
            ScanJobStore.status.in_(
                [StoreJobStatus.FAILED.value, StoreJobStatus.UNKNOWN.value]
            ),
        )
    ).scalars().all()
    store_ids = [row.store_id for row in rows if not _is_credential_code(row.error_code)]
    if not store_ids:
        return None
    # 防重叠：同一任务已有作业排队/运行时不补跑（下一轮自然会覆盖）。
    active = session.execute(
        select(ScanJob.id)
        .where(
            ScanJob.task_id == job.task_id,
            ScanJob.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value]),
        )
        .limit(1)
    ).first()
    if active is not None:
        return None
    store_codes = list(
        session.execute(select(Store.store_code).where(Store.id.in_(store_ids))).scalars()
    )
    if not store_codes:
        return None
    try:
        retry_job = enqueue_job(
            session,
            tenant_id=job.tenant_id,
            store_codes=store_codes,
            style_codes=list(job.style_codes or []),
            size_labels=list(job.size_labels or []),
            task_id=job.task_id,
            kind="RETRY",
            owner_user_id=job.owner_user_id,
            credential_binding=CredentialBinding(
                owner_user_id=job.owner_user_id,
                credential_id=job.credential_id,
                generation=job.credential_generation,
            ),
        )
    except CredentialError:
        # 账号停用或绑定已变化：先让用户处理连接问题，这次不补跑。
        return None
    retry_job.retry_round = job.retry_round + 1
    not_before = datetime.now(UTC) + timedelta(seconds=settings.job_retry_delay_seconds)
    if cooldown is not None and cooldown.until > not_before:
        not_before = cooldown.until
    retry_job.not_before = not_before
    session.flush()
    logger.info(
        "job %s scheduled retry %s for %s store(s) at %s",
        job.id,
        retry_job.id,
        len(store_codes),
        not_before.isoformat(),
    )
    return retry_job


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
