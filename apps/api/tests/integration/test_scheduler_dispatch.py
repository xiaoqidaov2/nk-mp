"""Scheduler dispatch: all-store tasks resolve at dispatch time.

Dispatch is personal: a task only produces a job when its creator still has a
usable Nike binding, so the scheduler never falls back to a shared account.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select, update

from app.config import get_settings
from app.db import SessionLocal
from app.models import Credential, CredentialStatus, JobStatus, MonitorTask, ScanJob, ScanJobStore, Store
from app.services.credentials import CredentialService, refresh_expiring
from app.services.scheduler import dispatch_due_tasks

from .helpers import make_store, make_style, make_tenant, make_user, store_credential


def _make_task(db, tenant_id, *, created_by, name="全国任务"):
    task = MonitorTask(
        tenant_id=tenant_id,
        name=name,
        store_codes=[],
        style_codes=["DN3707-202"],
        all_stores=True,
        is_active=True,
        next_run_at=datetime.now(UTC),
        created_by=created_by,
    )
    db.add(task)
    db.flush()
    return task


def test_all_store_task_expands_to_active_stores_at_dispatch() -> None:
    settings = get_settings()
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant, email="dispatch@example.com")
        make_store(db, owner=user, code="S1", region_id="R1")
        make_store(db, owner=user, code="S2", region_id="R2")
        make_style(db, owner=user, code="DN3707-202")
        task = _make_task(db, tenant.id, created_by=user.id)
        db.commit()
        task_id, user_id = task.id, user.id
    store_credential(settings, user_id)

    with SessionLocal() as db:
        dispatched = dispatch_due_tasks(db, settings)
        db.commit()
        assert dispatched == 1

    with SessionLocal() as db:
        job = db.execute(select(ScanJob).where(ScanJob.task_id == task_id)).scalar_one()
        assert sorted(job.store_codes) == ["S1", "S2"]
        assert job.owner_user_id == user_id
        # The scheduler attributes the job to the task creator, not a "requester".
        assert job.requested_by is None
        assert job.credential_id is not None
        assert job.credential_generation is not None
        rows = db.execute(select(ScanJobStore).where(ScanJobStore.job_id == job.id)).scalars().all()
        assert len(rows) == 2
        assert job.kind == "MONITOR"

        # A second tick must not stack another active scan for the same task.
        task = db.get(MonitorTask, task_id)
        task.next_run_at = datetime.now(UTC)
        task.cadence_seconds = 300
        db.commit()
    with SessionLocal() as db:
        assert dispatch_due_tasks(db, settings) == 0
        db.commit()

    with SessionLocal() as db:
        active = db.execute(
            select(ScanJob).where(
                ScanJob.task_id == task_id,
                ScanJob.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value]),
            )
        ).scalars().all()
        assert len(active) == 1
        assert db.execute(select(Store)).scalars().all() is not None


def test_task_without_a_binding_dispatches_nothing() -> None:
    """No fallback account: a creator without a Nike binding simply skips."""
    settings = get_settings()
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant, email="nobinding@example.com")
        make_store(db, owner=user, code="S1")
        make_style(db, owner=user, code="DN3707-202")
        task = _make_task(db, tenant.id, created_by=user.id)
        db.commit()
        task_id = task.id

    with SessionLocal() as db:
        assert dispatch_due_tasks(db, settings) == 0
        db.commit()

    with SessionLocal() as db:
        assert db.execute(select(ScanJob)).scalars().all() == []
        task = db.get(MonitorTask, task_id)
        # The user's intent is preserved; only the cadence moves forward.
        assert task.is_active is True
        assert task.next_run_at is not None


def test_stale_catalogue_is_refreshed_with_the_owners_own_binding() -> None:
    """目录缓存过期的用户会被登记刷新计划；新鲜的跳过；没有绑定的跳过。"""
    from datetime import timedelta

    from app.models import BootstrapState, OwnerBootstrap
    from app.services.bootstrap import enqueue_due

    settings = get_settings()
    with SessionLocal() as db:
        tenant = make_tenant(db)
        stale_id = make_user(db, tenant, email="stale@example.com").id
        fresh_id = make_user(db, tenant, email="fresh@example.com").id
        unbound_id = make_user(db, tenant, email="unbound@example.com").id
        db.commit()

    # 凭证服务使用独立会话，先提交用户再装绑定。
    store_credential(settings, stale_id)
    store_credential(settings, fresh_id)

    now = datetime.now(UTC)
    with SessionLocal() as db:
        db.add(
            OwnerBootstrap(
                owner_user_id=stale_id,
                state=BootstrapState.SUCCEEDED.value,
                progress=1.0,
                stage="门店数据已缓存（2 家）",
                store_count=2,
                finished_at=now - timedelta(days=2),
                updated_at=now - timedelta(days=2),
            )
        )
        db.add(
            OwnerBootstrap(
                owner_user_id=fresh_id,
                state=BootstrapState.SUCCEEDED.value,
                progress=1.0,
                stage="门店数据已缓存（2 家）",
                store_count=2,
                finished_at=now,
                updated_at=now,
            )
        )
        db.flush()

        assert enqueue_due(db, settings) == 1
        stale_plan = db.get(OwnerBootstrap, stale_id)
        assert stale_plan.state == BootstrapState.PENDING.value
        assert stale_plan.progress == 0.0
        # 新鲜的与没有绑定的都不受影响。
        assert db.get(OwnerBootstrap, fresh_id).state == BootstrapState.SUCCEEDED.value
        assert db.get(OwnerBootstrap, unbound_id) is None
        # 已经在排队的不会重复登记。
        assert enqueue_due(db, settings) == 0


def test_expiring_credential_is_renewed_without_user_action(monkeypatch) -> None:
    """调度器续期：到期/临近到期的连接自动换新，不再让用户重新验证。"""
    settings = get_settings()
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant, email="idle@example.com")
        db.commit()
        user_id = user.id
    store_credential(settings, user_id, access_token="old-access", refresh_token="old-refresh")

    with SessionLocal() as db:
        db.execute(
            update(Credential)
            .where(Credential.owner_user_id == user_id)
            .values(access_expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        db.commit()

    calls: dict[str, object] = {}

    class _TokenResponse:
        status_code = 200

        def json(self):  # noqa: ANN201 - duck-typed for the test
            return {"access_token": "new-access", "refresh_token": "new-refresh"}

    def fake_post(url, **kwargs):  # noqa: ANN001
        calls["url"] = url
        calls["data"] = kwargs.get("data")
        return _TokenResponse()

    monkeypatch.setattr(httpx, "post", fake_post)

    assert refresh_expiring(settings) == 1
    assert str(calls["url"]).endswith("/token/v1")
    data = calls["data"]
    assert isinstance(data, dict) and data.get("grant_type") == "refresh_token"

    service = CredentialService(settings, user_id)
    assert service.access_token() == "new-access"
    info = service.status()
    assert info is not None
    assert info.status == CredentialStatus.ACTIVE.value
    # 续期完成后同一个连接不会再次发请求。
    assert refresh_expiring(settings) == 0


def test_expired_credential_without_refresh_token_stays_for_the_user() -> None:
    """没有续期凭据的过期连接保持“需要重新验证”，调度器不会碰它。"""
    settings = get_settings()
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant, email="norefresh@example.com")
        db.commit()
        user_id = user.id
    store_credential(settings, user_id, access_token="old-access", refresh_token=None)

    with SessionLocal() as db:
        db.execute(
            update(Credential)
            .where(Credential.owner_user_id == user_id)
            .values(access_expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        db.commit()

    assert refresh_expiring(settings) == 0

    info = CredentialService(settings, user_id).status()
    assert info is not None
    assert info.status == CredentialStatus.NEEDS_AUTHORIZATION.value
