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

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import BootstrapState, Credential, CredentialStatus, OwnerBootstrap
from app.nike.client import NikeClient
from app.services.credentials import CredentialError, CredentialService
from app.services.stores import sync_stores

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3

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


def lease_next_plan(session: Session, settings: Settings) -> OwnerBootstrap | None:
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
    plan.stage = STAGE_CONNECTING
    plan.progress = 0.05
    plan.updated_at = now
    session.flush()
    return plan


def _checkpoint(session: Session, plan: OwnerBootstrap, *, progress: float, stage: str) -> None:
    plan.progress = progress
    plan.stage = stage
    plan.updated_at = datetime.now(UTC)
    session.commit()


def _settle_failure(session: Session, plan: OwnerBootstrap, error_code: str) -> None:
    now = datetime.now(UTC)
    plan.lease_until = None
    plan.error_code = error_code
    plan.updated_at = now
    if plan.attempts >= MAX_ATTEMPTS:
        plan.state = BootstrapState.FAILED.value
        plan.stage = "未能获取门店数据"
        plan.finished_at = now
    else:
        # 可重试：回到队列，进度条显示等待重试。
        plan.state = BootstrapState.PENDING.value
        plan.stage = "等待重试"
    session.commit()


def run_plan(session: Session, settings: Settings, plan: OwnerBootstrap) -> None:
    """执行预加载：用该用户自己的连接拉取门店清单并写入他的缓存。"""
    owner = plan.owner_user_id
    service = CredentialService(settings, owner)
    try:
        binding = service.binding()
        token = service.access_token(binding=binding)
        _checkpoint(session, plan, progress=0.25, stage=STAGE_FETCHING)
        with NikeClient(
            settings,
            token,
            token_provider=service.token_provider(binding),
            credential_guard=lambda: service.assert_binding(binding),
        ) as client:
            records = client.list_stores()
    except CredentialError as exc:
        _settle_failure(session, plan, exc.code)
        return
    except Exception:  # noqa: BLE001 - 网络/上游异常按可重试处理
        logger.warning("bootstrap fetch failed for %s", owner, exc_info=True)
        _settle_failure(session, plan, "NIKE_UPSTREAM_UNAVAILABLE")
        return

    _checkpoint(session, plan, progress=0.55, stage=f"{STAGE_WRITING}（{len(records)} 家门店）")
    touched = sync_stores(session, records, "bootstrap", owner)
    plan.store_count = touched
    plan.progress = 1.0
    plan.stage = f"{STAGE_DONE}（{touched} 家）"
    plan.state = BootstrapState.SUCCEEDED.value
    plan.error_code = None
    plan.lease_until = None
    plan.finished_at = datetime.now(UTC)
    plan.updated_at = datetime.now(UTC)
    session.commit()
    logger.info("bootstrap finished for %s: %s stores", owner, touched)
