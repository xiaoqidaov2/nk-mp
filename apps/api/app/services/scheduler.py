"""Turn active monitor tasks into queued scans, respecting cadence.

Dispatch is personal: the job's owner is the task creator. A task whose owner
is missing, disabled, or has no usable Nike binding simply skips this round
(the cadence keeps moving); the platform never falls back to a shared default
account, and the user's ``is_active`` intent is never changed here.
"""

from __future__ import annotations

import hashlib
import logging
import math
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import JobStatus, MonitorTask, ScanJob, Store, Tenant, User
from app.services import quiet_hours, throttle
from app.services.credentials import CredentialBinding, CredentialService, pick_ready
from app.services.jobs import enqueue_job
from app.services.stores import clearance_filter

logger = logging.getLogger("nike_mp.scheduler")


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


def _clock_key(task: MonitorTask) -> str:
    """任务的「节奏钟」键：只由账号 + 租户 + 间隔决定。

    合并分组还看门店/尺码集合，但**到期时刻**只看这三项——这样同一账号同一间隔的**所有**
    任务（含本轮因冷却/无绑定/已有作业被跳过的）都落在同一批网格时刻上，错位不会累积，
    队列排空后自然聚成一批、合成一个作业。
    """
    return f"{task.created_by}|{task.tenant_id}|{task.cadence_seconds}"


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


def _effective_cadence(settings: Settings, cadence_seconds: int) -> int:
    """夜间窗口内拉长后的实际间隔（窗口外等于原间隔）。"""
    return quiet_hours.interval(settings, cadence_seconds)


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

    多节点：本函数用事务级 advisory lock 做互斥，抢不到锁的 scheduler 跳过这一拍
    （主备自动接替，不需要 leader 状态机）。一用户多账号：每个 owner 本轮只选一次号
    （未冷却账号里最近最少使用优先），同 owner 的任务共用该账号；一个账号都不可用时
    暂停到最早一个冷却结束。
    """
    acquired = session.execute(
        select(func.pg_try_advisory_xact_lock(settings.dispatch_lock_key))
    ).scalar()
    if not acquired:
        logger.debug("dispatch tick skipped: another scheduler holds the lock")
        return 0
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

    def _next_for(task: MonitorTask) -> datetime:
        """该任务的下一次到期时刻（夜间窗口内自动拉长间隔）。"""
        return _next_run_at(
            _effective_cadence(settings, task.cadence_seconds),
            settings.dispatch_jitter_seconds,
            key=_clock_key(task),
        )

    #: 每个 owner 本轮只选一次号（同一拍内该 owner 的任务共用同一个账号）。
    picks: dict = {}
    #: owner 不可用时的恢复时刻缓存（没有冷却记录 = 下一轮网格）。
    resume_hints: dict = {}

    def pick_for(owner_user_id):
        if owner_user_id not in picks:
            picks[owner_user_id] = pick_ready(session, owner_user_id)
        return picks[owner_user_id]

    def resume_at(owner_user_id) -> datetime | None:
        if owner_user_id not in resume_hints:
            cooldown = throttle.owner_cooldowns(session, [owner_user_id]).get(owner_user_id)
            resume_hints[owner_user_id] = cooldown.until if cooldown is not None else None
        return resume_hints[owner_user_id]

    ready: list[tuple[MonitorTask, CredentialBinding, list[str]]] = []
    #: 已排队但**还没开跑**的单任务作业：本轮若产生合并作业，它们会被取消并由合并作业接管。
    #: 不这么做的话：客户节奏（分钟级）比一次 147 店扫描还快，每个任务永远"有作业在跑"，
    #: 只有恰好同时空出来的两三个任务才合得起来——合并永远拼不齐（生产实测：只能成对合并）。
    superseded: dict[str, ScanJob] = {}
    #: 本轮真正派出作业的 owner → 选中的账号（派发成功后推进 LRU，保证轮换前进）。
    used: dict = {}
    for task in tasks:
        active = _active_job_for(session, task)
        if active is not None:
            if active.status == JobStatus.QUEUED.value and active.task_id is not None:
                # 单任务且未开跑 → 允许被本轮合并接管（下面的 ready 照常收它）
                superseded[str(task.id)] = active
            else:
                # Keep the cadence moving without stacking duplicate scans.
                task.next_run_at = _next_for(task)
                continue
        if not task.style_codes or task.created_by is None:
            task.next_run_at = _next_for(task)
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
            task.next_run_at = _next_for(task)
            continue

        record = pick_for(task.created_by)
        if record is None:
            # 冷却是账号级的：全部账号都不可用时暂停到最早一个冷却结束；
            # 没有冷却记录（例如账号被断开）时按下一轮节奏再试。无兜底账号。
            until = resume_at(task.created_by)
            task.next_run_at = until if until is not None else _next_for(task)
            continue
        binding = CredentialBinding(
            owner_user_id=task.created_by,
            credential_id=record.id,
            generation=record.binding_generation,
        )

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
            task.next_run_at = _next_for(task)
            continue
        ready.append((task, binding, store_codes))

    # 按"账号 + 节奏 + 门店集合 + 尺码集合"分组：这几项一致才能合并（节奏不同会互相拖累，
    # 尺码集合是扫描期过滤条件，门店集合决定实际要扫哪些店）。款式取并集——一款一任务时
    # 正是靠这一点才能把同一批门店合并成一次采集。**账号必须进 key**：一个作业只冻结
    # 一个绑定，跨账号合并会让部分任务用错账号（绑定校验会整轮失败）。
    groups: dict[tuple, list[tuple[MonitorTask, CredentialBinding, list[str]]]] = {}
    for entry in ready:
        task, binding, store_codes = entry
        key = (
            str(task.created_by),
            str(task.tenant_id),
            int(task.cadence_seconds or 0),
            tuple(sorted(store_codes)),
            tuple(sorted(task.size_labels or [])),
            str(binding.credential_id),
        )
        groups.setdefault(key, []).append(entry)

    for _key, entries in groups.items():
        first_task, binding, store_codes = entries[0]
        if len(entries) == 1 and str(first_task.id) in superseded:
            # 只有一个任务、且它已有排队作业 → 保留原作业（重复派发等于把扫描频率翻倍）。
            first_task.next_run_at = _next_for(first_task)
            continue
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
        # 派发成功：推进该账号的 LRU（下一次选号轮到另一个账号）。
        used.setdefault(first_task.created_by, binding.credential_id)
        if merged:
            # 合并作业接管这些任务：把还没开跑的单任务作业停掉（没有数据损失，它们一行都还没扫）。
            # 条件更新兜住竞态：worker 若刚好租走（RUNNING），这里就不会命中，那次扫描照常跑完。
            for task, _binding, _stores in entries:
                pending = superseded.get(str(task.id))
                if pending is None:
                    continue
                session.execute(
                    update(ScanJob)
                    .where(
                        ScanJob.id == pending.id,
                        ScanJob.status == JobStatus.QUEUED.value,
                    )
                    .values(
                        status=JobStatus.CANCELLED.value,
                        error_summary="已并入合并采集作业",
                    )
                )
        next_at = _next_for(first_task)
        for task, _binding, _stores in entries:
            task.next_run_at = next_at
            dispatched += 1
    # 选号账目在派发之后独立提交（小事务）：即使本轮派发事务被回滚，轮换也不会原地打转。
    for owner_user_id, credential_id in used.items():
        CredentialService(settings, owner_user_id).mark_used(credential_id)
    session.flush()
    return dispatched
