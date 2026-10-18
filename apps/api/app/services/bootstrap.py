"""Binding-time preload ("bootstrap").

Rule: anything fetched through a user's Nike binding belongs to that user and is
only cached for them. The moment a binding succeeds we queue a preload that
fetches the owner's store catalogue with the owner's own token, keeps progress
visible, and retries transient failures a few times.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import BootstrapState, Credential, CredentialStatus, OwnerBootstrap
from app.nike.client import NikeClient
from app.services.credentials import CredentialError, CredentialService
from app.services.stores import sync_stores

logger = logging.getLogger("nike_mp.bootstrap")

MAX_ATTEMPTS = 3


class BootstrapLeaseLost(RuntimeError):
    """Raised when this worker no longer owns the plan's lease.

    Only the lease holder may renew the lease or write state; a takeover
    (multi-worker) means this worker must stop without touching the plan.
    """


STAGE_QUEUED = "排队中：准备获取门店清单"
STAGE_CONNECTING = "正在连接 Nike"
STAGE_FETCHING = "正在获取门店清单"
STAGE_WRITING = "正在写入缓存"
STAGE_DONE = "门店数据已缓存"


def ensure_plan(session: Session, owner_user_id: uuid.UUID) -> OwnerBootstrap:
    """绑定成功后登记（或重置）预加载计划；正在执行的计划不打断。"""
    now = datetime.now(UTC)
    plan = session.get(OwnerBootstrap, owner_user_id)
    if plan is None:
        plan = OwnerBootstrap(owner_user_id=owner_user_id)
        session.add(plan)
    elif plan.state == BootstrapState.RUNNING.value:
        return plan
    plan.state = BootstrapState.PENDING.value
    plan.progress = 0.0
    plan.stage = STAGE_QUEUED
    plan.error_code = None
    plan.attempts = 0
    plan.finished_at = None
    plan.lease_until = None
    plan.leased_by = None
    plan.updated_at = now
    session.flush()
    return plan


def enqueue_due(session: Session, settings: Settings) -> int:
    """给"绑定仍然有效、但目录缓存已过期"的用户登记刷新计划。

    只使用各用户自己的连接；运行中/排队中的计划不重复登记。
    """
    ttl = settings.catalog_ttl_seconds
    if ttl <= 0:
        return 0
    now = datetime.now(UTC)
    stale_before = now - timedelta(seconds=ttl)
    owners = session.execute(
        select(Credential.owner_user_id)
        .distinct()
        .outerjoin(OwnerBootstrap, OwnerBootstrap.owner_user_id == Credential.owner_user_id)
        .where(
            Credential.status == CredentialStatus.ACTIVE.value,
            (OwnerBootstrap.owner_user_id.is_(None))
            | (
                OwnerBootstrap.state.in_(
                    (BootstrapState.SUCCEEDED.value, BootstrapState.FAILED.value)
                )
                & ((OwnerBootstrap.finished_at.is_(None)) | (OwnerBootstrap.finished_at < stale_before))
            ),
        )
    ).scalars()
    enqueued = 0
    for owner_user_id in owners:
        plan = ensure_plan(session, owner_user_id)
        if plan.state == BootstrapState.PENDING.value:
            enqueued += 1
    return enqueued


def lease_next_plan(
    session: Session, settings: Settings, worker_id: str
) -> OwnerBootstrap | None:
    """领取待执行（或租约过期的）计划；没有可执行计划时返回 None。"""
    now = datetime.now(UTC)
    stmt = (
        select(OwnerBootstrap)
        .where(
            (OwnerBootstrap.state == BootstrapState.PENDING.value)
            | (
                (OwnerBootstrap.state == BootstrapState.RUNNING.value)
                & OwnerBootstrap.lease_until.is_not(None)
                & (OwnerBootstrap.lease_until < now)
            )
        )
        .order_by(OwnerBootstrap.updated_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    plan = session.execute(stmt).scalar_one_or_none()
    if plan is None:
        return None
    plan.state = BootstrapState.RUNNING.value
    plan.attempts += 1
    plan.started_at = plan.started_at or now
    plan.lease_until = now + timedelta(seconds=settings.bootstrap_lease_seconds)
    plan.leased_by = worker_id
    plan.stage = STAGE_CONNECTING
    plan.progress = 0.05
    plan.updated_at = now
    session.flush()
    return plan


def _checkpoint(
    session: Session,
    settings: Settings,
    plan: OwnerBootstrap,
    *,
    progress: float,
    stage: str,
    worker_id: str,
) -> None:
    """写进度并续租；租约已易主时抛 :class:`BootstrapLeaseLost`（不写状态）。"""
    now = datetime.now(UTC)
    result = session.execute(
        update(OwnerBootstrap)
        .where(
            OwnerBootstrap.owner_user_id == plan.owner_user_id,
            OwnerBootstrap.leased_by == worker_id,
        )
        .values(
            progress=progress,
            stage=stage,
            lease_until=now + timedelta(seconds=settings.bootstrap_lease_seconds),
            updated_at=now,
        )
    )
    if result.rowcount == 0:
        session.rollback()
        raise BootstrapLeaseLost(f"bootstrap plan {plan.owner_user_id} is owned by another worker")
    session.commit()


def _settle_failure(
    session: Session, plan: OwnerBootstrap, error_code: str, worker_id: str
) -> None:
    now = datetime.now(UTC)
    attempts = int(plan.attempts or 0)
    values: dict = {
        "lease_until": None,
        "leased_by": None,
        "error_code": error_code,
        "updated_at": now,
    }
    if attempts >= MAX_ATTEMPTS:
        values.update(
            state=BootstrapState.FAILED.value,
            stage="未能获取门店数据",
            finished_at=now,
        )
    else:
        # 可重试：回到队列，进度条显示等待重试。
        values.update(state=BootstrapState.PENDING.value, stage="等待重试")
    session.execute(
        update(OwnerBootstrap)
        .where(
            OwnerBootstrap.owner_user_id == plan.owner_user_id,
            OwnerBootstrap.leased_by == worker_id,
        )
        .values(**values)
    )
    session.commit()


def run_plan(
    session: Session, settings: Settings, plan: OwnerBootstrap, worker_id: str
) -> None:
    """执行预加载：用该用户自己的连接拉取门店清单并写入他的缓存。

    只有持有该计划租约的 worker 才能写状态；租约被别的 worker 接管后本函数
    安静退出，绝不覆盖新持有者的进度（多 worker 下的真实缺口）。
    """
    if plan.leased_by != worker_id:
        logger.warning(
            "bootstrap plan for %s is leased by %s, skipping", plan.owner_user_id, plan.leased_by
        )
        return
    owner = plan.owner_user_id
    service = CredentialService(settings, owner)
    try:
        binding = service.binding()
        token = service.access_token(binding=binding)
        _checkpoint(
            session, settings, plan, progress=0.25, stage=STAGE_FETCHING, worker_id=worker_id
        )
        with NikeClient(
            settings,
            token,
            token_provider=service.token_provider(binding),
            credential_guard=lambda: service.assert_binding(binding),
        ) as client:
            records = client.list_stores()
    except BootstrapLeaseLost:
        logger.warning("bootstrap plan lease lost for %s", owner)
        return
    except CredentialError as exc:
        _settle_failure(session, plan, exc.code, worker_id)
        return
    except Exception:  # noqa: BLE001 - 网络/上游异常按可重试处理
        logger.warning("bootstrap fetch failed for %s", owner, exc_info=True)
        _settle_failure(session, plan, "NIKE_UPSTREAM_UNAVAILABLE", worker_id)
        return

    try:
        _checkpoint(
            session,
            settings,
            plan,
            progress=0.55,
            stage=f"{STAGE_WRITING}（{len(records)} 家门店）",
            worker_id=worker_id,
        )
    except BootstrapLeaseLost:
        logger.warning("bootstrap plan lease lost for %s", owner)
        return

    touched = sync_stores(session, records, "bootstrap", owner)
    now = datetime.now(UTC)
    result = session.execute(
        update(OwnerBootstrap)
        .where(
            OwnerBootstrap.owner_user_id == owner,
            OwnerBootstrap.leased_by == worker_id,
        )
        .values(
            store_count=touched,
            progress=1.0,
            stage=f"{STAGE_DONE}（{touched} 家）",
            state=BootstrapState.SUCCEEDED.value,
            error_code=None,
            lease_until=None,
            leased_by=None,
            finished_at=now,
            updated_at=now,
        )
    )
    if result.rowcount == 0:
        session.rollback()
        logger.warning("bootstrap plan lease lost for %s before commit", owner)
        return
    session.commit()
    logger.info("bootstrap finished for %s: %s stores", owner, touched)
