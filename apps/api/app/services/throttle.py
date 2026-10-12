"""Account-level cooldown for quota/rate-limited accounts.

When an account runs out of its query budget or gets rate-limited by upstream
(HTTP 403/429), scanning stops for that account instead of hammering the same
wall store after store. The cooldown is stored on the owner's ``credential``
row, so every task of that account obeys it: the scheduler simply skips
dispatch until ``cooldown_until`` and then resumes automatically.

Two flavours:

* **budget** (``tenant_budget`` / ``platform_daily_budget`` /
  ``platform_hourly_budget``): the wall-clock window is known, so the cooldown
  ends at the next window boundary (plus a small margin); no ladder.
* **throttled**: upstream refused the request, so the account backs off along
  an escalating ladder (``throttle_backoff_seconds``). Repeated hits within a
  run of failures escalate; one fully successful run resets the ladder.

One notice is queued when a cooldown starts, at most once per
``throttle_notice_interval_seconds`` (24h => at most 3 notices), so the user
learns that scanning paused and when it will continue. The notice reuses the
push outbox (``push.enqueue_system_notification``), so it follows the normal
retry policy and shows up in the delivery history.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Credential
from app.services import push, quota

logger = logging.getLogger("nike_mp.throttle")

#: 上游风控命中（403/429）的冷却原因码。
THROTTLED_REASON = "throttled"
#: 额度类原因码（来自 ``UsageSnapshot.rejection()``）；没有窗口阶梯。
BUDGET_REASONS = ("tenant_budget", "platform_daily_budget", "platform_hourly_budget")
#: ``last_rejection`` 缺失时的兜底原因：按小时窗口处理（恢复最快，下一轮会纠正）。
HOURLY_REASON = "platform_hourly_budget"

NOTICE_TITLE = "检查已自动降速"

_REASON_TEXT = {
    "platform_hourly_budget": "本小时查询额度已用完",
    "platform_daily_budget": "今日查询额度已用完",
    "tenant_budget": "今日查询额度已用完",
    THROTTLED_REASON: "被 Nike 限速",
}

#: 客户端展示的北京时间（与推送正文一致）。
_CST = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class Cooldown:
    until: datetime
    reason: str | None


def _ladder(settings: Settings) -> list[int]:
    try:
        values = [
            int(part)
            for part in settings.throttle_backoff_seconds.split(",")
            if part.strip()
        ]
    except ValueError:
        values = []
    return [value for value in values if value > 0] or [300]


def current(session: Session, owner_user_id) -> Cooldown | None:
    """The active cooldown of one account, or ``None`` when it can run now."""
    if owner_user_id is None:
        return None
    return active(session, [owner_user_id]).get(owner_user_id)


def active(session: Session, owner_ids) -> dict:
    """Active cooldowns for several owners in one query (``owner_id -> Cooldown``)."""
    ids = [owner_id for owner_id in dict.fromkeys(owner_ids) if owner_id is not None]
    if not ids:
        return {}
    now = datetime.now(UTC)
    rows = session.execute(
        select(
            Credential.owner_user_id,
            Credential.cooldown_until,
            Credential.cooldown_reason,
        ).where(
            Credential.owner_user_id.in_(ids),
            Credential.cooldown_until.is_not(None),
            Credential.cooldown_until > now,
        )
    ).all()
    return {
        row.owner_user_id: Cooldown(until=row.cooldown_until, reason=row.cooldown_reason)
        for row in rows
    }


def enter(
    session: Session,
    settings: Settings,
    owner_user_id,
    reason: str,
    *,
    resume_at: datetime | None = None,
) -> datetime | None:
    """Record (or extend) the account cooldown and queue one notice.

    ``resume_at`` carries the precomputed window for budget reasons
    (``QuotaGuard.resume_at()``); the throttled ladder is computed here. The
    returned moment is the stored ``cooldown_until``; an existing longer
    cooldown is never shortened. Returns ``None`` when the owner has no
    credential row (nothing to freeze).
    """
    if owner_user_id is None:
        return None
    record = session.execute(
        select(Credential).where(Credential.owner_user_id == owner_user_id)
    ).scalar_one_or_none()
    if record is None:
        return None

    now = datetime.now(UTC)
    is_budget = reason in BUDGET_REASONS
    if is_budget:
        computed = resume_at or quota.resume_at_for(reason, now)
        if computed is None:  # pragma: no cover - every budget reason has a window
            computed = now + timedelta(seconds=_ladder(settings)[0])
    else:
        ladder = _ladder(settings)
        level = min(int(record.cooldown_level or 0) + 1, len(ladder))
        record.cooldown_level = level
        computed = now + timedelta(seconds=ladder[level - 1])

    existing = record.cooldown_until
    if existing is not None and existing > computed:
        # 不缩短已有窗口：保留仍在生效的原因说明。
        until = existing
    else:
        until = computed
        record.cooldown_reason = reason
    record.cooldown_until = until

    notified = record.cooldown_notified_at
    if notified is None or (
        (now - notified).total_seconds() >= settings.throttle_notice_interval_seconds
    ):
        queued = push.enqueue_system_notification(
            session,
            settings,
            owner_user_id=owner_user_id,
            title=NOTICE_TITLE,
            body=_notice_body(reason, until),
        )
        if queued:
            record.cooldown_notified_at = now
            logger.info(
                "cooldown notice queued owner=%s reason=%s until=%s",
                owner_user_id,
                reason,
                until.isoformat(),
            )
    logger.warning(
        "cooldown entered owner=%s reason=%s until=%s", owner_user_id, reason, until.isoformat()
    )
    session.flush()
    return until


def clear(session: Session, owner_user_id) -> None:
    """A fully successful run resets the ladder; an expired window is cleared.

    An *active* cooldown is left alone: one lucky success from another manual
    scan must not lift the pause while the window is still running.
    """
    if owner_user_id is None:
        return
    record = session.execute(
        select(Credential).where(Credential.owner_user_id == owner_user_id)
    ).scalar_one_or_none()
    if record is None:
        return
    record.cooldown_level = 0
    if record.cooldown_until is not None and record.cooldown_until <= datetime.now(UTC):
        record.cooldown_until = None
        record.cooldown_reason = None
    session.flush()


def _notice_body(reason: str, until: datetime) -> str:
    text = _REASON_TEXT.get(reason, "查询受限")
    moment = until.astimezone(_CST).strftime("%H:%M")
    return f"原因：{text}。系统将在 {moment} 自动继续检查，无需操作。"
