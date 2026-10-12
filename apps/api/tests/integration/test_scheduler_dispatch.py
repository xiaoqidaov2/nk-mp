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
from app.models import (
    Credential,
    CredentialStatus,
    JobStatus,
    MonitorTask,
    NotificationChannel,
    NotificationDelivery,
    ScanJob,
    ScanJobStore,
    Store,
)
from app.security import encrypt_secret, secret_fingerprint
from app.services import push
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


def test_permanent_rejection_queues_a_reauth_notice(monkeypatch) -> None:
    """续期被永久拒绝：标记需要重新验证，并给用户入队一条系统通知（只发一次）。"""
    settings = get_settings()
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant, email="reauth@example.com")
        db.add(
            NotificationChannel(
                owner_user_id=user.id,
                platform="custom",
                name="测试渠道",
                target_enc=encrypt_secret("json://127.0.0.1:19100/__push"),
                target_fingerprint=secret_fingerprint(
                    "json://127.0.0.1:19100/__push", purpose="push"
                ),
                target_hint="••••push",
                enabled=True,
            )
        )
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

    class _Reject:
        status_code = 400

        def json(self):  # noqa: ANN201 - duck-typed for the test
            return {"error": "invalid_grant"}

    monkeypatch.setattr(httpx, "post", lambda url, **kwargs: _Reject())

    assert CredentialService(settings, user_id).try_refresh(force=True) is False

    with SessionLocal() as db:
        notice = db.execute(select(NotificationDelivery)).scalar_one()
        assert notice.job_id is None
        assert notice.task_id is None
        assert notice.title == push.REAUTH_REQUIRED_TITLE
        assert push.REAUTH_REQUIRED_BODY in notice.body
        assert notice.state == "PENDING"
        credential = db.execute(
            select(Credential).where(Credential.owner_user_id == user_id)
        ).scalar_one()
        assert credential.status == CredentialStatus.NEEDS_AUTHORIZATION.value

    # 状态守卫（以及冷却期）阻止重复通知。
    assert CredentialService(settings, user_id).try_refresh(force=True) is False
    with SessionLocal() as db:
        assert len(db.execute(select(NotificationDelivery)).scalars().all()) == 1


def test_cooling_account_is_not_dispatched_until_the_window_ends() -> None:
    """额度/风控冷却中的账号：不派发，只把 next_run_at 推到窗口结束。"""
    settings = get_settings()
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant, email="cooling@example.com")
        make_store(db, owner=user, code="S1", region_id="R1")
        make_style(db, owner=user, code="DN3707-202")
        task = _make_task(db, tenant.id, created_by=user.id)
        db.commit()
        task_id, user_id = task.id, user.id
    store_credential(settings, user_id)

    until = datetime.now(UTC) + timedelta(minutes=20)
    with SessionLocal() as db:
        db.execute(
            update(Credential)
            .where(Credential.owner_user_id == user_id)
            .values(cooldown_until=until, cooldown_reason="throttled", cooldown_level=1)
        )
        db.commit()

    with SessionLocal() as db:
        assert dispatch_due_tasks(db, settings) == 0
        db.commit()

    with SessionLocal() as db:
        assert db.execute(select(ScanJob)).scalars().all() == []
        task = db.get(MonitorTask, task_id)
        assert task.next_run_at is not None
        assert abs((task.next_run_at - until).total_seconds()) <= 1
        # 冷却到期后恢复派发（把任务重新置为到期，模拟窗口结束后的下一次 tick）。
        db.execute(
            update(Credential)
            .where(Credential.owner_user_id == user_id)
            .values(cooldown_until=datetime.now(UTC) - timedelta(seconds=1))
        )
        task.next_run_at = datetime.now(UTC)
        db.commit()

    with SessionLocal() as db:
        assert dispatch_due_tasks(db, settings) == 1
        db.commit()
    with SessionLocal() as db:
        job = db.execute(select(ScanJob).where(ScanJob.task_id == task_id)).scalar_one()
        assert job.status == JobStatus.QUEUED.value


def test_30_second_cadence_does_not_stack_rounds() -> None:
    """30 秒档：上一轮作业未结束时只推进 next_run_at，不再叠加一轮。"""
    settings = get_settings()
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant, email="fast-cadence@example.com")
        make_store(db, owner=user, code="S1", region_id="R1")
        make_style(db, owner=user, code="DN3707-202")
        task = _make_task(db, tenant.id, created_by=user.id)
        task.cadence_seconds = 30
        task.next_run_at = datetime.now(UTC)
        db.commit()
        task_id, user_id = task.id, user.id
    store_credential(settings, user_id)

    with SessionLocal() as db:
        assert dispatch_due_tasks(db, settings) == 1
        db.commit()

    # 手动把任务置为"又到期"：本轮作业仍在排队，不允许叠加。
    with SessionLocal() as db:
        task = db.get(MonitorTask, task_id)
        task.next_run_at = datetime.now(UTC)
        db.commit()
    with SessionLocal() as db:
        assert dispatch_due_tasks(db, settings) == 0
        db.commit()

    with SessionLocal() as db:
        jobs = db.execute(select(ScanJob).where(ScanJob.task_id == task_id)).scalars().all()
        assert len(jobs) == 1
        task = db.get(MonitorTask, task_id)
        assert task.next_run_at > datetime.now(UTC) + timedelta(seconds=20)


def test_a_store_added_later_is_monitored_from_the_next_round() -> None:
    """「全部门店」任务的名单在派发时解析：新同步进来的门店下一轮即被覆盖。"""
    settings = get_settings()
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant, email="newstore@example.com")
        make_store(db, owner=user, code="S1", region_id="R1")
        make_style(db, owner=user, code="DN3707-202")
        task = _make_task(db, tenant.id, created_by=user.id)
        db.commit()
        task_id, user_id = task.id, user.id
    store_credential(settings, user_id)

    with SessionLocal() as db:
        assert dispatch_due_tasks(db, settings) == 1
        db.commit()
    with SessionLocal() as db:
        first = db.execute(
            select(ScanJob).where(ScanJob.task_id == task_id).order_by(ScanJob.created_at)
        ).scalars().first()
        assert sorted(first.store_codes) == ["S1"]
        first.status = JobStatus.SUCCEEDED.value
        db.commit()

    # 用户后来同步了门店目录：新店是活跃状态。
    with SessionLocal() as db:
        make_store(db, owner=user, code="S9", region_id="R9")
        task = db.get(MonitorTask, task_id)
        task.next_run_at = datetime.now(UTC)
        db.commit()

    with SessionLocal() as db:
        assert dispatch_due_tasks(db, settings) == 1
        db.commit()
    with SessionLocal() as db:
        jobs = (
            db.execute(
                select(ScanJob).where(ScanJob.task_id == task_id).order_by(ScanJob.created_at)
            )
            .scalars()
            .all()
        )
        assert sorted(jobs[-1].store_codes) == ["S1", "S9"]


def test_a_task_with_an_explicit_store_list_ignores_new_stores() -> None:
    """指定门店的任务只在选中的门店里跑：新店不会被动加入。"""
    settings = get_settings()
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant, email="fixedlist@example.com")
        make_store(db, owner=user, code="S1", region_id="R1")
        make_style(db, owner=user, code="DN3707-202")
        task = _make_task(db, tenant.id, created_by=user.id, name="固定门店")
        task.all_stores = False
        task.store_codes = ["S1"]
        db.commit()
        task_id, user_id = task.id, user.id
    store_credential(settings, user_id)

    with SessionLocal() as db:
        make_store(db, owner=user, code="S9", region_id="R9")
        db.commit()
        assert dispatch_due_tasks(db, settings) == 1
        db.commit()
    with SessionLocal() as db:
        job = db.execute(select(ScanJob).where(ScanJob.task_id == task_id)).scalar_one()
        assert sorted(job.store_codes) == ["S1"]


def test_dispatch_jitter_spreads_the_next_round() -> None:
    """下一轮不精确落在「现在 + 间隔」：右移不超过 min(配置, 间隔的 10%)。"""
    settings = get_settings()
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant, email="jitter@example.com")
        make_store(db, owner=user, code="S1", region_id="R1")
        make_style(db, owner=user, code="DN3707-202")
        task = _make_task(db, tenant.id, created_by=user.id, name="抖动任务")
        task.cadence_seconds = 300
        db.commit()
        task_id, user_id = task.id, user.id
    store_credential(settings, user_id)

    with SessionLocal() as db:
        assert dispatch_due_tasks(db, settings) == 1
        db.commit()
    with SessionLocal() as db:
        task = db.get(MonitorTask, task_id)
        delay = (task.next_run_at - datetime.now(UTC)).total_seconds()
    ceiling = min(settings.dispatch_jitter_seconds, 300 * 0.1)
    assert 300 <= delay <= 300 + ceiling + 2


def test_thirty_second_cadence_keeps_its_jitter_small() -> None:
    """30 秒档的抖动上限是 3 秒（间隔的 10%）：几乎就是"连续轮次"。"""
    from app.services.scheduler import _next_run_at

    now = datetime.now(UTC)
    delay = (_next_run_at(30, 60.0) - now).total_seconds()
    assert 30 <= delay <= 33.0 + 0.5


def test_all_store_task_only_covers_clearance_stores() -> None:
    """客户要求：默认只盯换季优惠店——「全部门店」解析时也要按这个默认过滤。"""
    settings = get_settings()
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant, email="clearance@example.com")
        make_store(db, owner=user, code="S1", region_id="R1", name="NIKE杭州江南换季优惠店")
        make_store(db, owner=user, code="S2", region_id="R2", name="NIKE上海001")  # 非换季优惠
        make_style(db, owner=user, code="DN3707-202")
        task = _make_task(db, tenant.id, created_by=user.id, name="只看换季优惠")
        db.commit()
        task_id, user_id = task.id, user.id
    store_credential(settings, user_id)

    with SessionLocal() as db:
        assert dispatch_due_tasks(db, settings) == 1
        db.commit()
    with SessionLocal() as db:
        job = db.execute(select(ScanJob).where(ScanJob.task_id == task_id)).scalar_one()
        assert job.store_codes == ["S1"]
