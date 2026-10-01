"""Push notifications: channel targets, aggregated messages and the outbox.

A push channel belongs to exactly one user and stores its Apprise URL
encrypted; a fingerprint deduplicates equal targets per owner. This module is
the only place that understands Apprise URLs: the API submits platform fields
(``build_target``), validates the result (``validate_target``) and the worker
sends through ``send_now``.

Every finished scan job aggregates its notified events into a single message
per channel (``enqueue_for_job``) and stores it as a delivery row. The worker
leases due rows (``lease_due_delivery``), sends them (``deliver``) and applies
the configured retry backoff; ``retry_delivery`` lets the user try again from
the UI. Failure text is fixed and never contains Apprise logs or the target
URL.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, quote, urlsplit

import apprise
import httpx
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    ChangeEvent,
    EventType,
    MonitorTask,
    NotificationChannel,
    NotificationDelivery,
    NotificationDeliveryState,
    NotificationPlatform,
    ScanJob,
)
from app.security import decrypt_secret

logger = logging.getLogger("nike_mp.push")

PUSH_TARGET_INVALID = "PUSH_TARGET_INVALID"
PUSH_TARGET_PRIVATE = "PUSH_TARGET_PRIVATE"
PUSH_DELIVERY_FAILED = "PUSH_DELIVERY_FAILED"

#: 发送失败时对用户展示的固定文案；绝不透出 Apprise 日志或渠道地址。
DELIVERY_FAILED_TEXT = "发送失败：平台没有接受这条消息（可在渠道里点“发送测试”复查）。"

#: 测试消息（渠道详情页的“发送测试”）。
TEST_TITLE = "【测试】门店监控台"
TEST_BODY = "这是一条测试消息。收到即表示该渠道可用。"

#: 请求超时兜底（Apprise 默认等待上游的秒数）。
DEFAULT_TIMEOUT_SECONDS = 8
#: 投递行被 worker 领取后的租约时长；超时视为崩溃并允许重领。
DELIVERY_LEASE_SECONDS = 120
DELIVERY_RUNNING = "RUNNING"

#: 允许的 scheme；其余一律拒绝（含未知/危险 scheme）。
#: 其中 dingtalkapp 不是 Apprise scheme：它是企业机器人（AppKey/AppSecret + openConversationId）。
ALLOWED_SCHEMES = {
    "dingtalk",
    "dingtalkapp",
    "feishu",
    "wecombot",
    "schan",
    "pushdeer",
    "pushdeers",
    "bark",
    "barks",
    "ntfy",
    "ntfys",
    "gotify",
    "gotifys",
    "tgram",
    "mailto",
    "mailtos",
    "json",
    "jsons",
}
#: 这些 scheme 的目标是一个主机名，需要先做内网地址检查。
HOST_SCHEMES = {
    "pushdeer",
    "pushdeers",
    "bark",
    "barks",
    "ntfy",
    "ntfys",
    "gotify",
    "gotifys",
    "json",
    "jsons",
    "mailto",
    "mailtos",
}

#: 平台表单字段的中文名；缺失字段的报错文案用它拼装。
FIELD_LABELS = {
    "access_token": "Webhook access_token",
    "secret": "加签密钥",
    "robot_code": "AppKey（robotCode）",
    "robot_secret": "AppSecret",
    "conversation_id": "群会话 openConversationId",
    "token": "机器人 token",
    "key_or_url": "Webhook 地址或 key",
    "sendkey": "SendKey",
    "device_key": "Device Key",
    "server": "服务器地址",
    "smtp_host": "SMTP 服务器",
    "smtp_port": "SMTP 端口",
    "username": "邮箱账号",
    "password": "邮箱密码",
    "from": "发件地址",
    "to": "收件地址",
    "url": "Apprise URL",
}

#: 渠道留空名称时的默认名。
PLATFORM_LABELS = {
    NotificationPlatform.DINGTALK.value: "钉钉群机器人",
    NotificationPlatform.DINGTALK_APP.value: "钉钉企业机器人",
    NotificationPlatform.FEISHU.value: "飞书机器人",
    NotificationPlatform.WECOMBOT.value: "企业微信机器人",
    NotificationPlatform.SCHAN.value: "Server酱",
    NotificationPlatform.BARK.value: "Bark",
    NotificationPlatform.MAILTO.value: "邮件",
    NotificationPlatform.CUSTOM.value: "自定义",
}

_DEFAULT_BARK_SERVER = "https://api.day.app"
_WECOMBOT_KEY = re.compile(r"[?&]key=([A-Za-z0-9_-]+)")

#: 钉钉企业内部应用机器人（新版接口）：AppKey/AppSecret 换 token 后发消息。
DINGTALK_TOKEN_URL = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
DINGTALK_GROUP_SEND_URL = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
DINGTALK_DM_SEND_URL = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"

_CATEGORIES = (
    (EventType.RESTOCK.value, "补货"),
    (EventType.SOLD_OUT.value, "售罄"),
    (EventType.PRICE_CHANGE.value, "变价"),
)
DETAIL_LIMIT = 5
BODY_LIMIT = 900


class PushTargetError(RuntimeError):
    """A channel target the user submitted cannot be used."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _require(fields: dict[str, str], name: str) -> str:
    value = str(fields.get(name) or "").strip()
    if not value:
        raise PushTargetError(PUSH_TARGET_INVALID, f"请填写{FIELD_LABELS[name]}。")
    return value


def build_target(platform: str, fields: dict[str, str]) -> str:
    """Turn the per-platform form fields into the target we store."""
    if platform == NotificationPlatform.DINGTALK_APP.value:
        robot_code = _require(fields, "robot_code")
        robot_secret = _require(fields, "robot_secret")
        conversation_id = _require(fields, "conversation_id")
        return (
            f"dingtalkapp://{quote(robot_code, safe='')}"
            f"?secret={quote(robot_secret, safe='')}"
            f"&conversation={quote(conversation_id, safe='')}"
        )
    if platform == NotificationPlatform.DINGTALK.value:
        access_token = _require(fields, "access_token")
        secret = str(fields.get("secret") or "").strip()
        if secret:
            return f"dingtalk://{secret}@{access_token}/?format=markdown"
        return f"dingtalk://{access_token}/?format=markdown"
    if platform == NotificationPlatform.FEISHU.value:
        return f"feishu://{_require(fields, 'token')}"
    if platform == NotificationPlatform.WECOMBOT.value:
        raw = _require(fields, "key_or_url")
        found = _WECOMBOT_KEY.search(raw)
        key = found.group(1) if found else raw
        return f"wecombot://{key}"
    if platform == NotificationPlatform.SCHAN.value:
        return f"schan://{_require(fields, 'sendkey')}/"
    if platform == NotificationPlatform.BARK.value:
        device_key = _require(fields, "device_key")
        server = str(fields.get("server") or "").strip() or _DEFAULT_BARK_SERVER
        parsed = urlsplit(server if "://" in server else f"https://{server}")
        host = parsed.hostname or ""
        try:
            port = parsed.port
        except ValueError as exc:
            raise PushTargetError(PUSH_TARGET_INVALID, "请填写服务器地址。") from exc
        if port:
            host = f"{host}:{port}"
        if not host:
            raise PushTargetError(PUSH_TARGET_INVALID, "请填写服务器地址。")
        scheme = "bark" if parsed.scheme == "http" else "barks"
        return f"{scheme}://{host}/{device_key}"
    if platform == NotificationPlatform.MAILTO.value:
        host = _require(fields, "smtp_host")
        port = str(fields.get("smtp_port") or "").strip() or "587"
        username = _require(fields, "username")
        password = _require(fields, "password")
        from_addr = _require(fields, "from")
        to_addr = _require(fields, "to")
        return (
            f"mailtos://{quote(username)}:{quote(password)}@{host}:{port}"
            f"?from={quote(from_addr)}&to={quote(to_addr)}"
        )
    if platform == NotificationPlatform.CUSTOM.value:
        return _require(fields, "url")
    raise PushTargetError(PUSH_TARGET_INVALID, "不支持的推送平台。")


def target_hint(platform: str, url: str) -> str:
    """A short, non-secret reminder of what this channel points at."""
    if platform == NotificationPlatform.MAILTO.value:
        values = parse_qs(urlsplit(url).query).get("to") or [""]
        return values[0][:120]
    if platform == NotificationPlatform.DINGTALK_APP.value:
        parsed = _dingtalk_app_target(url)
        if parsed is None:
            return "••••"
        robot, _secret, conversation, user_id, is_dm = parsed
        tail = (user_id or conversation)[-4:]
        return f"{'钉钉单聊' if is_dm else '钉钉群'} · {robot[:20]} → ••••{tail}"
    return "••••" + url[-4:]


def _reject_private_host(url: str, scheme: str, settings: Settings) -> None:
    probe_scheme = "smtp" if scheme in {"mailto", "mailtos"} else "https"
    host = urlsplit(url)._replace(scheme=probe_scheme).hostname or ""
    if not host:
        raise PushTargetError(PUSH_TARGET_INVALID, "渠道地址无法解析，请检查填写内容。")
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise PushTargetError(
            PUSH_TARGET_INVALID, "渠道地址无法解析，请检查填写内容。"
        ) from exc
    if settings.push_allow_private_targets:
        return
    for info in infos:
        try:
            address = ipaddress.ip_address(str(info[4][0]))
        except ValueError:
            continue
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
        ):
            raise PushTargetError(
                PUSH_TARGET_PRIVATE, "不允许推送到内网地址；如确需，请联系管理员开启。"
            )


def validate_target(url: str, settings: Settings) -> None:
    """Reject schemes we do not support, private hosts and unknown targets."""
    scheme = urlsplit(url).scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise PushTargetError(
            PUSH_TARGET_INVALID,
            "不支持的推送地址；支持 dingtalk:// feishu:// wecombot:// schan:// bark:// "
            "pushdeer:// ntfy:// gotify:// tgram:// mailto:// json:// 等。",
        )
    if scheme in HOST_SCHEMES:
        _reject_private_host(url, scheme, settings)
    if scheme == "dingtalkapp":
        # 平台机器人不走 Apprise：结构对即可（凭证在发送时校验）。
        if not (urlsplit(url).hostname and _dingtalk_app_target(url)):
            raise PushTargetError(PUSH_TARGET_INVALID, "渠道地址无法识别，请检查填写内容。")
        return
    if not apprise.Apprise().add(url):
        raise PushTargetError(PUSH_TARGET_INVALID, "渠道地址无法识别，请检查填写内容。")


def _money(value) -> str:
    if value is None:
        return "—"
    return f"¥{value:,.2f}"


def _supplement(event: ChangeEvent) -> str:
    if event.event_type == EventType.RESTOCK.value:
        if event.after_total_price is None:
            return "待补价"
        return f"¥{event.after_total_price:,.2f}"
    if event.event_type == EventType.PRICE_CHANGE.value:
        return f"{_money(event.before_total_price)} → {_money(event.after_total_price)}"
    return ""


def _detail_line(event: ChangeEvent) -> str:
    style_code = event.style.style_code if event.style is not None else ""
    store_code = event.store.store_code if event.store is not None else ""
    parts = ["·", style_code, event.size_label, store_code, _supplement(event)]
    return " ".join(part for part in parts if part)


def build_message(
    task_name: str, events: list[ChangeEvent], *, base_url: str
) -> tuple[str, str] | None:
    """Aggregate one task's notified events into a single message.

    Format is frozen: the category order is 补货/售罄/变价, each category shows
    at most ``DETAIL_LIMIT`` lines, and when a category keeps back rows the
    footer counts them and links to the changes page. A body longer than
    ``BODY_LIMIT`` characters is truncated whole-line by whole-line from the
    tail, never losing the ``查看：`` link. No events means no message.
    """
    if not events:
        return None
    title = f"【库存提醒】{task_name[:40]}"
    changes_url = f"{base_url}/changes"
    entries: list[str] = []
    for event_type, label in _CATEGORIES:
        rows = [event for event in events if event.event_type == event_type]
        if not rows:
            continue
        entries.append(f"{label} {len(rows)} 条")
        entries.extend(_detail_line(event) for event in rows[:DETAIL_LIMIT])
    total = len(events)

    def render(kept: list[str]) -> str:
        shown = sum(1 for line in kept if line.startswith("· "))
        footer = (
            f"另有 {total - shown} 条，查看：{changes_url}"
            if shown < total
            else f"查看：{changes_url}"
        )
        return "\n".join([*kept, footer])

    while entries and len(render(entries)) > BODY_LIMIT:
        entries.pop()
    return title, render(entries)


def _dingtalk_app_target(url: str) -> tuple[str, str, str, str, bool] | None:
    """读 ``dingtalkapp://robot?secret=…&conversation=…`` 或 ``…&user=…&dm=1``。

    返回 ``(robot_code, secret, conversation_id, user_id, is_dm)``；凭证不全返回 None。
    """
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    robot_code = (parts.hostname or "").strip()
    secret = (query.get("secret") or [""])[0].strip()
    conversation = (query.get("conversation") or [""])[0].strip()
    user_id = (query.get("user") or [""])[0].strip()
    is_dm = (query.get("dm") or [""])[0] == "1"
    if not (robot_code and secret):
        return None
    if not conversation and not user_id:
        return None
    return robot_code, secret, conversation, user_id, is_dm


def build_bot_target(
    robot_code: str, robot_secret: str, *, conversation_id: str = "", user_id: str = ""
) -> str:
    """平台机器人在一个已绑定会话里的推送目标：群会话或单聊（二选一）。"""
    base = f"dingtalkapp://{quote(robot_code, safe='')}?secret={quote(robot_secret, safe='')}"
    if conversation_id:
        return f"{base}&conversation={quote(conversation_id, safe='')}"
    return f"{base}&user={quote(user_id, safe='')}&dm=1"


def _send_dingtalk_app(
    target: str, title: str, body: str, settings: Settings
) -> tuple[bool, str | None]:
    """平台机器人：换 accessToken 后按会话类型调群消息/单聊接口（不走 Apprise）。"""
    parsed = _dingtalk_app_target(target)
    if parsed is None:
        return False, PUSH_TARGET_INVALID
    robot_code, robot_secret, conversation_id, user_id, is_dm = parsed
    timeout = settings.push_timeout_seconds or DEFAULT_TIMEOUT_SECONDS
    try:
        token_response = httpx.post(
            DINGTALK_TOKEN_URL,
            json={"appKey": robot_code, "appSecret": robot_secret},
            timeout=timeout,
        )
        token_response.raise_for_status()
        access_token = str(token_response.json().get("accessToken") or "")
    except Exception:  # noqa: BLE001 - 网络/凭证问题都按投递失败处理
        logger.warning("dingtalk bot token request failed", exc_info=True)
        return False, PUSH_DELIVERY_FAILED
    if not access_token:
        logger.warning("dingtalk bot token response carried no accessToken")
        return False, PUSH_DELIVERY_FAILED
    payload: dict[str, object] = {
        "robotCode": robot_code,
        "msgKey": "sampleMarkdown",
        "msgParam": json.dumps({"title": title, "text": body}, ensure_ascii=False),
    }
    if is_dm:
        payload["userIds"] = [user_id]
        send_url = DINGTALK_DM_SEND_URL
    else:
        payload["openConversationId"] = conversation_id
        send_url = DINGTALK_GROUP_SEND_URL
    try:
        response = httpx.post(
            send_url,
            headers={"x-acs-dingtalk-access-token": access_token},
            json=payload,
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001
        logger.warning("dingtalk bot send raised", exc_info=True)
        return False, PUSH_DELIVERY_FAILED
    if response.status_code >= 400:
        logger.warning("dingtalk bot send rejected: HTTP %s", response.status_code)
        return False, PUSH_DELIVERY_FAILED
    return True, None


def send_now(
    target: str, title: str, body: str, settings: Settings
) -> tuple[bool, str | None]:
    """Send one message now; returns ``(ok, error_code)`` with fixed codes."""
    if urlsplit(target).scheme.lower() == "dingtalkapp":
        return _send_dingtalk_app(target, title, body, settings)
    apobj = apprise.Apprise()
    if not apobj.add(target):
        return False, PUSH_TARGET_INVALID
    try:
        result = apobj.notify(
            title=title,
            body=body,
            body_format=apprise.NotifyFormat.MARKDOWN,
            timeout=settings.push_timeout_seconds or DEFAULT_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 - a push must never crash its caller
        logger.warning("push send raised", exc_info=True)
        return False, PUSH_DELIVERY_FAILED
    if result:
        return True, None
    return False, PUSH_DELIVERY_FAILED


def _notified_types(task: MonitorTask) -> list[str]:
    return [
        event_type
        for event_type, enabled in (
            (EventType.RESTOCK.value, task.notify_on_restock),
            (EventType.SOLD_OUT.value, task.notify_on_sold_out),
            (EventType.PRICE_CHANGE.value, task.notify_on_price_change),
        )
        if enabled
    ]


def enqueue_for_job(session: Session, settings: Settings, job: ScanJob) -> int:
    """Queue one aggregated delivery per enabled channel for a finished job.

    Returns the number of rows created; the caller owns the transaction.
    """
    if not settings.push_enabled or job.task_id is None or job.owner_user_id is None:
        return 0
    task = session.get(MonitorTask, job.task_id)
    if task is None:
        return 0
    types = _notified_types(task)
    if not types:
        return 0
    events = (
        session.execute(
            select(ChangeEvent)
            .where(ChangeEvent.job_id == job.id, ChangeEvent.event_type.in_(types))
            .order_by(ChangeEvent.detected_at, ChangeEvent.id)
        )
        .scalars()
        .all()
    )
    if not events:
        return 0
    channels = (
        session.execute(
            select(NotificationChannel).where(
                NotificationChannel.owner_user_id == job.owner_user_id,
                NotificationChannel.enabled.is_(True),
            )
        )
        .scalars()
        .all()
    )
    if not channels:
        return 0
    since = datetime.now(UTC) - timedelta(days=1)
    recent = session.execute(
        select(func.count())
        .select_from(NotificationDelivery)
        .where(
            NotificationDelivery.owner_user_id == job.owner_user_id,
            NotificationDelivery.created_at >= since,
        )
    ).scalar_one()
    if recent >= settings.push_max_per_day:
        logger.warning(
            "push daily cap reached for owner %s; job %s queues nothing",
            job.owner_user_id,
            job.id,
        )
        return 0
    message = build_message(task.name, list(events), base_url=settings.public_base_url)
    if message is None:
        return 0
    title, body = message
    queued = set(
        session.execute(
            select(NotificationDelivery.channel_id).where(NotificationDelivery.job_id == job.id)
        )
        .scalars()
        .all()
    )
    created = 0
    for channel in channels:
        if channel.id in queued:
            continue
        session.add(
            NotificationDelivery(
                owner_user_id=job.owner_user_id,
                channel_id=channel.id,
                job_id=job.id,
                task_id=job.task_id,
                title=title,
                body=body,
                state=NotificationDeliveryState.PENDING.value,
            )
        )
        created += 1
    if created:
        session.flush()
    return created


def _backoff_seconds(settings: Settings) -> list[int]:
    try:
        values = [int(part) for part in settings.push_backoff_seconds.split(",") if part.strip()]
    except ValueError:
        return [60]
    return values or [60]


def lease_due_delivery(session: Session, settings: Settings) -> NotificationDelivery | None:
    """Claim the oldest due delivery (or a delivery whose lease expired)."""
    now = datetime.now(UTC)
    delivery = session.execute(
        select(NotificationDelivery)
        .where(
            or_(
                and_(
                    NotificationDelivery.state == NotificationDeliveryState.PENDING.value,
                    NotificationDelivery.next_attempt_at <= now,
                ),
                and_(
                    NotificationDelivery.state == DELIVERY_RUNNING,
                    NotificationDelivery.lease_until < now,
                ),
            )
        )
        .order_by(NotificationDelivery.next_attempt_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    ).scalar_one_or_none()
    if delivery is None:
        return None
    delivery.state = DELIVERY_RUNNING
    delivery.lease_until = now + timedelta(seconds=DELIVERY_LEASE_SECONDS)
    session.flush()
    return delivery


def deliver(session: Session, settings: Settings, delivery: NotificationDelivery) -> None:
    """Send one leased delivery and record the outcome; commits the row."""
    channel = session.get(NotificationChannel, delivery.channel_id)
    if channel is None:
        return
    now = datetime.now(UTC)
    try:
        target = decrypt_secret(channel.target_enc)
    except Exception:  # noqa: BLE001 - an unreadable secret is a failed delivery
        logger.warning("push channel %s cannot be decrypted", channel.id, exc_info=True)
        ok, error_code = False, PUSH_DELIVERY_FAILED
    else:
        ok, error_code = send_now(target, delivery.title, delivery.body, settings)
    delivery.lease_until = None
    if ok:
        delivery.state = NotificationDeliveryState.SENT.value
        delivery.sent_at = now
        delivery.last_error_code = None
        delivery.last_error = None
        channel.last_ok_at = now
        channel.last_error_code = None
        channel.last_error = None
        channel.last_error_at = None
        session.commit()
        return
    delivery.attempts += 1
    backoff = _backoff_seconds(settings)
    if delivery.attempts > len(backoff):
        delivery.state = NotificationDeliveryState.FAILED.value
    else:
        delivery.state = NotificationDeliveryState.PENDING.value
        delivery.next_attempt_at = now + timedelta(seconds=backoff[delivery.attempts - 1])
    delivery.last_error_code = error_code
    delivery.last_error = DELIVERY_FAILED_TEXT
    channel.last_error_code = error_code
    channel.last_error = DELIVERY_FAILED_TEXT
    channel.last_error_at = now
    session.commit()


def retry_delivery(session: Session, settings: Settings, delivery: NotificationDelivery) -> None:
    """Reset a delivery so the worker picks it up immediately."""
    delivery.state = NotificationDeliveryState.PENDING.value
    delivery.attempts = 0
    delivery.next_attempt_at = datetime.now(UTC)
    delivery.lease_until = None
    delivery.last_error_code = None
    delivery.last_error = None
