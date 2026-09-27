from __future__ import annotations

import json
import time
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db, require_write
from app.db import SessionLocal
from app.models import JobStatus, MonitorTask, ScanJob, ScanJobStore, Store, User
from app.schemas import ScanCreateIn, ScanJobOut, ScanStoreOut
from app.services.jobs import enqueue_job, requeue_failed_stores

router = APIRouter(prefix="/scans", tags=["scans"])

TERMINAL = {
    JobStatus.SUCCEEDED.value,
    JobStatus.PARTIAL.value,
    JobStatus.FAILED.value,
    JobStatus.CANCELLED.value,
}


def _job_out(db: Session, job: ScanJob) -> ScanJobOut:
    progress = 0.0
    if job.total_stores:
        progress = round((job.done_stores + job.failed_stores + job.unknown_stores) / job.total_stores, 3)
    return ScanJobOut(
        id=job.id,
        kind=job.kind,
        status=job.status,
        store_codes=list(job.store_codes or []),
        style_codes=list(job.style_codes or []),
        total_stores=job.total_stores,
        done_stores=job.done_stores,
        failed_stores=job.failed_stores,
        unknown_stores=job.unknown_stores,
        attempt=job.attempt,
        error_summary=job.error_summary or "",
        started_at=job.started_at,
        finished_at=job.finished_at,
        created_at=job.created_at,
        progress=progress,
    )


def _load_job(db: Session, tenant_id, job_id: UUID) -> ScanJob:
    job = db.execute(
        select(ScanJob).where(ScanJob.id == job_id, ScanJob.tenant_id == tenant_id)
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "扫描任务不存在")
    return job


@router.post("", response_model=ScanJobOut, status_code=status.HTTP_201_CREATED)
def create_scan(
    payload: ScanCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> ScanJobOut:
    style_codes = payload.style_codes
    store_codes = payload.store_codes
    task_id = payload.task_id

    if task_id is not None:
        task = db.execute(
            select(MonitorTask).where(
                MonitorTask.id == task_id, MonitorTask.tenant_id == user.tenant_id
            )
        ).scalar_one_or_none()
        if task is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
        style_codes = style_codes or list(task.style_codes or [])
        store_codes = store_codes or list(task.store_codes or [])

    if payload.all_stores or not store_codes:
        store_codes = list(
            db.execute(
                select(Store.store_code).where(Store.is_active.is_(True)).order_by(Store.store_code)
            ).scalars()
        )
    if not style_codes:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "请至少提供一个货号")

    known = set(
        db.execute(
            select(Store.store_code).where(Store.store_code.in_(store_codes))
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
        task_id=task_id,
        requested_by=user.id,
        kind="SCAN",
    )
    return _job_out(db, job)


@router.get("", response_model=list[ScanJobOut])
def list_scans(
    limit: int = 20,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ScanJobOut]:
    rows = db.execute(
        select(ScanJob)
        .where(ScanJob.tenant_id == user.tenant_id)
        .order_by(ScanJob.created_at.desc())
        .limit(max(1, min(limit, 200)))
    ).scalars().all()
    return [_job_out(db, row) for row in rows]


@router.get("/{job_id}", response_model=ScanJobOut)
def get_scan(
    job_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ScanJobOut:
    return _job_out(db, _load_job(db, user.tenant_id, job_id))


@router.get("/{job_id}/stores", response_model=list[ScanStoreOut])
def scan_stores(
    job_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ScanStoreOut]:
    job = _load_job(db, user.tenant_id, job_id)
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
) -> ScanJobOut:
    job = _load_job(db, user.tenant_id, job_id)
    if job.status not in TERMINAL:
        raise HTTPException(status.HTTP_409_CONFLICT, "扫描仍在运行")
    requeued = requeue_failed_stores(db, job)
    if requeued == 0:
        raise HTTPException(status.HTTP_409_CONFLICT, "没有需要重试的门店")
    return _job_out(db, job)


@router.get("/{job_id}/events")
def scan_progress(
    job_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    job = _load_job(db, user.tenant_id, job_id)
    job_ref = job.id

    def _stream():
        # The request-scoped session is torn down with the dependency, so poll
        # from a dedicated session that lives exactly as long as the stream.
        stream_db = SessionLocal()
        try:
            last_payload = ""
            deadline = time.monotonic() + 1800
            while time.monotonic() < deadline:
                if request.is_disconnected():
                    break
                stream_db.expire_all()
                fresh = stream_db.execute(
                    select(ScanJob).where(ScanJob.id == job_ref)
                ).scalar_one_or_none()
                if fresh is None:
                    break
                payload = _job_out(stream_db, fresh).model_dump(mode="json")
                encoded = json.dumps(payload, ensure_ascii=False)
                if encoded != last_payload:
                    last_payload = encoded
                    yield f"event: progress\ndata: {encoded}\n\n"
                if fresh.status in TERMINAL:
                    yield "event: done\ndata: {}\n\n"
                    break
                time.sleep(2.0)
        finally:
            stream_db.close()

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
