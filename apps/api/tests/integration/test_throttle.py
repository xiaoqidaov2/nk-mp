"""账号级冷却（PostgreSQL）：阶梯升级、通知节流、恢复与清空。

冷却写在 owner 的 credential 行上，所以这一组测试直接对表断言：
窗口是否符合阶梯、连续命中是否升级且不缩短、通知是否按间隔节流、
以及"整轮成功"只重置阶梯不清除仍在生效的窗口。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Credential, NotificationChannel, NotificationDelivery
from app.security import encrypt_secret, secret_fingerprint
from app.services import quota, throttle

from .helpers import make_tenant, make_user, store_credential


def _seed_owner(*, with_channel: bool = True):
    """租户 + 用户 + Nike 连接（可选：一个启用的推送渠道）。"""
    settings = get_settings()
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant)
        if with_channel:
            db.add(
                NotificationChannel(
                    owner_user_id=user.id,
                    platform="custom",
                    name="冷却测试渠道",
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
    store_credential(settings, user_id)
    return settings, user_id


def _record(db, owner_id) -> Credential:
    return db.execute(
        select(Credential).where(Credential.owner_user_id == owner_id)
    ).scalar_one()


def test_throttled_hits_escalate_the_ladder_and_cap_it() -> None:
    settings, owner_id = _seed_owner()
    with SessionLocal() as db:
        first = throttle.enter(db, settings, owner_id, throttle.THROTTLED_REASON)
        db.commit()
        assert first is not None
        assert 290 <= (first - datetime.now(UTC)).total_seconds() <= 305
        assert _record(db, owner_id).cooldown_level == 1

        second = throttle.enter(db, settings, owner_id, throttle.THROTTLED_REASON)
        db.commit()
        assert second is not None
        assert 890 <= (second - datetime.now(UTC)).total_seconds() <= 905
        assert _record(db, owner_id).cooldown_level == 2

        third = throttle.enter(db, settings, owner_id, throttle.THROTTLED_REASON)
        db.commit()
        assert third is not None and 1790 <= (third - datetime.now(UTC)).total_seconds() <= 1805
        assert _record(db, owner_id).cooldown_level == 3

        fourth = throttle.enter(db, settings, owner_id, throttle.THROTTLED_REASON)
        db.commit()
        assert fourth is not None and 3590 <= (fourth - datetime.now(UTC)).total_seconds() <= 3605
        assert _record(db, owner_id).cooldown_level == 4

        # 已在末档：继续命中保持 3600 秒，不再升级。
        fifth = throttle.enter(db, settings, owner_id, throttle.THROTTLED_REASON)
        db.commit()
        assert fifth is not None and 3590 <= (fifth - datetime.now(UTC)).total_seconds() <= 3605
        assert _record(db, owner_id).cooldown_level == 4


def test_budget_reason_keeps_the_ladder_and_never_shortens_a_window() -> None:
    settings, owner_id = _seed_owner()
    now = datetime.now(UTC)
    expected = quota.resume_at_for("platform_hourly_budget", now)
    assert expected is not None
    with SessionLocal() as db:
        until = throttle.enter(db, settings, owner_id, "platform_hourly_budget")
        db.commit()
        assert until is not None
        assert abs((until - expected).total_seconds()) <= 2
        record = _record(db, owner_id)
        assert record.cooldown_level == 0
        assert record.cooldown_reason == "platform_hourly_budget"

        # 已有的更长窗口（风控）不会被额度类进入缩短；原因保持治理中的那个。
        # 必须挑一个晚于"次日 UTC 00:00 + 30s"的窗口，否则额度窗口本来就更晚。
        longer = now + timedelta(days=1, hours=2)
        record.cooldown_until = longer
        record.cooldown_reason = throttle.THROTTLED_REASON
        db.commit()

        again = throttle.enter(db, settings, owner_id, "tenant_budget")
        db.commit()
        record = _record(db, owner_id)
        assert again is not None and abs((again - longer).total_seconds()) <= 2
        assert abs((record.cooldown_until - longer).total_seconds()) <= 2
        assert record.cooldown_reason == throttle.THROTTLED_REASON


def test_notices_are_gated_by_the_interval() -> None:
    settings, owner_id = _seed_owner()
    with SessionLocal() as db:
        throttle.enter(db, settings, owner_id, throttle.THROTTLED_REASON)
        db.commit()
        rows = db.execute(select(NotificationDelivery)).scalars().all()
        assert len(rows) == 1
        notice = rows[0]
        assert notice.job_id is None and notice.task_id is None
        assert notice.title == throttle.NOTICE_TITLE
        assert "被 Nike 限速" in notice.body and "自动继续" in notice.body

        # 间隔内再次进入（含升级）不再发第二条。
        throttle.enter(db, settings, owner_id, throttle.THROTTLED_REASON)
        db.commit()
        assert len(db.execute(select(NotificationDelivery)).scalars().all()) == 1

        # 把上次通知时间推到间隔之前 → 允许再发一条。
        record = _record(db, owner_id)
        record.cooldown_notified_at = datetime.now(UTC) - timedelta(
            seconds=settings.throttle_notice_interval_seconds + 60
        )
        db.commit()
        throttle.enter(db, settings, owner_id, throttle.THROTTLED_REASON)
        db.commit()
        assert len(db.execute(select(NotificationDelivery)).scalars().all()) == 2


def test_clear_resets_the_ladder_but_keeps_an_active_window() -> None:
    settings, owner_id = _seed_owner(with_channel=False)
    with SessionLocal() as db:
        throttle.enter(db, settings, owner_id, throttle.THROTTLED_REASON)
        db.commit()
        throttle.clear(db, owner_id)
        db.commit()
        record = _record(db, owner_id)
        # 仍在生效的窗口不提前解除，但阶梯已复位。
        assert record.cooldown_until is not None
        assert record.cooldown_level == 0

        record.cooldown_until = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
        throttle.clear(db, owner_id)
        db.commit()
        record = _record(db, owner_id)
        assert record.cooldown_until is None
        assert record.cooldown_reason is None


def test_active_reports_only_future_windows() -> None:
    settings, owner_id = _seed_owner(with_channel=False)
    with SessionLocal() as db:
        throttle.enter(db, settings, owner_id, throttle.THROTTLED_REASON)
        db.commit()
        cooldown = throttle.current(db, owner_id)
        assert cooldown is not None and cooldown.reason == throttle.THROTTLED_REASON
        assert owner_id in throttle.active(db, [owner_id])

        record = _record(db, owner_id)
        record.cooldown_until = datetime.now(UTC) - timedelta(seconds=5)
        db.commit()
        assert throttle.current(db, owner_id) is None
        assert throttle.active(db, [owner_id]) == {}
