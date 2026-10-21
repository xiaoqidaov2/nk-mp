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
from app.services.scheduler import _effective_cadence, dispatch_due_tasks

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


def test_night_window_stretches_the_next_run(monkeypatch) -> None:
    """夜间降速：派发后的下一轮按 1/factor 拉长（30 秒档 → 2 分钟）。"""
    settings = get_settings()
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant, email="night@example.com")
        make_store(db, owner=user, code="S1", region_id="R1")
        make_style(db, owner=user, code="DN3707-202")
        task = _make_task(db, tenant.id, created_by=user.id)
        task.cadence_seconds = 30
        db.commit()
        task_id, user_id = task.id, user.id
    store_credential(settings, user_id)

    monkeypatch.setattr("app.services.quiet_hours.factor", lambda *_args, **_kw: 0.25)
    assert _effective_cadence(settings, 30) == 120
    started = datetime.now(UTC)
    with SessionLocal() as db:
        assert dispatch_due_tasks(db, settings) == 1
        db.commit()

    with SessionLocal() as db:
        task = db.get(MonitorTask, task_id)
        assert task.next_run_at is not None
        # 网格对齐：下一轮落在 (now, now + 120s] 内，不会更早也不会更晚。
        assert started < task.next_run_at <= started + timedelta(seconds=121)


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
        # 网格对齐：下一次落在 (now, now + 间隔 + 组偏移] 内（旧的"现在 + 间隔"会超过间隔）。
        delay = (task.next_run_at - datetime.now(UTC)).total_seconds()
        assert 0 < delay <= 30 + min(settings.dispatch_jitter_seconds, 3.0) + 0.5


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
    """下一轮落在「间隔网格 + 组偏移」上：不超过一个间隔 + min(配置, 间隔的 10%)。"""
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
    assert 0 < delay <= 300 + ceiling + 2


def test_thirty_second_cadence_keeps_its_jitter_small() -> None:
    """30 秒档的组偏移上限是 3 秒（间隔的 10%），且下一个时刻不出 30 秒窗口。"""
    from app.services.scheduler import _next_run_at

    now = datetime.now(UTC)
    delay = (_next_run_at(30, 60.0) - now).total_seconds()
    assert 0 < delay <= 30.0 + 0.5


def test_the_next_round_is_grid_aligned_per_clock() -> None:
    """同一"节奏钟"（账号+间隔）不管何时算，下一个到期时刻都相同——同批任务自动收敛。

    这正是生产上"31 个任务一分钟一个作业"的解法：各自随机的抖动会让错位永久保留。
    被跳过的任务（冷却/无绑定/已有作业）也走同一套钟，错位不会累积。
    """
    from app.services.scheduler import _clock_key, _next_run_at

    class _Task:
        created_by = "user-1"
        tenant_id = "tenant-1"
        cadence_seconds = 120
        # 门店/尺码集合不同的任务，到期时刻也要对齐（它们只是分组不同，节奏钟相同）

    first = _next_run_at(120, 10.0, key=_clock_key(_Task()))
    second = _next_run_at(120, 10.0, key=_clock_key(_Task()))
    assert first == second
    delay = (first - datetime.now(UTC)).total_seconds()
    assert 0 < delay <= 120 + 10


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


def test_due_tasks_of_one_owner_merge_into_a_single_job() -> None:
    """合并采集：同账号 + 同门店/尺码集合的到期任务派**一个**作业（一次请求可带 50 个跨款 GTIN）。

    门店集合不同的任务不合并（否则会多扫门店）。
    """
    settings = get_settings()
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant, email="merge@example.com")
        make_store(db, owner=user, code="S1", region_id="R1")
        make_store(db, owner=user, code="S2", region_id="R2")
        for index, style in enumerate(("DN3707-202", "HJ2461-010", "FZ5029-010")):
            make_style(db, owner=user, code=style)
            task = _make_task(db, tenant.id, created_by=user.id, name=f"合并任务 {index}")
            task.style_codes = [style]
            task.store_codes = []
            task.all_stores = True
            if index == 2:
                # 第三个只盯一家店 → 门店集合不同，不能并入
                task.all_stores = False
                task.store_codes = ["S1"]
        db.commit()
        user_id = user.id
    store_credential(settings, user_id)

    with SessionLocal() as db:
        assert dispatch_due_tasks(db, settings) == 3  # 计数的是"任务数"
        db.commit()

    with SessionLocal() as db:
        jobs = (
            db.execute(select(ScanJob).where(ScanJob.kind == "MONITOR")).scalars().all()
        )
        assert len(jobs) == 2, [(job.id, job.task_id, job.task_ids) for job in jobs]
        merged = next(job for job in jobs if job.task_ids)
        assert len(merged.task_ids) == 2
        assert sorted(merged.style_codes) == ["DN3707-202", "HJ2461-010"]
        assert sorted(merged.store_codes) == ["S1", "S2"]
        assert merged.task_id is None
        single = next(job for job in jobs if not job.task_ids)
        assert single.task_id is not None
        assert single.store_codes == ["S1"]
        # 三个任务的 next_run_at 都被推进，不会重复派发
        tasks = db.execute(select(MonitorTask)).scalars().all()
        assert all(task.next_run_at > datetime.now(UTC) for task in tasks)


def test_pending_single_jobs_are_superseded_by_the_merge() -> None:
    """已排队但还没开跑的单任务作业会被合并作业接管。

    生产实测：客户节奏（分钟级）比一次 147 店扫描还快，每个任务永远"有作业在跑"，
    只有恰好同时空出来的两三个任务才合得起来——只能成对合并。这里验证第二轮会把
    三个排队作业停掉，换成一个三任务合并作业。
    """
    settings = get_settings()
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant, email="supersede@example.com")
        make_store(db, owner=user, code="S1", region_id="R1")
        task_ids = []
        for index in range(3):
            make_style(db, owner=user, code=f"SUP-{index}")
            task = _make_task(db, tenant.id, created_by=user.id, name=f"接管任务 {index}")
            task.style_codes = [f"SUP-{index}"]
            task.cadence_seconds = 120
            task.next_run_at = datetime.now(UTC)
            task_ids.append(task.id)
        # 直接造三个"已排队但还没开跑"的单任务作业（生产上就是它们把队列塞满的）
        for task_id in task_ids:
            db.add(
                ScanJob(
                    tenant_id=tenant.id,
                    owner_user_id=user.id,
                    kind="MONITOR",
                    status=JobStatus.QUEUED.value,
                    store_codes=["S1"],
                    style_codes=[],
                    total_stores=1,
                    task_id=task_id,
                    task_ids=[],
                )
            )
        db.commit()
        user_id = user.id
    store_credential(settings, user_id)

    with SessionLocal() as db:
        assert dispatch_due_tasks(db, settings) == 3
        db.commit()
    with SessionLocal() as db:
        jobs = (
            db.execute(select(ScanJob).where(ScanJob.kind == "MONITOR").order_by(ScanJob.created_at))
            .scalars()
            .all()
        )
        assert len(jobs) == 4, [(job.status, job.task_id, job.task_ids) for job in jobs]
        cancelled = [job for job in jobs if job.status == JobStatus.CANCELLED.value]
        assert len(cancelled) == 3
        assert all(job.task_id is not None for job in cancelled)
        merged = [job for job in jobs if job.task_ids]
        assert len(merged) == 1
        assert len(merged[0].task_ids) == 3
        assert merged[0].status == JobStatus.QUEUED.value
        assert sorted(merged[0].style_codes) == ["SUP-0", "SUP-1", "SUP-2"]


def test_a_cohort_spread_over_the_jitter_window_still_merges() -> None:
    """抖动把同一批任务摊到几十秒内时，仍要收拢成**一个**作业，并且此后同刻到期。

    生产回归：31 个"1 款 1 任务"创建时间被抖动摊开 → 每分钟只到一个 → 各自成作业，
    合并永不成立（请求数降不下来）。
    """
    settings = get_settings()
    now = datetime.now(UTC)
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant, email="cohort@example.com")
        make_store(db, owner=user, code="S1", region_id="R1")
        for index in range(3):
            make_style(db, owner=user, code=f"COHORT-{index}")
            task = _make_task(db, tenant.id, created_by=user.id, name=f"同批任务 {index}")
            task.style_codes = [f"COHORT-{index}"]
            task.cadence_seconds = 120
            # 到期时刻被抖动摊开：0 / 5 / 9 秒（都在抖动幅度 12 秒之内）
            task.next_run_at = now + timedelta(seconds=index * 4.5)
        db.commit()
        user_id = user.id
    store_credential(settings, user_id)

    with SessionLocal() as db:
        assert dispatch_due_tasks(db, settings) == 3
        db.commit()

    with SessionLocal() as db:
        jobs = db.execute(select(ScanJob).where(ScanJob.kind == "MONITOR")).scalars().all()
        assert len(jobs) == 1, [(job.task_id, job.task_ids) for job in jobs]
        assert len(jobs[0].task_ids) == 3
        assert sorted(jobs[0].style_codes) == ["COHORT-0", "COHORT-1", "COHORT-2"]
        # 组内共用同一个 next_run_at：下一轮还会一起到期、继续合并
        stamps = {task.next_run_at for task in db.execute(select(MonitorTask)).scalars().all()}
        assert len(stamps) == 1


def test_a_task_due_beyond_the_window_waits_for_its_own_round() -> None:
    """窗口只收拢"马上到期"的任务：还差 150 秒的同组任务不提前扫。"""
    settings = get_settings()
    now = datetime.now(UTC)
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant, email="window@example.com")
        make_store(db, owner=user, code="S1", region_id="R1")
        for index, offset in enumerate((0.0, 150.0)):
            make_style(db, owner=user, code=f"WIN-{index}")
            task = _make_task(db, tenant.id, created_by=user.id, name=f"窗口任务 {index}")
            task.style_codes = [f"WIN-{index}"]
            task.cadence_seconds = 120
            task.next_run_at = now + timedelta(seconds=offset)
        db.commit()
        user_id = user.id
    store_credential(settings, user_id)

    with SessionLocal() as db:
        assert dispatch_due_tasks(db, settings) == 1
        db.commit()

    with SessionLocal() as db:
        job = db.execute(select(ScanJob).where(ScanJob.kind == "MONITOR")).scalar_one()
        assert not job.task_ids
        assert job.style_codes == ["WIN-0"]
        later = db.execute(
            select(MonitorTask).where(MonitorTask.style_codes == ["WIN-1"])
        ).scalar_one()
        assert later.next_run_at > now + timedelta(seconds=120)  # 原定时刻没被改动


def test_tasks_with_different_cadence_do_not_merge() -> None:
    """节奏不同的任务不合并：否则慢任务会被快任务的节奏拖着反复扫。"""
    settings = get_settings()
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant, email="cadence@example.com")
        make_store(db, owner=user, code="S1", region_id="R1")
        for index, cadence in enumerate((60, 300)):
            make_style(db, owner=user, code=f"CAD-{index}")
            task = _make_task(db, tenant.id, created_by=user.id, name=f"节奏任务 {index}")
            task.style_codes = [f"CAD-{index}"]
            task.cadence_seconds = cadence
        db.commit()
        user_id = user.id
    store_credential(settings, user_id)

    with SessionLocal() as db:
        assert dispatch_due_tasks(db, settings) == 2
        db.commit()

    with SessionLocal() as db:
        jobs = db.execute(select(ScanJob).where(ScanJob.kind == "MONITOR")).scalars().all()
        assert len(jobs) == 2
        assert all(not job.task_ids for job in jobs)
