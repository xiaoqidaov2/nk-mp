"""Per-user push notification channels and delivery history (``/me/...``).

Every route operates on the logged-in user only; no route accepts an owner or
tenant id. Targets are validated before they are stored, their secrets are
encrypted with the platform credential key, and another user's channel or
delivery always reads back as 404.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_session, get_db, require_self_write, settings_dep
from app.config import Settings
from app.models import (
    AuthSession,
    DingTalkBotLink,
    MonitorTask,
    NotificationChannel,
    NotificationDelivery,
    NotificationPlatform,
    User,
)
from app.schemas import (
    DingTalkLinkCodeOut,
    DingTalkLinkOut,
    NotificationChannelIn,
    NotificationChannelOut,
    NotificationChannelPatch,
    NotificationDeliveryOut,
)
from app.security import decrypt_secret, encrypt_secret, secret_fingerprint
from app.services import dingtalk_bot, push

router = APIRouter(prefix="/me", tags=["push"])

PLATFORM_VALUES = {platform.value for platform in NotificationPlatform}


def _channel_out(channel: NotificationChannel) -> NotificationChannelOut:
    return NotificationChannelOut(
        id=channel.id,
        platform=channel.platform,
        name=channel.name,
        target_hint=channel.target_hint,
        enabled=channel.enabled,
        last_ok_at=channel.last_ok_at,
        last_error_code=channel.last_error_code,
        last_error=channel.last_error,
        last_error_at=channel.last_error_at,
        created_at=channel.created_at,
    )


def _load_channel(db: Session, user: User, channel_id: uuid.UUID) -> NotificationChannel:
    channel = db.execute(
        select(NotificationChannel).where(
            NotificationChannel.id == channel_id,
            NotificationChannel.owner_user_id == user.id,
        )
    ).scalar_one_or_none()
    if channel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "没有找到这个渠道。")
    return channel


def _summary(body: str) -> str:
    return (body or "").splitlines()[0][:60]


@router.get("/notification-channels", response_model=list[NotificationChannelOut])
def list_notification_channels(
    db: Session = Depends(get_db),
    payload: tuple[User, AuthSession] = Depends(get_current_session),
) -> list[NotificationChannelOut]:
    user = payload[0]
    rows = (
        db.execute(
            select(NotificationChannel)
            .where(NotificationChannel.owner_user_id == user.id)
            .order_by(NotificationChannel.created_at)
        )
        .scalars()
        .all()
    )
    return [_channel_out(row) for row in rows]


@router.post(
    "/notification-channels",
    response_model=NotificationChannelOut,
    status_code=status.HTTP_201_CREATED,
)
def create_notification_channel(
    body: NotificationChannelIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(settings_dep),
    payload: tuple[User, AuthSession] = Depends(require_self_write),
) -> NotificationChannelOut:
    user, _ = payload
    platform = body.platform.strip().lower()
    if platform not in PLATFORM_VALUES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "不支持的推送平台。")
    try:
        target = push.build_target(platform, body.fields)
        push.validate_target(target, settings)
    except push.PushTargetError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.detail) from exc
    total = db.execute(
        select(func.count())
        .select_from(NotificationChannel)
        .where(NotificationChannel.owner_user_id == user.id)
    ).scalar_one()
    if total >= settings.push_max_channels_per_user:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"最多只能添加 {settings.push_max_channels_per_user} 个推送渠道。",
        )
    fingerprint = secret_fingerprint(target, purpose="push")
    duplicate = db.execute(
        select(NotificationChannel.id).where(
            NotificationChannel.owner_user_id == user.id,
            NotificationChannel.target_fingerprint == fingerprint,
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "这个渠道已经添加过了。")
    channel = NotificationChannel(
        owner_user_id=user.id,
        platform=platform,
        name=body.name.strip()[:64] or push.PLATFORM_LABELS.get(platform, platform),
        target_enc=encrypt_secret(target),
        target_fingerprint=fingerprint,
        target_hint=push.target_hint(platform, target),
        enabled=True,
    )
    db.add(channel)
    db.commit()
    return _channel_out(channel)


@router.patch("/notification-channels/{channel_id}", response_model=NotificationChannelOut)
def update_notification_channel(
    channel_id: uuid.UUID,
    body: NotificationChannelPatch,
    db: Session = Depends(get_db),
    payload: tuple[User, AuthSession] = Depends(require_self_write),
) -> NotificationChannelOut:
    user, _ = payload
    channel = _load_channel(db, user, channel_id)
    if body.name is not None:
        channel.name = body.name.strip()[:64]
    if body.enabled is not None:
        channel.enabled = body.enabled
    db.commit()
    return _channel_out(channel)


@router.delete("/notification-channels/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_notification_channel(
    channel_id: uuid.UUID,
    db: Session = Depends(get_db),
    payload: tuple[User, AuthSession] = Depends(require_self_write),
) -> Response:
    user, _ = payload
    channel = _load_channel(db, user, channel_id)
    db.delete(channel)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/notification-channels/{channel_id}/test")
def test_notification_channel(
    channel_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(settings_dep),
    payload: tuple[User, AuthSession] = Depends(require_self_write),
) -> dict:
    """Send the fixed test message through this channel and record the result."""
    user, _ = payload
    channel = _load_channel(db, user, channel_id)
    now = datetime.now(UTC)
    try:
        target = decrypt_secret(channel.target_enc)
    except Exception:  # noqa: BLE001 - an unreadable secret is a failed test
        ok, error_code = False, push.PUSH_DELIVERY_FAILED
    else:
        ok, error_code = push.send_now(target, push.TEST_TITLE, push.TEST_BODY, settings)
    if ok:
        channel.last_ok_at = now
        channel.last_error_code = None
        channel.last_error = None
        channel.last_error_at = None
    else:
        channel.last_error_code = error_code
        channel.last_error = push.DELIVERY_FAILED_TEXT
        channel.last_error_at = now
    db.commit()
    return {"ok": ok, "error": None if ok else push.DELIVERY_FAILED_TEXT}


@router.get("/notifications", response_model=list[NotificationDeliveryOut])
def list_notifications(
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    payload: tuple[User, AuthSession] = Depends(get_current_session),
) -> list[NotificationDeliveryOut]:
    user = payload[0]
    rows = db.execute(
        select(NotificationDelivery, NotificationChannel, MonitorTask)
        .join(NotificationChannel, NotificationChannel.id == NotificationDelivery.channel_id)
        .outerjoin(MonitorTask, MonitorTask.id == NotificationDelivery.task_id)
        .where(NotificationDelivery.owner_user_id == user.id)
        .order_by(NotificationDelivery.created_at.desc(), NotificationDelivery.id.desc())
        .limit(limit)
    ).all()
    return [
        NotificationDeliveryOut(
            id=delivery.id,
            job_id=delivery.job_id,
            task_name=task.name if task is not None else None,
            channel_id=delivery.channel_id,
            channel_name=channel.name,
            platform=channel.platform,
            state=delivery.state,
            attempts=delivery.attempts,
            last_error=delivery.last_error,
            created_at=delivery.created_at,
            sent_at=delivery.sent_at,
            title=delivery.title,
            summary=_summary(delivery.body),
        )
        for delivery, channel, task in rows
    ]


@router.post("/notifications/{delivery_id}/retry", status_code=status.HTTP_202_ACCEPTED)
def retry_notification(
    delivery_id: int,
    db: Session = Depends(get_db),
    settings: Settings = Depends(settings_dep),
    payload: tuple[User, AuthSession] = Depends(require_self_write),
) -> dict:
    """Queue a delivery again; a SENT row may be re-sent on purpose."""
    user, _ = payload
    delivery = db.execute(
        select(NotificationDelivery).where(
            NotificationDelivery.id == delivery_id,
            NotificationDelivery.owner_user_id == user.id,
        )
    ).scalar_one_or_none()
    if delivery is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "没有找到这条推送记录。")
    push.retry_delivery(db, settings, delivery)
    db.commit()
    return {"ok": True}


@router.post("/dingtalk/link-code", response_model=DingTalkLinkCodeOut)
def create_dingtalk_link_code(
    db: Session = Depends(get_db),
    settings: Settings = Depends(settings_dep),
    payload: tuple[User, AuthSession] = Depends(require_self_write),
) -> DingTalkLinkCodeOut:
    """生成一次性验证码：在钉钉里把它发给平台机器人，即完成本账号的会话绑定。"""
    user, _ = payload
    if not (settings.dingtalk_bot_enabled and settings.dingtalk_bot_client_id):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "钉钉机器人还没有配置，请联系管理员。"
        )
    row = dingtalk_bot.issue_code(db, settings, user.id)
    db.commit()
    return DingTalkLinkCodeOut(code=row.code, expires_at=row.expires_at)


@router.get("/dingtalk/links", response_model=list[DingTalkLinkOut])
def list_dingtalk_links(
    db: Session = Depends(get_db),
    payload: tuple[User, AuthSession] = Depends(get_current_session),
) -> list[DingTalkLinkOut]:
    user = payload[0]
    rows = (
        db.execute(
            select(DingTalkBotLink)
            .where(DingTalkBotLink.owner_user_id == user.id)
            .order_by(DingTalkBotLink.created_at)
        )
        .scalars()
        .all()
    )
    return [
        DingTalkLinkOut(
            id=row.id,
            conversation_title=row.conversation_title,
            conversation_type=row.conversation_type,
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.delete("/dingtalk/links/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_dingtalk_link(
    link_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(settings_dep),
    payload: tuple[User, AuthSession] = Depends(require_self_write),
) -> Response:
    user, _ = payload
    row = db.execute(
        select(DingTalkBotLink).where(
            DingTalkBotLink.id == link_id, DingTalkBotLink.owner_user_id == user.id
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "没有找到这个绑定。")
    dingtalk_bot.drop_channel(db, settings, row)
    db.delete(row)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
