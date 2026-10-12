"""Push outbox integration: enqueue rules, the retry state machine and ownership.

These tests never call a notification platform: ``send_now`` is monkeypatched
and the enqueue step only writes rows. Everything runs against the scratch
PostgreSQL database like the other integration tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select

from app.config import Settings
from app.db import SessionLocal
from app.models import (
    ChangeEvent,
    EventType,
    JobStatus,
    MonitorTask,
    NotificationChannel,
    NotificationDelivery,
    NotificationDeliveryState,
    ScanJob,
    Tenant,
)
from app.security import encrypt_secret, secret_fingerprint
from app.services import push

from .helpers import (
    csrf_headers,
    login_session,
    make_store,
    make_style,
    make_tenant,
    make_user,
    session_cookies,
)

TARGET = "json://127.0.0.1:19100/__push"


def _settings(**overrides) -> Settings:
    base = {"credential_key": "test-key-for-unit-tests"}
    base.update(overrides)
    return Settings(**base)


def _seed(
    db,
    *,
    owner=None,
    name: str = "推送任务",
    notify_restock: bool = True,
    notify_sold_out: bool = False,
    notify_price_change: bool = True,
    channel_enabled: bool = True,
    with_task: bool = True,
    with_channel: bool = True,
    event_types: tuple[str, ...] = ("RESTOCK", "SOLD_OUT", "PRICE_CHANGE"),
):
    if owner is None:
        tenant = make_tenant(db)
        user = make_user(db, tenant)
    else:
        user = owner
        tenant = db.get(Tenant, user.tenant_id)
    store = make_store(db, owner=user)
    style = make_style(db, owner=user)
    task = None
    if with_task:
        task = MonitorTask(
            tenant_id=tenant.id,
            name=name,
            store_codes=[store.store_code],
            style_codes=[style.style_code],
            all_stores=False,
            notify_on_restock=notify_restock,
            notify_on_sold_out=notify_sold_out,
            notify_on_price_change=notify_price_change,
            created_by=user.id,
        )
        db.add(task)
        db.flush()
    job = ScanJob(
        tenant_id=tenant.id,
        task_id=task.id if task is not None else None,
        status=JobStatus.SUCCEEDED.value,
        owner_user_id=user.id,
    )
    db.add(job)
    db.flush()
    checked_at = datetime.now(UTC)
    for order, event_type in enumerate(event_types):
        db.add(
            ChangeEvent(
                tenant_id=tenant.id,
                owner_user_id=user.id,
                store_id=store.id,
                style_id=style.id,
                job_id=job.id,
                size_label="42",
                event_type=event_type,
                before_total_price=699 if event_type == "PRICE_CHANGE" else None,
                after_total_price=699 if event_type in ("RESTOCK", "PRICE_CHANGE") else None,
                payload_hash=f"{event_type}:{order}",
                dedupe_key=f"{event_type}:{uuid4().hex}",
                checked_at=checked_at,
            )
        )
    channel = None
    if with_channel:
        channel = NotificationChannel(
            owner_user_id=user.id,
            platform="custom",
            name="测试渠道",
            target_enc=encrypt_secret(TARGET),
            target_fingerprint=secret_fingerprint(TARGET, purpose="push"),
            target_hint="••••push",
            enabled=channel_enabled,
        )
        db.add(channel)
        db.flush()
    return user, task, job, channel


def test_enqueue_aggregates_the_notified_types_once() -> None:
    settings = _settings()
    with SessionLocal() as db:
        _user, _task, job, channel = _seed(db)
        created = push.enqueue_for_job(db, settings, job)
        assert created == 1
        db.commit()
        job_id, channel_id = job.id, channel.id

    with SessionLocal() as db:
        row = db.execute(select(NotificationDelivery)).scalar_one()
        assert row.job_id == job_id
        assert row.channel_id == channel_id
        assert row.state == NotificationDeliveryState.PENDING.value
        assert row.title.startswith("【库存提醒】")
        assert "补货" in row.body
        assert "变价" in row.body
        assert "售罄" not in row.body

    with SessionLocal() as db:
        job = db.get(ScanJob, job_id)
        assert push.enqueue_for_job(db, settings, job) == 0
        db.commit()
    with SessionLocal() as db:
        assert len(db.execute(select(NotificationDelivery)).scalars().all()) == 1


def test_enqueue_skips_disabled_channels_and_unnotified_jobs() -> None:
    settings = _settings()
    with SessionLocal() as db:
        _user, _task, job, _channel = _seed(db, channel_enabled=False)
        assert push.enqueue_for_job(db, settings, job) == 0
    with SessionLocal() as db:
        _user, _task, job, _channel = _seed(
            db, name="全员关闭", notify_restock=False, notify_sold_out=False,
            notify_price_change=False,
        )
        assert push.enqueue_for_job(db, settings, job) == 0
    with SessionLocal() as db:
        assert db.execute(select(NotificationDelivery)).scalars().all() == []


def test_enqueue_respects_the_rolling_daily_cap() -> None:
    settings = _settings(push_max_per_day=1)
    with SessionLocal() as db:
        owner, _task, job, _channel = _seed(db)
        assert push.enqueue_for_job(db, settings, job) == 1
        db.commit()
    with SessionLocal() as db:
        _owner, _task, second_job, _channel = _seed(
            db, owner=owner, name="第二次检查", with_channel=False
        )
        assert push.enqueue_for_job(db, settings, second_job) == 0
    with SessionLocal() as db:
        assert len(db.execute(select(NotificationDelivery)).scalars().all()) == 1


def test_turned_off_push_produces_nothing() -> None:
    settings = _settings(push_enabled=False)
    with SessionLocal() as db:
        _user, _task, job, _channel = _seed(db)
        assert push.enqueue_for_job(db, settings, job) == 0


def _touch(db, delivery_id: int) -> None:
    """Make a backed-off delivery due again (what the clock would do)."""
    row = db.get(NotificationDelivery, delivery_id)
    row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()


def _drain_once(settings: Settings) -> None:
    with SessionLocal() as db:
        delivery = push.lease_due_delivery(db, settings)
        assert delivery is not None
        db.commit()
        push.deliver(db, settings, delivery)


def test_delivery_retries_then_fails_and_can_be_retried(monkeypatch) -> None:
    settings = _settings(push_backoff_seconds="60,300,900")
    with SessionLocal() as db:
        _user, _task, job, channel = _seed(db)
        assert push.enqueue_for_job(db, settings, job) == 1
        db.commit()
        delivery_id, channel_id = (
            db.execute(select(NotificationDelivery)).scalar_one().id,
            channel.id,
        )

    monkeypatch.setattr(push, "send_now", lambda *args, **kwargs: (False, push.PUSH_DELIVERY_FAILED))
    _drain_once(settings)
    with SessionLocal() as db:
        row = db.get(NotificationDelivery, delivery_id)
        assert row.state == NotificationDeliveryState.PENDING.value
        assert row.attempts == 1
        assert row.next_attempt_at > datetime.now(UTC)
        assert row.last_error_code == push.PUSH_DELIVERY_FAILED
        assert row.last_error == push.DELIVERY_FAILED_TEXT
        assert db.get(NotificationChannel, channel_id).last_error_at is not None

    for _ in range(3):
        with SessionLocal() as db:
            _touch(db, delivery_id)
        _drain_once(settings)
    with SessionLocal() as db:
        row = db.get(NotificationDelivery, delivery_id)
        assert row.attempts == 4
        assert row.state == NotificationDeliveryState.FAILED.value
        assert row.lease_until is None

    with SessionLocal() as db:
        row = db.get(NotificationDelivery, delivery_id)
        push.retry_delivery(db, settings, row)
        db.commit()
    with SessionLocal() as db:
        row = db.get(NotificationDelivery, delivery_id)
        assert row.state == NotificationDeliveryState.PENDING.value
        assert row.attempts == 0
        assert row.last_error is None

    monkeypatch.setattr(push, "send_now", lambda *args, **kwargs: (True, None))
    _drain_once(settings)
    with SessionLocal() as db:
        row = db.get(NotificationDelivery, delivery_id)
        assert row.state == NotificationDeliveryState.SENT.value
        assert row.sent_at is not None
        assert db.get(NotificationChannel, channel_id).last_ok_at is not None


def test_deleting_a_channel_keeps_delivery_history() -> None:
    settings = _settings()
    with SessionLocal() as db:
        _user, _task, job, channel = _seed(db)
        assert push.enqueue_for_job(db, settings, job) == 1
        db.commit()
        delivery_id = db.execute(select(NotificationDelivery)).scalar_one().id
        channel_id = channel.id

    with SessionLocal() as db:
        db.delete(db.get(NotificationChannel, channel_id))
        db.commit()

    with SessionLocal() as db:
        row = db.get(NotificationDelivery, delivery_id)
        assert row is not None
        assert row.channel_id is None


def test_delivery_for_a_removed_channel_fails_instead_of_looping() -> None:
    settings = _settings()
    with SessionLocal() as db:
        _user, _task, job, channel = _seed(db)
        assert push.enqueue_for_job(db, settings, job) == 1
        db.commit()
        db.delete(channel)
        db.commit()

    with SessionLocal() as db:
        delivery = push.lease_due_delivery(db, settings)
        assert delivery is not None
        assert delivery.channel_id is None
        db.commit()
        push.deliver(db, settings, delivery)

    with SessionLocal() as db:
        row = db.execute(select(NotificationDelivery)).scalar_one()
        assert row.state == NotificationDeliveryState.FAILED.value
        assert row.last_error_code == push.CHANNEL_REMOVED
        assert row.last_error == push.CHANNEL_REMOVED_TEXT
        assert row.lease_until is None


def test_a_pasted_dingtalk_webhook_address_is_accepted_by_the_api(client) -> None:
    """端到端：客户把整段 Webhook 地址填进渠道表单，API 自动还原出 token。"""
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant, email=f"webhook-{uuid4().hex[:8]}@example.com")
        db.commit()
        email = user.email
    session = login_session(client, email)

    response = client.post(
        "/api/v1/me/notification-channels",
        json={
            "platform": "dingtalk",
            "name": "",
            "fields": {
                "access_token": "https://oapi.dingtalk.com/robot/send?access_token=abcdef123456"
            },
        },
        headers=csrf_headers(session["csrf"]),
        cookies=session_cookies(session["session"]),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["platform"] == "dingtalk"
    # 提示是真实 token 的尾 4 位，而不是 Apprise URL 的 “down”。
    assert body["target_hint"] == "••••3456"


def test_enqueue_includes_first_time_arrival() -> None:
    """未铺货→有货（新上架）必须与补货一样进入推送，否则这类变化会被静默丢掉。"""
    settings = _settings()
    with SessionLocal() as db:
        _user, _task, job, _channel = _seed(
            db,
            name="新上架任务",
            notify_restock=True,
            notify_sold_out=False,
            notify_price_change=False,
            event_types=("NEW_ARRIVAL_AT_STORE",),
        )
        assert push.enqueue_for_job(db, settings, job) == 1
        db.commit()
        job_id = job.id

    with SessionLocal() as db:
        row = db.execute(select(NotificationDelivery)).scalar_one()
        assert row.job_id == job_id
        assert row.state == NotificationDeliveryState.PENDING.value
        assert "新上架" in row.body


def test_enqueue_respects_the_restock_switch_for_arrivals() -> None:
    settings = _settings()
    with SessionLocal() as db:
        _user, _task, job, _channel = _seed(
            db,
            name="关闭补货的任务",
            notify_restock=False,
            notify_sold_out=False,
            notify_price_change=False,
            event_types=("NEW_ARRIVAL_AT_STORE",),
        )
        assert push.enqueue_for_job(db, settings, job) == 0
    with SessionLocal() as db:
        assert db.execute(select(NotificationDelivery)).scalars().all() == []


def test_push_usage_reports_the_cap() -> None:
    settings = _settings(push_max_per_day=2)
    with SessionLocal() as db:
        user, _task, job, _channel = _seed(db)
        push.enqueue_for_job(db, settings, job)
        db.commit()
        used, cap = push.push_usage(db, settings, user.id)
    assert (used, cap) == (1, 2)


def test_push_usage_endpoint_reports_the_cap(client) -> None:
    """页面靠这个接口显示「今日已推送 N/上限」：到顶时不再静默丢消息。"""
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant, email=f"usage-{uuid4().hex[:8]}@example.com")
        db.commit()
        email = user.email
    login_session(client, email)

    response = client.get("/api/v1/me/push-usage")
    assert response.status_code == 200, response.text
    assert response.json() == {"used": 0, "cap": 200, "limited": False}


def test_manual_scan_without_a_task_still_queues_a_delivery() -> None:
    """手动「立即检查」产生的作业没有 task_id：检测到的变化同样要推送。"""
    settings = _settings()
    with SessionLocal() as db:
        user, _task, job, channel = _seed(
            db, name="手动检查", with_task=False, event_types=("RESTOCK",)
        )
        assert push.enqueue_for_job(db, settings, job) == 1
        db.commit()
        job_id, channel_id, owner_id = job.id, channel.id, user.id
    with SessionLocal() as db:
        row = db.execute(
            select(NotificationDelivery).where(NotificationDelivery.job_id == job_id)
        ).scalar_one()
        assert row.channel_id == channel_id
        assert row.owner_user_id == owner_id
        assert push.TASK_LESS_TITLE in row.title


def test_merged_job_queues_one_delivery_per_task() -> None:
    """合并作业：每个任务各推一条，只带自己的款与开关（B 关了变价 → B 的消息里没有变价）。"""
    settings = _settings()
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant, email=f"merge-push-{uuid4().hex[:8]}@example.com")
        store = make_store(db, owner=user)
        style_a = make_style(db, owner=user, code="DN3707-202")
        style_b = make_style(db, owner=user, code="HJ2461-010")
        task_a = MonitorTask(
            tenant_id=tenant.id, name="任务A", store_codes=[store.store_code],
            style_codes=[style_a.style_code], all_stores=False,
            notify_on_restock=True, notify_on_sold_out=False, notify_on_price_change=True,
            created_by=user.id,
        )
        task_b = MonitorTask(
            tenant_id=tenant.id, name="任务B", store_codes=[store.store_code],
            style_codes=[style_b.style_code], all_stores=False,
            notify_on_restock=True, notify_on_sold_out=False, notify_on_price_change=False,
            created_by=user.id,
        )
        db.add_all([task_a, task_b])
        db.flush()
        channel = NotificationChannel(
            owner_user_id=user.id,
            platform="custom",
            name="合并推送",
            target_enc=encrypt_secret("json://example.test"),
            target_fingerprint=secret_fingerprint("json://example.test", purpose="push"),
            target_hint="••••push",
            enabled=True,
        )
        db.add(channel)
        db.flush()
        job = ScanJob(
            tenant_id=tenant.id, task_id=None, task_ids=[str(task_a.id), str(task_b.id)],
            kind="MONITOR", status=JobStatus.SUCCEEDED.value,
            store_codes=[store.store_code], style_codes=[style_a.style_code, style_b.style_code],
            size_labels=[], total_stores=1, owner_user_id=user.id,
        )
        db.add(job)
        db.flush()
        db.add_all(
            [
                _event(tenant, user, job, store, style_a, EventType.RESTOCK.value),
                _event(tenant, user, job, store, style_a, EventType.PRICE_CHANGE.value,
                       before=699.0, after=599.0),
                _event(tenant, user, job, store, style_b, EventType.RESTOCK.value),
                _event(tenant, user, job, store, style_b, EventType.PRICE_CHANGE.value,
                       before=699.0, after=499.0),
            ]
        )
        db.commit()
        job_id = job.id

    with SessionLocal() as db:
        job = db.get(ScanJob, job_id)
        assert push.enqueue_for_job(db, settings, job) == 2   # 每个任务各一条
        db.commit()

    with SessionLocal() as db:
        rows = (
            db.execute(
                select(NotificationDelivery).where(NotificationDelivery.job_id == job_id)
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        by_title = {row.title: row for row in rows}
        assert set(by_title) == {"【库存提醒】任务A", "【库存提醒】任务B"}  # 标题各带自己的任务名
        assert "DN3707-202" in by_title["【库存提醒】任务A"].body   # 只带自己的款
        assert "HJ2461-010" not in by_title["【库存提醒】任务A"].body
        assert "变价" in by_title["【库存提醒】任务A"].body         # A 开了变价
        assert "HJ2461-010" in by_title["【库存提醒】任务B"].body
        assert "变价" not in by_title["【库存提醒】任务B"].body      # B 关了变价


def _event(tenant, user, job, store, style, event_type: str, *, before=None, after=None) -> ChangeEvent:
    """构造一条变化事件（dedupe_key/payload_hash 非空）。"""
    key = f"{event_type}:{uuid4().hex}"  # dedupe_key 列 64 字符上限
    return ChangeEvent(
        tenant_id=tenant.id,
        owner_user_id=user.id,
        job_id=job.id,
        store_id=store.id,
        style_id=style.id,
        size_label="42",
        event_type=event_type,
        before_total_price=before,
        after_total_price=after,
        payload_hash=key,
        dedupe_key=key,
        checked_at=datetime.now(UTC),
    )
