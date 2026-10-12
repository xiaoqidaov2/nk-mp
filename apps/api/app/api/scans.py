from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_current_session, get_db, require_write, settings_dep
from app.api.http_errors import binding_changed_response, credential_error_response
from app.config import Settings
from app.db import SessionLocal
from app.models import (
    AuthSession,
    JobStatus,
    MonitorTask,
    ScanJob,
    ScanJobStore,
    Store,
    StoreJobStatus,
    User,
)
from app.schemas import ScanCreateIn, ScanJobOut, ScanStoreOut
from app.services.credentials import CredentialError, CredentialService
from app.services.jobs import enqueue_job, requeue_failed_stores

router = APIRouter(prefix="/scans", tags=["scans"])

STREAM_POLL_SECONDS = 2.0
STREAM_MAX_SECONDS = 1800.0

TERMINAL = {
    JobStatus.SUCCEEDED.value,
    JobStatus.PARTIAL.value,
    JobStatus.FAILED.value,
    JobStatus.CANCELLED.value,
}


def _live_store_counts(db: Session, job: ScanJob) -> tuple[int, int]:
    """跑着的作业按门店行实时统计进度。

    ``job.done_stores`` 只在收尾（``_finalize``）时写，所以进行中的作业在界面上一直显示
    ``0/N``，客户会以为卡住了。这里读侧兜底：QUEUED/RUNNING 时按 ``scan_job_store`` 的
    状态现算（已收尾的作业仍以 job 上的计数为准，保持语义一致）。
    """
    if job.status not in (JobStatus.QUEUED.value, JobStatus.RUNNING.value):
        return job.done_stores, job.unknown_stores
    rows = db.execute(
        select(ScanJobStore.status, func.count())
        .where(ScanJobStore.job_id == job.id)
        .group_by(ScanJobStore.status)
    ).all()
    counts = dict(rows)
    done = counts.get(StoreJobStatus.SUCCEEDED.value, 0) + counts.get(StoreJobStatus.FAILED.value, 0)
    unknown = counts.get(StoreJobStatus.UNKNOWN.value, 0)
    return done, unknown


def _job_out(db: Session, job: ScanJob) -> ScanJobOut:
    progress = 0.0
    # 门店数为 0 的作业（刚入队、或被跳过）没有实时统计可取，退回作业上的计数——
    # 否则整个 /scans 列表会因为一个空作业而 500。
    done_live, unknown_live = job.done_stores, job.unknown_stores
    if job.total_stores:
        done_live, unknown_live = _live_store_counts(db, job)
        progress = round((done_live + unknown_live) / job.total_stores, 3)
    return ScanJobOut(
        id=job.id,
        task_id=job.task_id,
        task_ids=[UUID(value) for value in (job.task_ids or [])],
        kind=job.kind,
        status=job.status,
        store_codes=list(job.store_codes or []),
        style_codes=list(job.style_codes or []),
        total_stores=job.total_stores,
        done_stores=done_live,
        failed_stores=job.failed_stores,
        unknown_stores=unknown_live,
        attempt=job.attempt,
        error_summary=job.error_summary or "",
        started_at=job.started_at,
        finished_at=job.finished_at,
        created_at=job.created_at,
        progress=progress,
    )


def _load_job(db: Session, user: User, job_id: UUID) -> ScanJob:
    """Load a job inside the caller's tenant *and* owner boundary."""
    job = db.execute(
        select(ScanJob).where(
            ScanJob.id == job_id,
            ScanJob.tenant_id == user.tenant_id,
            ScanJob.owner_user_id == user.id,
        )
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "扫描任务不存在")
    return job


@router.post("", response_model=ScanJobOut, status_code=status.HTTP_201_CREATED)
def create_scan(
    payload: ScanCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
    settings: Settings = Depends(settings_dep),
):
    try:
        binding = CredentialService(settings, user.id).binding()
    except CredentialError as exc:
        return credential_error_response(exc)

    style_codes = payload.style_codes
    store_codes = payload.store_codes
    task_id = payload.task_id
    #: 临时作业（无 task_id）默认全部尺码；从任务建作业时冻结任务的尺码选择。
    size_labels: list[str] = []

    if task_id is not None:
        task = db.execute(
            select(MonitorTask).where(
                MonitorTask.id == task_id,
                MonitorTask.tenant_id == user.tenant_id,
                MonitorTask.created_by == user.id,
            )
        ).scalar_one_or_none()
        if task is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
        style_codes = style_codes or list(task.style_codes or [])
        store_codes = store_codes or list(task.store_codes or [])
        size_labels = list(task.size_labels or [])

    if payload.all_stores or not store_codes:
        store_codes = list(
            db.execute(
                select(Store.store_code)
                .where(Store.owner_user_id == user.id, Store.is_active.is_(True))
                .order_by(Store.store_code)
            ).scalars()
        )
    if not style_codes:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "请至少提供一个货号")

    known = set(
        db.execute(
            select(Store.store_code).where(
                Store.owner_user_id == user.id, Store.store_code.in_(store_codes)
            )
        ).scalars()
    )
    resolved = [code for code in store_codes if code in known]
    if not resolved:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "门店清单为空，请先同步门店")

    job = enqueue_job(
        db,
        tenant_id=user.tenant_id,
        store_codes=resolved,
        style_codes=[code.upper() for code in style_codes],
        size_labels=size_labels,
        task_id=task_id,
        requested_by=user.id,
        kind="SCAN",
        owner_user_id=user.id,
        credential_binding=binding,
    )
    # Commit before the 201 so a worker (or the next request) sees the job.
    db.commit()
    return _job_out(db, job)


@router.get("", response_model=list[ScanJobOut])
def list_scans(
    limit: int = 20,
    task_id: UUID | None = None,
    db: Session = Depends(get_db),
    payload: tuple[User, AuthSession] = Depends(get_current_session),
) -> list[ScanJobOut]:
    user = payload[0]
    stmt = select(ScanJob).where(
        ScanJob.tenant_id == user.tenant_id,
        ScanJob.owner_user_id == user.id,
    )
    if task_id is not None:
        # 合并采集作业的 task_id 为空、真正的归属写在 task_ids 里：按任务过滤时要一并带出来，
        # 否则客户在任务详情页里看不到自己的扫描记录。
        stmt = stmt.where(
            or_(
                ScanJob.task_id == task_id,
                ScanJob.task_ids.contains([str(task_id)]),
            )
        )
    rows = db.execute(
        stmt.order_by(ScanJob.created_at.desc()).limit(max(1, min(limit, 200)))
    ).scalars().all()
    return [_job_out(db, row) for row in rows]


@router.get("/{job_id}", response_model=ScanJobOut)
def get_scan(
    job_id: UUID,
    db: Session = Depends(get_db),
    payload: tuple[User, AuthSession] = Depends(get_current_session),
) -> ScanJobOut:
    return _job_out(db, _load_job(db, payload[0], job_id))


@router.get("/{job_id}/stores", response_model=list[ScanStoreOut])
def scan_stores(
    job_id: UUID,
    db: Session = Depends(get_db),
    payload: tuple[User, AuthSession] = Depends(get_current_session),
) -> list[ScanStoreOut]:
    job = _load_job(db, payload[0], job_id)
    rows = db.execute(
        select(ScanJobStore, Store)
        .join(Store, Store.id == ScanJobStore.store_id)
        .where(ScanJobStore.job_id == job.id)
        .order_by(Store.store_code)
    ).all()
    return [
        ScanStoreOut(
            store_code=store.store_code,
            store_name=store.display_name,
            status=row.status,
            attempt=row.attempt,
            error_code=row.error_code,
            message=row.message or "",
            observations_written=row.observations_written,
            events_written=row.events_written,
            started_at=row.started_at,
            finished_at=row.finished_at,
        )
        for row, store in rows
    ]


@router.post("/{job_id}/retry-failed", response_model=ScanJobOut)
def retry_failed(
    job_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
    settings: Settings = Depends(settings_dep),
):
    job = _load_job(db, user, job_id)
    if job.status not in TERMINAL:
        raise HTTPException(status.HTTP_409_CONFLICT, "扫描仍在运行")
    try:
        binding = CredentialService(settings, user.id).binding()
    except CredentialError as exc:
        return credential_error_response(exc)
    if (
        job.credential_id != binding.credential_id
        or job.credential_generation != binding.generation
        or job.owner_user_id != binding.owner_user_id
    ):
        # A retry must never continue a scan with a different Nike account.
        return binding_changed_response()
    requeued = requeue_failed_stores(db, job)
    if requeued == 0:
        raise HTTPException(status.HTTP_409_CONFLICT, "没有需要重试的门店")
    # Commit before returning so the requeued state is immediately visible.
    db.commit()
    return _job_out(db, job)


def _job_snapshot(
    job_ref: UUID, user_id: UUID, tenant_id: UUID, auth_session_id: UUID
) -> dict | None:
    """Read one job snapshot from a dedicated short-lived session.

    Every frame re-checks that the platform session is still valid and that
    the job still belongs to the same user and tenant, so a logout or an
    account switch stops the stream instead of leaking progress.
    """
    stream_db = SessionLocal()
    try:
        auth = stream_db.execute(
            select(AuthSession).where(AuthSession.id == auth_session_id)
        ).scalar_one_or_none()
        if auth is None or auth.revoked_at is not None or auth.expires_at <= datetime.now(UTC):
            return None
        fresh = stream_db.execute(
            select(ScanJob).where(
                ScanJob.id == job_ref,
                ScanJob.tenant_id == tenant_id,
                ScanJob.owner_user_id == user_id,
            )
        ).scalar_one_or_none()
        if fresh is None:
            return None
        return _job_out(stream_db, fresh).model_dump(mode="json")
    finally:
        stream_db.close()


async def _progress_stream(
    request: Request,
    job_ref: UUID,
    snapshot: Callable[[UUID], dict | None],
) -> AsyncIterator[str]:
    """Yield SSE progress frames until the job finishes or the client leaves."""
    fetch = snapshot
    last_payload = ""
    deadline = time.monotonic() + STREAM_MAX_SECONDS
    while time.monotonic() < deadline:
        if await request.is_disconnected():
            break
        payload = await run_in_threadpool(fetch, job_ref)
        if payload is None:
            break
        encoded = json.dumps(payload, ensure_ascii=False)
        if encoded != last_payload:
            last_payload = encoded
            yield f"event: progress\ndata: {encoded}\n\n"
        if payload.get("status") in TERMINAL:
            yield "event: done\ndata: {}\n\n"
            break
        await asyncio.sleep(STREAM_POLL_SECONDS)


@router.get("/{job_id}/events")
def scan_progress(
    job_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    payload: tuple[User, AuthSession] = Depends(get_current_session),
) -> StreamingResponse:
    user, auth_session = payload
    job = _load_job(db, user, job_id)
    job_ref = job.id

    def snapshot(ref: UUID) -> dict | None:
        return _job_snapshot(ref, user.id, user.tenant_id, auth_session.id)

    return StreamingResponse(
        _progress_stream(request, job_ref, snapshot),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
