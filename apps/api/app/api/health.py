from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.api.deps import get_db, settings_dep
from app.config import Settings
from app.models import Credential, JobStatus, ScanJob
from app.schemas import HealthOut

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthOut)
def health(
    db: Session = Depends(get_db),
    settings: Settings = Depends(settings_dep),
) -> HealthOut:
    try:
        db.execute(text("SELECT 1"))
        database = "ok"
    except Exception:  # noqa: BLE001 - health must never raise
        database = "down"
    credential = db.execute(select(Credential).limit(1)).scalar_one_or_none()
    credential_status = "missing"
    if credential is not None:
        credential_status = "ok" if credential.access_token_enc else "missing"
    queue_depth = int(
        db.execute(
            select(func.count()).select_from(ScanJob).where(
                ScanJob.status == JobStatus.QUEUED.value
            )
        ).scalar_one()
    )
    overall = "ok" if database == "ok" and credential_status == "ok" else "degraded"
    return HealthOut(
        status=overall,
        database=database,
        credential=credential_status,
        queue_depth=queue_depth,
    )
