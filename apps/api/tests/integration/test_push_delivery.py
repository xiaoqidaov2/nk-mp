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

from .helpers import make_store, make_style, make_tenant, make_user

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
        _user, _task, job, _channel = _seed(db, name="手动检查", with_task=False)
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
