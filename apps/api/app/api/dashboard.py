from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_session, get_db, settings_dep
from app.config import Settings
from app.models import (
    AuthSession,
    ChangeEvent,
    CredentialStatus,
    EventType,
    JobStatus,
    MonitorTask,
    ObservationLatest,
    ScanJob,
    Store,
    Tenant,
    UsageDaily,
    User,
)
from app.schemas import DashboardOut
from app.services.credentials import CredentialError, CredentialService

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _today_window() -> datetime:
    now = datetime.now(UTC)
    return datetime(now.year, now.month, now.day, tzinfo=UTC)


@router.get("", response_model=DashboardOut)
def get_dashboard(
    db: Session = Depends(get_db),
    payload: tuple[User, AuthSession] = Depends(get_current_session),
    settings: Settings = Depends(settings_dep),
) -> DashboardOut:
    """Personal dashboard.

    Task, job, event and latest-state counters are scoped to the caller.
    ``UsageDaily`` stays the tenant-wide upstream budget (labelled as such in
    the UI); it is not a per-user quota.
    """
    user = payload[0]
    today = _today_window()
    tenant = db.execute(select(Tenant).where(Tenant.id == user.tenant_id)).scalar_one()

    binding = None
    try:
        binding = CredentialService(settings, user.id).binding()
    except CredentialError:
        binding = None

    def _count_events(event_type: EventType) -> int:
        stmt = select(func.count()).select_from(ChangeEvent).where(
            ChangeEvent.event_type == event_type.value,
            ChangeEvent.detected_at >= today,
            ChangeEvent.owner_user_id == user.id,
            ChangeEvent.tenant_id == user.tenant_id,
        )
        return int(db.execute(stmt).scalar_one())

    task_filter = [
        MonitorTask.tenant_id == user.tenant_id,
        MonitorTask.created_by == user.id,
    ]
    active_tasks = int(
        db.execute(
            select(func.count()).select_from(MonitorTask).where(
                MonitorTask.is_active.is_(True), *task_filter
            )
        ).scalar_one()
    )
    monitored_codes: set[str] = set()
    for codes in db.execute(
        select(MonitorTask.style_codes).where(*task_filter)
    ).scalars():
        monitored_codes.update(codes or [])

    store_count = int(
        db.execute(
            select(func.count())
            .select_from(Store)
            .where(Store.owner_user_id == user.id, Store.is_active.is_(True))
        ).scalar_one()
    )

    latest_filter = [ObservationLatest.owner_user_id == user.id]
    if binding is not None:
        latest_filter.extend(
            [
                ObservationLatest.credential_id == binding.credential_id,
                ObservationLatest.credential_generation == binding.generation,
            ]
        )
    monitored_stores = 0
    last_checked = None
    if binding is not None:
        monitored_stores = int(
            db.execute(
                select(func.count(func.distinct(ObservationLatest.store_id))).where(*latest_filter)
            ).scalar_one()
        )
        last_checked = db.execute(
            select(func.max(ObservationLatest.checked_at)).where(*latest_filter)
        ).scalar_one()

    job_filter = [
        ScanJob.tenant_id == user.tenant_id,
        ScanJob.owner_user_id == user.id,
    ]
    running_jobs = int(
        db.execute(
            select(func.count()).select_from(ScanJob).where(
                ScanJob.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value]),
                *job_filter,
            )
        ).scalar_one()
    )
    failed_jobs = int(
        db.execute(
            select(func.count()).select_from(ScanJob).where(
                ScanJob.status == JobStatus.FAILED.value,
                ScanJob.created_at >= today,
                *job_filter,
            )
        ).scalar_one()
    )

    usage_row = db.execute(
        select(UsageDaily).where(UsageDaily.tenant_id == user.tenant_id, UsageDaily.day == today)
    ).scalar_one_or_none()
    credential = CredentialService(settings, user.id).status()

    return DashboardOut(
        active_tasks=active_tasks,
        monitored_styles=len(monitored_codes),
        monitored_stores=monitored_stores,
        store_coverage=store_count,
        restock_today=_count_events(EventType.RESTOCK),
        sold_out_today=_count_events(EventType.SOLD_OUT),
        price_change_today=_count_events(EventType.PRICE_CHANGE),
        new_arrival_today=_count_events(EventType.NEW_ARRIVAL_AT_STORE),
        failed_jobs=failed_jobs,
        running_jobs=running_jobs,
        last_checked_at=last_checked,
        credential_status=(
            credential.status if credential else CredentialStatus.UNASSIGNED.value
        ),
        credential_expires_at=credential.access_expires_at if credential else None,
        review_requests_today=(
            int(usage_row.review_requests) + int(usage_row.price_requests) if usage_row else 0
        ),
        daily_review_budget=tenant.daily_review_budget,
    )
