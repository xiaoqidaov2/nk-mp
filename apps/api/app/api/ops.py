from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_ops, require_ops_write, settings_dep
from app.config import Settings
from app.models import JobStatus, MonitorTask, ScanJob, Store, Style, Tenant, User
from app.nike.client import NikeClient
from app.schemas import CredentialOut
from app.services.credentials import CredentialNotConfigured, CredentialService
from app.services.stores import sync_stores

router = APIRouter(prefix="/ops", tags=["ops"])


@router.get("/credentials", response_model=CredentialOut)
def credential_status(
    db: Session = Depends(get_db),
    user: User = Depends(require_ops),
    settings: Settings = Depends(settings_dep),
) -> CredentialOut:
    return CredentialOut(**CredentialService(settings, db).status())  # type: ignore[arg-type]


@router.post("/credentials/refresh")
def refresh_credential(
    db: Session = Depends(get_db),
    user: User = Depends(require_ops_write),
    settings: Settings = Depends(settings_dep),
) -> dict[str, object]:
    service = CredentialService(settings, db)
    ok = service.try_refresh()
    return {"ok": ok, **service.status()}


@router.post("/stores/sync")
def sync_store_catalog(
    db: Session = Depends(get_db),
    user: User = Depends(require_ops_write),
    settings: Settings = Depends(settings_dep),
) -> dict[str, object]:
    service = CredentialService(settings, db)
    try:
        token = service.access_token()
    except CredentialNotConfigured:
        return {"ok": False, "message": "凭证未导入，无法同步门店"}
    with NikeClient(settings, token, token_provider=service.token_provider()) as client:
        records = client.list_stores()
    touched = sync_stores(db, records, "ops_sync")
    return {"ok": True, "synced": touched, "total": len(records)}


@router.get("/tenants")
def list_tenants(
    db: Session = Depends(get_db),
    user: User = Depends(require_ops),
) -> list[dict[str, object]]:
    rows = db.execute(select(Tenant).order_by(Tenant.created_at)).scalars().all()
    out = []
    for tenant in rows:
        task_count = int(
            db.execute(
                select(func.count()).select_from(MonitorTask).where(MonitorTask.tenant_id == tenant.id)
            ).scalar_one()
        )
        out.append(
            {
                "id": str(tenant.id),
                "slug": tenant.slug,
                "name": tenant.name,
                "is_platform": tenant.is_platform,
                "is_active": tenant.is_active,
                "daily_review_budget": tenant.daily_review_budget,
                "task_count": task_count,
            }
        )
    return out


@router.get("/health/detail")
def health_detail(
    db: Session = Depends(get_db),
    user: User = Depends(require_ops),
    settings: Settings = Depends(settings_dep),
) -> dict[str, object]:
    queue_depth = int(
        db.execute(
            select(func.count()).select_from(ScanJob).where(
                ScanJob.status == JobStatus.QUEUED.value
            )
        ).scalar_one()
    )
    running = int(
        db.execute(
            select(func.count()).select_from(ScanJob).where(
                ScanJob.status == JobStatus.RUNNING.value
            )
        ).scalar_one()
    )
    stores = int(db.execute(select(func.count()).select_from(Store)).scalar_one())
    styles = int(db.execute(select(func.count()).select_from(Style)).scalar_one())
    return {
        "queue_depth": queue_depth,
        "running_jobs": running,
        "stores": stores,
        "styles": styles,
        "credential": CredentialService(settings, db).status(),
    }
