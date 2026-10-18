from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_session, get_db
from app.models import (
    AuthSession,
    ChangeEvent,
    Credential,
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
from app.services.credentials import current_binding_condition, effective_status

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _today_window() -> datetime:
    now = datetime.now(UTC)
    return datetime(now.year, now.month, now.day, tzinfo=UTC)


def _credential_summary(db: Session, user_id) -> tuple[str, datetime | None]:
    """账号聚合视图：任一账号 ACTIVE/EXPIRING 即视为已连接。

    有效期取**最早**到期的可用账号（最保守，先提醒最先需要处理的连接）；
    一个可用账号都没有时，有需要重新验证的账号就报该状态，否则按未连接。
    """
    rows = db.execute(
        select(Credential).where(Credential.owner_user_id == user_id)
    ).scalars().all()
    reviewed = [(effective_status(record), record) for record in rows]
    usable = [
        (status, record)
        for status, record in reviewed
        if status
        in (CredentialStatus.ACTIVE.value, CredentialStatus.EXPIRING.value)
    ]
    if usable:
        status = (
            CredentialStatus.ACTIVE.value
            if any(item[0] == CredentialStatus.ACTIVE.value for item in usable)
            else CredentialStatus.EXPIRING.value
        )
        expiries = [
            record.access_expires_at
            for _status, record in usable
            if record.access_expires_at is not None
        ]
        return status, (min(expiries) if expiries else None)
    if any(
        status == CredentialStatus.NEEDS_AUTHORIZATION.value for status, _record in reviewed
    ):
        return CredentialStatus.NEEDS_AUTHORIZATION.value, None
    return CredentialStatus.UNASSIGNED.value, None


@router.get("", response_model=DashboardOut)
def get_dashboard(
    db: Session = Depends(get_db),
    payload: tuple[User, AuthSession] = Depends(get_current_session),
) -> DashboardOut:
    """Personal dashboard.

    Task, job, event and latest-state counters are scoped to the caller.
    ``UsageDaily`` stays the tenant-wide upstream budget (labelled as such in
    the UI); it is not a per-user quota.
    """
    user = payload[0]
    today = _today_window()
    tenant = db.execute(select(Tenant).where(Tenant.id == user.tenant_id)).scalar_one()

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

    latest_filter = [
        ObservationLatest.owner_user_id == user.id,
        # 一用户多账号：任一仍在的账号（generation 未变）写入的当前态都算。
        current_binding_condition(
            user.id,
            ObservationLatest.credential_id,
            ObservationLatest.credential_generation,
        ),
    ]
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
    credential_status, credential_expires_at = _credential_summary(db, user.id)

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
        credential_status=credential_status,
        credential_expires_at=credential_expires_at,
        review_requests_today=(
            int(usage_row.review_requests) + int(usage_row.price_requests) if usage_row else 0
        ),
        daily_review_budget=tenant.daily_review_budget,
    )
