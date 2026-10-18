"""Scheduler/worker health monitoring.

Every service instance writes one heartbeat row per tick (``service_heartbeat``,
upsert by ``(service, instance_id)``); the report is derived in real time — no
scheduling state is stored, so a stale heartbeat can never masquerade as a
healthy scheduler.

The independent ``app.monitor_main`` process keeps this running (warn-only) and
``/ops/scheduler-health`` exposes the same report to operators.
"""

from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    Credential,
    JobStatus,
    MonitorTask,
    NotificationDelivery,
    Role,
    ScanJob,
    ServiceHeartbeat,
    User,
)
from app.services import push
from app.services.credentials import pick_ready
from app.services.scheduler import _active_job_for

logger = logging.getLogger("nike_mp.monitor")

#: 报告里最多列出的迟到任务数（运维用；不追求全量）。
OVERDUE_TASK_LIMIT = 50

#: 根分区使用率告警线（%）：磁盘写满会让 Postgres 无法写 postmaster.pid 而崩溃循环
#: （2026-10-15 生产事故：40G 盘满 → 全站登录 500）。
DISK_WARN_PERCENT = 85

#: 磁盘水位告警的通知标题（同类 6 小时内最多一条）。
DISK_NOTICE_TITLE = "服务器磁盘空间不足"


def _disk_usage() -> dict:
    """根分区水位：提前看见"盘要满了"，别等数据库崩了才知道。"""
    total, used, free = shutil.disk_usage("/")
    percent = round(100.0 * used / total, 1) if total else 0.0
    return {
        "total_gb": round(total / 1024**3, 1),
        "used_gb": round(used / 1024**3, 1),
        "free_gb": round(free / 1024**3, 1),
        "used_percent": percent,
        "warning": percent >= DISK_WARN_PERCENT,
    }


def _instance_entry(row: ServiceHeartbeat, now: datetime, stale_seconds: int) -> dict:
    age = max(0.0, (now - row.last_seen_at).total_seconds())
    return {
        "instance_id": row.instance_id,
        "last_seen_at": row.last_seen_at.isoformat(),
        "age_seconds": round(age, 1),
        "alive": age <= stale_seconds,
    }


def notify_disk_warning(session: Session, settings: Settings) -> int:
    """磁盘水位告警：给管理后台管理员入队系统通知（同类 6 小时内最多一条）。

    磁盘写满会让 Postgres 无法写 ``postmaster.pid`` 而崩溃循环（2026-10-15 生产事故：
    40G 盘满 → 全站登录 500）。必须在崩之前让人看见；没有配置推送渠道时返回 0。
    """
    disk = _disk_usage()
    admins = session.execute(
        select(User.id).where(
            User.role == Role.OPS_ADMIN.value, User.is_active.is_(True)
        )
    ).scalars().all()
    cutoff = datetime.now(UTC) - timedelta(hours=6)
    queued = 0
    for owner_user_id in admins:
        recent = session.execute(
            select(func.count())
            .select_from(NotificationDelivery)
            .where(
                NotificationDelivery.owner_user_id == owner_user_id,
                NotificationDelivery.title == DISK_NOTICE_TITLE,
                NotificationDelivery.created_at >= cutoff,
            )
        ).scalar_one()
        if recent:
            continue
        queued += push.enqueue_system_notification(
            session,
            settings,
            owner_user_id=owner_user_id,
            title=DISK_NOTICE_TITLE,
            body=(
                f"服务器根分区已用 {disk['used_percent']:.0f}%（剩余 {disk['free_gb']:.1f}G）。"
                "请清理旧镜像/日志，磁盘写满会导致数据库停止服务。"
            ),
        )
    session.commit()
    return queued


def prune_stale_heartbeats(session: Session, settings: Settings) -> int:
    """删除远超保留窗口的心跳行：实例 ID 每次启动都不同，表会按实例累积。"""
    retention = max(60, settings.monitor_heartbeat_retention_seconds)
    cutoff = datetime.now(UTC) - timedelta(seconds=retention)
    result = session.execute(
        delete(ServiceHeartbeat).where(ServiceHeartbeat.last_seen_at < cutoff)
    )
    session.commit()
    return int(result.rowcount or 0)


def build_report(session: Session, settings: Settings) -> dict:
    """实时健康报告：实例心跳、迟到任务、冷却账号、队列深度。

    只列**最近 ``monitor_instance_window_seconds`` 内出现过**的实例：掉线的实例
    在这个窗口里仍然可见（``alive=False``，供告警/排查），窗口之后自动退场——
    否则每次发版换代都会在面板上留下永久"调度心跳超时"的幽灵实例。
    """
    now = datetime.now(UTC)
    report: dict = {
        "scheduler": [],
        "workers": [],
        "overdue_tasks": [],
        "cooling_accounts": [],
        "queue": {},
        "disk": _disk_usage(),
    }

    window_start = now - timedelta(seconds=max(0, settings.monitor_instance_window_seconds))
    rows = session.execute(
        select(ServiceHeartbeat)
        .where(ServiceHeartbeat.last_seen_at >= window_start)
        .order_by(ServiceHeartbeat.service, ServiceHeartbeat.instance_id)
    ).scalars().all()
    running_by_instance = dict(
        session.execute(
            select(ScanJob.leased_by, func.count())
            .where(
                ScanJob.status == JobStatus.RUNNING.value,
                ScanJob.leased_by.is_not(None),
            )
            .group_by(ScanJob.leased_by)
        ).all()
    )
    for row in rows:
        entry = _instance_entry(row, now, settings.monitor_stale_seconds)
        if row.service == "scheduler":
            report["scheduler"].append(entry)
        elif row.service == "worker":
            report["workers"].append(
                {**entry, "running_jobs": int(running_by_instance.get(row.instance_id, 0))}
            )

    grace = timedelta(seconds=max(0, settings.monitor_overdue_grace_seconds))
    candidates = session.execute(
        select(MonitorTask)
        .where(
            MonitorTask.is_active.is_(True),
            MonitorTask.next_run_at.is_not(None),
            MonitorTask.next_run_at < now - grace,
        )
        .order_by(MonitorTask.next_run_at)
        .limit(OVERDUE_TASK_LIMIT)
    ).scalars().all()
    picks: dict = {}
    for task in candidates:
        if task.created_by is None:
            continue
        if _active_job_for(session, task) is not None:
            # 已有排队/运行中的作业：只是还没轮到，不算迟到。
            continue
        if task.created_by not in picks:
            picks[task.created_by] = pick_ready(session, task.created_by)
        if picks[task.created_by] is None:
            # 账号全部不可用（冷却/断开）：这是预期暂停，等冷却结束，见 cooling_accounts。
            continue
        last_finished = session.execute(
            select(func.max(ScanJob.finished_at)).where(
                or_(
                    ScanJob.task_id == task.id,
                    ScanJob.task_ids.contains([str(task.id)]),
                )
            )
        ).scalar_one_or_none()
        report["overdue_tasks"].append(
            {
                "task_id": str(task.id),
                "name": task.name,
                "next_run_at": task.next_run_at.isoformat(),
                "overdue_seconds": round((now - task.next_run_at).total_seconds(), 1),
                "last_finished_at": last_finished.isoformat() if last_finished else None,
            }
        )

    cooling = session.execute(
        select(
            Credential.owner_user_id,
            Credential.id,
            Credential.cooldown_reason,
            Credential.cooldown_until,
        )
        .where(Credential.cooldown_until.is_not(None), Credential.cooldown_until > now)
        .order_by(Credential.cooldown_until)
        .limit(200)
    ).all()
    report["cooling_accounts"] = [
        {
            "owner_user_id": str(row.owner_user_id),
            "credential_id": str(row.id),
            "reason": row.cooldown_reason,
            "until": row.cooldown_until.isoformat(),
        }
        for row in cooling
    ]

    counts = dict(session.execute(select(ScanJob.status, func.count()).group_by(ScanJob.status)).all())
    report["queue"] = {status.value: int(counts.get(status.value, 0)) for status in JobStatus}
    return report
