from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import JobStatus, ScanJob
from app.schemas import HealthOut

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthOut)
def health(db: Session = Depends(get_db)) -> HealthOut:
    """Infrastructure-only health: database reachability and queue depth.

    Personal credential state is never reported here; it lives behind the
    authenticated ``/me/nike-credentials`` endpoint.
    """
    try:
        db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - health must never raise
        db.rollback()
        return HealthOut(status="down", database="down", queue_depth=0)
    queue_depth = int(
        db.execute(
            select(func.count()).select_from(ScanJob).where(
                ScanJob.status == JobStatus.QUEUED.value
            )
        ).scalar_one()
    )
    return HealthOut(status="ok", database="ok", queue_depth=queue_depth)
