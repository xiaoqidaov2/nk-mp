"""Turn active monitor tasks into queued scans, respecting cadence.

Dispatch is personal: the job's owner is the task creator. A task whose owner
is missing, disabled, or has no usable Nike binding simply skips this round
(the cadence keeps moving); the platform never falls back to a shared default
account, and the user's ``is_active`` intent is never changed here.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import JobStatus, MonitorTask, ScanJob, Store, Tenant, User
from app.services import throttle
from app.services.credentials import CredentialError, CredentialService
from app.services.jobs import enqueue_job
from app.services.stores import clearance_filter


def due_tasks(session: Session, limit: int = 50) -> list[MonitorTask]:
    now = datetime.now(UTC)
    return list(
        session.execute(
            select(MonitorTask)
            .where(
                MonitorTask.is_active.is_(True),
                (MonitorTask.next_run_at.is_(None)) | (MonitorTask.next_run_at <= now),
            )
            .order_by(MonitorTask.next_run_at.nullsfirst())
            .limit(limit)
        ).scalars()
    )


def _next_run_at(cadence_seconds: int, jitter_seconds: float) -> datetime:
    """下一次派发时刻 = 间隔 + 一点随机右移（≤ 间隔的 10%，且不超过配置上限）。

    多任务共用同一 cadence 时若精确对齐在同一秒，会在同一分钟里把小时额度一次打满，
    于是集体进入冷却——客户看到的正是"任务一多就限速"。抖动把它们铺开：整体通过量不变，
    峰值下降。
    """
    span = min(max(0.0, jitter_seconds), max(0.0, float(cadence_seconds) * 0.1))
    offset = random.uniform(0.0, span) if span > 0 else 0.0
    return datetime.now(UTC) + timedelta(seconds=float(cadence_seconds) + offset)


def dispatch_due_tasks(session: Session, settings: Settings) -> int:
    dispatched = 0
    tasks = due_tasks(session)
    if not tasks:
        return 0
    # 一次查清所有任务创建者的冷却窗口：冷却中的账号不派发，到点自动继续。
    cooldowns = throttle.active(session, [task.created_by for task in tasks])
    for task in tasks:
        cooldown = cooldowns.get(task.created_by)
        if cooldown is not None:
            task.next_run_at = cooldown.until
            continue
        active = session.execute(
            select(ScanJob).where(
                ScanJob.task_id == task.id,
                ScanJob.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value]),
            )
        ).scalars().first()
        if active is not None:
            # Keep the cadence moving without stacking duplicate scans.
            task.next_run_at = datetime.now(UTC) + timedelta(seconds=task.cadence_seconds)
            continue
        if not task.style_codes or task.created_by is None:
            task.next_run_at = datetime.now(UTC) + timedelta(seconds=task.cadence_seconds)
            continue

        user = session.execute(
            select(User).where(User.id == task.created_by)
        ).scalar_one_or_none()
        tenant = session.execute(
            select(Tenant).where(Tenant.id == task.tenant_id)
        ).scalar_one_or_none()
        if (
            user is None
            or not user.is_active
            or user.tenant_id != task.tenant_id
            or tenant is None
            or not tenant.is_active
        ):
            task.next_run_at = datetime.now(UTC) + timedelta(seconds=task.cadence_seconds)
            continue

        try:
            binding = CredentialService(settings, task.created_by).binding()
        except CredentialError:
            # No fallback credential: resume automatically once the user
            # re-connects, on the next cadence.
            task.next_run_at = datetime.now(UTC) + timedelta(seconds=task.cadence_seconds)
            continue

        store_codes = list(task.store_codes or [])
        if not store_codes:
            # "All stores" is resolved at dispatch time so newly synced
            # stores are covered without editing every task.
            # 默认只跑换季优惠店：这类门店才是客户要盯的货源。
            store_codes = list(
                session.execute(
                    select(Store.store_code)
                    .where(
                        Store.owner_user_id == task.created_by,
                        Store.is_active.is_(True),
                        clearance_filter(),
                    )
                    .order_by(Store.store_code)
                ).scalars()
            )
        if not store_codes:
            task.next_run_at = datetime.now(UTC) + timedelta(seconds=task.cadence_seconds)
            continue
        enqueue_job(
            session,
            tenant_id=task.tenant_id,
            store_codes=store_codes,
            style_codes=list(task.style_codes),
            size_labels=list(task.size_labels or []),
            task_id=task.id,
            kind="MONITOR",
            owner_user_id=task.created_by,
            credential_binding=binding,
        )
        task.next_run_at = _next_run_at(task.cadence_seconds, settings.dispatch_jitter_seconds)
        dispatched += 1
    session.flush()
    return dispatched
