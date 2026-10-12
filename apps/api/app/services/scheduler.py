"""Turn active monitor tasks into queued scans, respecting cadence.

Dispatch is personal: the job's owner is the task creator. A task whose owner
is missing, disabled, or has no usable Nike binding simply skips this round
(the cadence keeps moving); the platform never falls back to a shared default
account, and the user's ``is_active`` intent is never changed here.
"""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import JobStatus, MonitorTask, ScanJob, Store, Tenant, User
from app.services import throttle
from app.services.credentials import CredentialError, CredentialService
from app.services.jobs import enqueue_job
from app.services.stores import clearance_filter


def due_tasks(
    session: Session, limit: int = 50, look_ahead_seconds: float = 0.0
) -> list[MonitorTask]:
    """到期的活跃任务；``look_ahead_seconds`` 顺带把"马上到期"的任务取出来。"""
    threshold = datetime.now(UTC) + timedelta(seconds=max(0.0, look_ahead_seconds))
    return list(
        session.execute(
            select(MonitorTask)
            .where(
                MonitorTask.is_active.is_(True),
                (MonitorTask.next_run_at.is_(None)) | (MonitorTask.next_run_at <= threshold),
            )
            .order_by(MonitorTask.next_run_at.nullsfirst())
            .limit(limit)
        ).scalars()
    )


def _jitter_span(cadence_seconds: int, jitter_seconds: float) -> float:
    """抖动幅度：≤ 间隔的 10%，且不超过配置上限。"""
    return min(max(0.0, jitter_seconds), max(0.0, float(cadence_seconds) * 0.1))


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _next_run_at(cadence_seconds: int, jitter_seconds: float, key: str | None = None) -> datetime:
    """下一次派发时刻：**按间隔网格对齐 + 组偏移**，落在 ``(now, now + 间隔]`` 内。

    为什么不是"现在 + 间隔 + 随机"：每个任务各自随机、各自到期，节奏一旦错开就再也对不上，
    同一批任务被摊到几十秒～几分钟里各派各的作业，**合并采集永远不成立**（抖动的本意是
    错峰，但错峰只应发生在**组与组之间**）。

    网格对齐后，同一 ``(间隔, 组偏移)`` 的任务不管在哪一刻被派发，下一个到期时刻都落在
    **同一个瞬间**——错位会在一轮内自动收敛，之后每轮都合并成一个作业；而且下一次不会
    早于此刻、也不会超过一个间隔（旧的"现在 + 间隔 + 抖动"会超过）。

    组偏移（≤ 间隔的 10%，且 ≤ 配置上限）由 ``key`` 稳定哈希得到：组间错峰、组内同刻。
    """
    interval = float(max(1, int(cadence_seconds)))
    span = _jitter_span(cadence_seconds, jitter_seconds)
    if key is None or span <= 0:
        offset = 0.0
    else:
        digest = hashlib.blake2s(key.encode("utf-8"), digest_size=8).digest()
        offset = (int.from_bytes(digest, "big") % 10_000) / 10_000.0 * span
    elapsed = (datetime.now(UTC) - _EPOCH).total_seconds()
    slot = math.floor((elapsed - offset) / interval) + 1
    return _EPOCH + timedelta(seconds=slot * interval + offset)


def _active_job_for(session: Session, task: MonitorTask) -> ScanJob | None:
    """该任务是否已有排队/运行中的作业（含"合并采集"作业：它把任务 id 记在 task_ids 里）。"""
    return session.execute(
        select(ScanJob)
        .where(
            or_(
                ScanJob.task_id == task.id,
                ScanJob.task_ids.contains([str(task.id)]),
            ),
            ScanJob.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value]),
        )
        .limit(1)
    ).scalars().first()


def dispatch_due_tasks(session: Session, settings: Settings) -> int:
    """派发到期任务；**同一账号、同一节奏、同一门店/尺码集合的多个任务合并成一个作业**。

    为什么合并：一次可用性请求能带 **50 个 GTIN（实测硬上限）且跨款式算数**。
    31 个"1 款 1 任务"各自派作业 → 每店 31 个请求；合并后每店 `ceil(Σ尺码/50) ≈ 12`
    （2.6× 减少）。事件与推送仍按任务归属（见 ``push.enqueue_for_job``）。

    抖动必须"组内一致、组间错开"：否则同一批任务被摊到多轮里，每个任务各成一个作业，
    合并永远不成立（生产上前 31 个任务就是这样一分钟一个）。所以这里先把"马上到期"
    的任务收拢进来，再让同组共用同一个 ``next_run_at``。
    """
    dispatched = 0
    now = datetime.now(UTC)
    window = max(0.0, float(settings.dispatch_merge_window_seconds))
    tasks = [
        task
        for task in due_tasks(session, look_ahead_seconds=window)
        if task.next_run_at is None
        or task.next_run_at <= now + timedelta(seconds=min(window, 0.5 * float(task.cadence_seconds)))
    ]
    # 网格对齐只对"下一次"生效；窗口负责把**历史遗留的错位**一次性收拢（最多提前半个间隔）。
    if not tasks:
        return 0
    cooldowns = throttle.active(session, [task.created_by for task in tasks])

    ready: list[tuple[MonitorTask, object, list[str]]] = []
    for task in tasks:
        cooldown = cooldowns.get(task.created_by)
        if cooldown is not None:
            task.next_run_at = cooldown.until
            continue
        if _active_job_for(session, task) is not None:
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
        ready.append((task, binding, store_codes))

    # 按"账号 + 节奏 + 门店集合 + 尺码集合"分组：这几项一致才能合并（节奏不同会互相拖累，
    # 尺码集合是扫描期过滤条件，门店集合决定实际要扫哪些店）。款式取并集——一款一任务时
    # 正是靠这一点才能把同一批门店合并成一次采集。
    groups: dict[tuple, list[tuple[MonitorTask, object, list[str]]]] = {}
    for entry in ready:
        task, _binding, store_codes = entry
        key = (
            str(task.created_by),
            str(task.tenant_id),
            int(task.cadence_seconds or 0),
            tuple(sorted(store_codes)),
            tuple(sorted(task.size_labels or [])),
        )
        groups.setdefault(key, []).append(entry)

    for key, entries in groups.items():
        first_task, binding, store_codes = entries[0]
        styles: list[str] = []
        for task, _binding, _stores in entries:
            for code in task.style_codes or []:
                if code not in styles:
                    styles.append(code)
        merged = len(entries) > 1
        enqueue_job(
            session,
            tenant_id=first_task.tenant_id,
            store_codes=store_codes,
            style_codes=styles,
            size_labels=list(first_task.size_labels or []),
            task_id=None if merged else first_task.id,
            kind="MONITOR",
            owner_user_id=first_task.created_by,
            credential_binding=binding,
            task_ids=[task.id for task, _binding, _stores in entries] if merged else None,
        )
        group_key = "|".join(str(part) for part in key)
        next_at = _next_run_at(
            first_task.cadence_seconds, settings.dispatch_jitter_seconds, key=group_key
        )
        for task, _binding, _stores in entries:
            task.next_run_at = next_at
            dispatched += 1
    session.flush()
    return dispatched
