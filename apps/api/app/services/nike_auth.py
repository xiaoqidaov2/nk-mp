"""Self-service Nike phone binding: sessions, budgets and atomic installation.

Fixed platform lock order: owner user row -> credential row -> auth attempt /
auth limit rows. Every state change commits its own short transaction; the
upstream calls (SMS send, code verify, token exchange) happen outside any
lock and are never replayed automatically.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import Request
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import SessionLocal
from app.models import (
    ACTIVE_AUTH_STATES,
    AuthSession,
    Credential,
    CredentialStatus,
    NikeAuthAttempt,
    NikeAuthLimit,
    NikeAuthState,
    User,
)
from app.nike.phone_auth import (
    SUPPORTED_CONTRACT_VERSION,
    NikePhoneAuthClient,
    PhoneAuthChallenge,
    PhoneAuthError,
    PhoneAuthTokens,
    mask_phone,
)
from app.security import (
    credential_key_available,
    decrypt_secret,
    encrypt_secret,
    secret_fingerprint,
)
from app.services.credentials import CredentialService, apply_tokens_locked

logger = logging.getLogger("nike_mp.nike_auth")

VERIFY_WINDOW = timedelta(minutes=15)
TERMINAL_ATTEMPT_RETENTION = timedelta(hours=24)
PHONE_RE = re.compile(r"^\+861[3-9]\d{9}$")

NOT_AVAILABLE_CODE = "NIKE_AUTH_NOT_AVAILABLE"
NOT_CONNECTED = "NOT_CONNECTED"


class NikeAuthError(RuntimeError):
    """Stable service-level error mapped to the JSON error contract."""

    def __init__(
        self,
        code: str,
        detail: str,
        status_code: int,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


@dataclass
class AttemptView:
    id: UUID
    state: str
    phone_masked: str
    expires_at: datetime | None
    resend_after: datetime | None
    attempts_remaining: int
    error_code: str | None


def login_available(settings: Settings) -> bool:
    return bool(
        settings.nike_phone_auth_enabled
        and settings.nike_phone_auth_contract_version == SUPPORTED_CONTRACT_VERSION
        and credential_key_available()
    )


def normalize_phone(raw: str) -> str:
    cleaned = re.sub(r"[\s-]", "", raw or "")
    if re.fullmatch(r"1[3-9]\d{9}", cleaned):
        cleaned = "+86" + cleaned
    if not PHONE_RE.fullmatch(cleaned):
        raise NikeAuthError("NIKE_AUTH_REQUEST_CONFLICT", "手机号格式不正确", 422)
    return cleaned


def client_ip_for(request: Request, settings: Settings) -> str:
    """Socket peer only; X-Forwarded-For is honoured solely behind trusted proxies.

    The socket peer is not attacker-controlled: an unparseable value (for
    example a unix-socket peer) is fingerprinted as-is instead of failing the
    request. Malformed ``X-Forwarded-For`` entries are rejected with 400
    because that header is client-controlled.
    """
    peer_raw = request.client.host if request.client else ""
    networks = []
    for cidr in settings.nike_sms_trusted_proxy_cidrs:
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            continue
    peer_ip = _try_parse_ip(peer_raw)
    if peer_ip is None:
        return peer_raw or "unknown"
    if networks and any(peer_ip in net for net in networks):
        forwarded = request.headers.get("x-forwarded-for") or ""
        chain = [_parse_ip(item) for item in forwarded.split(",") if item.strip()]
        for candidate in reversed(chain):
            if not any(candidate in net for net in networks):
                return str(candidate)
    return str(peer_ip)


def _try_parse_ip(value: str):
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError:
        return None


def _parse_ip(value: str):
    parsed = _try_parse_ip(value)
    if parsed is None:
        raise NikeAuthError("NIKE_AUTH_REQUEST_CONFLICT", "来源地址格式异常", 400)
    return parsed


def credential_view(settings: Settings, user_id: UUID) -> dict:
    info = CredentialService(settings, user_id).status()
    if info is None or info.status == CredentialStatus.UNASSIGNED.value:
        return {
            "status": NOT_CONNECTED,
            "phone_masked": None,
            "access_expires_at": None,
            "last_refreshed_at": None,
            "last_error_code": None,
            "binding_generation": None,
        }
    return {
        "status": info.status,
        "phone_masked": info.phone_masked,
        "access_expires_at": info.access_expires_at,
        "last_refreshed_at": info.last_refreshed_at,
        "last_error_code": info.last_error_code,
        "binding_generation": info.binding_generation,
    }


# --------------------------------------------------------------------- create


def create_attempt(
    *,
    settings: Settings,
    user_id: UUID,
    auth_session_id: UUID,
    phone: str,
    request_id: UUID,
    client_ip: str,
) -> tuple[AttemptView, bool]:
    """Create (and start) a send attempt. Returns ``(view, created)``."""
    phone_e164 = normalize_phone(phone)
    phone_hash = secret_fingerprint(phone_e164, purpose="phone")
    ip_hash = secret_fingerprint(client_ip, purpose="ip") if client_ip else ""
    now = datetime.now(UTC)

    session = SessionLocal()
    try:
        _advisory_locks(session, user_id, phone_hash, ip_hash)
        _expire_for_owner(session, user_id, now)

        conflict = session.execute(
            select(Credential).where(
                Credential.phone_hash == phone_hash,
                Credential.owner_user_id != user_id,
                Credential.status != CredentialStatus.REVOKED.value,
            )
        ).scalar_one_or_none()
        if conflict is not None:
            raise NikeAuthError("NIKE_PHONE_CONNECT_UNAVAILABLE", "该手机号已绑定其他平台账号", 409)

        existing = session.execute(
            select(NikeAuthAttempt).where(
                NikeAuthAttempt.owner_user_id == user_id,
                NikeAuthAttempt.client_request_id == request_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.phone_hash != phone_hash:
                raise NikeAuthError("NIKE_AUTH_REQUEST_CONFLICT", "请求编号与手机号不匹配", 409)
            if existing.auth_session_id != auth_session_id:
                raise NikeAuthError("NIKE_AUTH_SESSION_MISMATCH", "请使用发起请求的登录会话", 409)
            view = _attempt_view(session, existing, settings, now)
            session.commit()
            return view, False

        active = session.execute(
            select(NikeAuthAttempt)
            .where(
                NikeAuthAttempt.owner_user_id == user_id,
                NikeAuthAttempt.state.in_(ACTIVE_AUTH_STATES),
            )
            .with_for_update()
        ).scalar_one_or_none()
        if active is not None:
            if active.state in (
                NikeAuthState.SENDING.value,
                NikeAuthState.VERIFYING.value,
            ):
                raise NikeAuthError("NIKE_AUTH_IN_PROGRESS", "已有请求正在处理，请稍候", 409)
            if active.resend_after is not None and active.resend_after > now:
                retry = int((active.resend_after - now).total_seconds()) + 1
                raise NikeAuthError("NIKE_AUTH_RATE_LIMITED", "发送过于频繁，请稍后再试", 429, retry)
            _terminate(active, NikeAuthState.CANCELLED, now)

        phone_busy = session.execute(
            select(NikeAuthAttempt).where(
                NikeAuthAttempt.phone_hash == phone_hash,
                NikeAuthAttempt.owner_user_id != user_id,
                NikeAuthAttempt.state.in_(ACTIVE_AUTH_STATES),
            )
        ).scalar_one_or_none()
        if phone_busy is not None:
            raise NikeAuthError("NIKE_PHONE_CONNECT_UNAVAILABLE", "该手机号正在进行绑定验证", 409)

        _check_send_limits(session, settings, user_id, phone_hash, ip_hash, now)

        record = session.execute(
            select(Credential).where(Credential.owner_user_id == user_id)
        ).scalar_one_or_none()
        attempt = NikeAuthAttempt(
            owner_user_id=user_id,
            auth_session_id=auth_session_id,
            client_request_id=request_id,
            state=NikeAuthState.SENDING.value,
            phone_enc=encrypt_secret(phone_e164),
            phone_masked=mask_phone(phone_e164),
            phone_hash=phone_hash,
            ip_hash=ip_hash,
            base_credential_id=record.id if record else None,
            base_generation=record.binding_generation if record else 0,
            operation_started_at=now,
            operation_deadline_at=now
            + timedelta(seconds=settings.nike_sms_operation_timeout_seconds),
            expires_at=now + timedelta(seconds=settings.nike_sms_session_ttl_seconds),
        )
        session.add(attempt)
        session.flush()
        attempt_id = attempt.id
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        message = str(getattr(exc, "orig", exc))
        if "uq_nike_auth_attempt_phone_active" in message:
            raise NikeAuthError(
                "NIKE_PHONE_CONNECT_UNAVAILABLE", "该手机号正在进行绑定验证", 409
            ) from exc
        raise NikeAuthError("NIKE_AUTH_IN_PROGRESS", "已有请求正在处理，请稍候", 409) from exc
    finally:
        session.close()

    try:
        with NikePhoneAuthClient(settings) as client:
            challenge = client.start(phone_e164)
    except PhoneAuthError as exc:
        _finish_send_failure(attempt_id, _error_code(exc))
        raise _map_phone_error(exc) from exc
    except Exception as exc:  # noqa: BLE001 - unknown send outcome, never retried
        _finish_send_failure(attempt_id, "NIKE_SMS_SEND_UNCERTAIN")
        raise NikeAuthError(
            "NIKE_SMS_SEND_UNCERTAIN", "短信发送结果不确定，请稍后重试", 503
        ) from exc

    session = SessionLocal()
    try:
        row = session.execute(
            select(NikeAuthAttempt).where(NikeAuthAttempt.id == attempt_id).with_for_update()
        ).scalar_one()
        if (
            row.state == NikeAuthState.SENDING.value
            and row.auth_session_id == auth_session_id
            and (row.operation_deadline_at is None or row.operation_deadline_at >= datetime.now(UTC))
            and _base_binding_unchanged(session, row)
        ):
            row.provider_context_enc = encrypt_secret(json.dumps(challenge.to_context()))
            row.phone_masked = challenge.phone_masked
            row.state = NikeAuthState.CODE_SENT.value
            row.resend_after = datetime.now(UTC) + timedelta(
                seconds=settings.nike_sms_cooldown_seconds
            )
            row.last_error_code = None
        else:
            # The user cancelled or re-bound while the SMS was in flight:
            # discard the session material instead of resurrecting the attempt.
            row.provider_context_enc = ""
            row.phone_enc = ""
        row.updated_at = datetime.now(UTC)
        session.commit()
        view = _attempt_view(session, row, settings, datetime.now(UTC))
    finally:
        session.close()
    return view, True


def _finish_send_failure(attempt_id: UUID, code: str) -> None:
    session = SessionLocal()
    try:
        row = session.execute(
            select(NikeAuthAttempt).where(NikeAuthAttempt.id == attempt_id).with_for_update()
        ).scalar_one_or_none()
        if row is not None and row.state == NikeAuthState.SENDING.value:
            _terminate(row, NikeAuthState.FAILED, datetime.now(UTC))
            row.last_error_code = code
        session.commit()
    finally:
        session.close()


# --------------------------------------------------------------------- verify


def get_attempt(
    *, settings: Settings, user_id: UUID, auth_session_id: UUID, attempt_id: UUID
) -> AttemptView:
    now = datetime.now(UTC)
    session = SessionLocal()
    try:
        row = session.execute(
            select(NikeAuthAttempt).where(NikeAuthAttempt.id == attempt_id)
        ).scalar_one_or_none()
        if row is None or row.owner_user_id != user_id or row.auth_session_id != auth_session_id:
            raise NikeAuthError("NIKE_AUTH_SESSION_MISMATCH", "会话不存在", 404)
        if _expire_row(row, now):
            session.commit()
        return _attempt_view(session, row, settings, now)
    finally:
        session.close()


def cancel_attempt(
    *, user_id: UUID, auth_session_id: UUID, attempt_id: UUID
) -> None:
    now = datetime.now(UTC)
    session = SessionLocal()
    try:
        _lock_owner_row(session, user_id)
        row = session.execute(
            select(NikeAuthAttempt).where(NikeAuthAttempt.id == attempt_id).with_for_update()
        ).scalar_one_or_none()
        if row is None or row.owner_user_id != user_id or row.auth_session_id != auth_session_id:
            raise NikeAuthError("NIKE_AUTH_SESSION_MISMATCH", "会话不存在", 404)
        if row.state in ACTIVE_AUTH_STATES:
            _terminate(row, NikeAuthState.CANCELLED, now)
        session.commit()
    finally:
        session.close()


def verify_attempt(
    *,
    settings: Settings,
    user_id: UUID,
    auth_session_id: UUID,
    attempt_id: UUID,
    code: str,
) -> dict:
    now = datetime.now(UTC)
    session = SessionLocal()
    try:
        _lock_owner_row(session, user_id)
        row = session.execute(
            select(NikeAuthAttempt).where(NikeAuthAttempt.id == attempt_id).with_for_update()
        ).scalar_one_or_none()
        if row is None or row.owner_user_id != user_id or row.auth_session_id != auth_session_id:
            raise NikeAuthError("NIKE_AUTH_SESSION_MISMATCH", "会话不存在", 404)
        _expire_row(row, now)

        if row.state == NikeAuthState.CONNECTED.value:
            record = session.execute(
                select(Credential).where(Credential.owner_user_id == user_id)
            ).scalar_one_or_none()
            if (
                record is not None
                and record.status != CredentialStatus.REVOKED.value
                and row.result_credential_id == record.id
                and row.result_generation == record.binding_generation
            ):
                return credential_view(settings, user_id)
            raise NikeAuthError("NIKE_CREDENTIAL_CHANGED", "绑定已变更，请刷新页面", 409)
        if row.state == NikeAuthState.VERIFYING.value:
            raise NikeAuthError("NIKE_AUTH_IN_PROGRESS", "验证码校验处理中", 409)
        if row.state != NikeAuthState.CODE_SENT.value:
            raise NikeAuthError("NIKE_CODE_EXPIRED", "验证码已失效，请重新获取", 410)
        if row.expires_at is not None and row.expires_at <= now:
            _terminate(row, NikeAuthState.EXPIRED, now)
            session.commit()
            raise NikeAuthError("NIKE_CODE_EXPIRED", "验证码已过期，请重新获取", 410)

        limit = session.execute(
            select(NikeAuthLimit)
            .where(NikeAuthLimit.owner_user_id == user_id)
            .with_for_update()
        ).scalar_one_or_none()
        if limit is None:
            limit = NikeAuthLimit(owner_user_id=user_id)
            session.add(limit)
            session.flush()
        if limit.verify_locked_until is not None and limit.verify_locked_until > now:
            retry = int((limit.verify_locked_until - now).total_seconds()) + 1
            raise NikeAuthError("NIKE_AUTH_RATE_LIMITED", "验证次数过多，请稍后再试", 429, retry)
        if (
            limit.verify_window_started_at is None
            or (now - limit.verify_window_started_at) >= VERIFY_WINDOW
        ):
            limit.verify_window_started_at = now
            limit.verify_attempts_in_window = 0
        if limit.verify_attempts_in_window >= settings.nike_sms_verify_limit:
            limit.verify_locked_until = now + timedelta(seconds=settings.nike_sms_lock_seconds)
            session.commit()
            raise NikeAuthError(
                "NIKE_AUTH_RATE_LIMITED", "验证次数过多，请稍后再试", 429,
                settings.nike_sms_lock_seconds,
            )
        limit.verify_attempts_in_window += 1
        row.verify_attempts += 1
        row.state = NikeAuthState.VERIFYING.value
        row.operation_started_at = now
        row.operation_deadline_at = now + timedelta(
            seconds=settings.nike_sms_operation_timeout_seconds
        )
        row.updated_at = now
        context = PhoneAuthChallenge.from_context(
            json.loads(decrypt_secret(row.provider_context_enc) or "{}")
        )
        session.commit()
    finally:
        session.close()

    try:
        with NikePhoneAuthClient(settings) as client:
            tokens = client.verify(context, code)
    except PhoneAuthError as exc:
        locked = _finish_verify_failure(settings, attempt_id, _error_code(exc))
        if locked:
            raise NikeAuthError(
                "NIKE_AUTH_RATE_LIMITED", "验证次数过多，请稍后再试", 429, locked
            ) from exc
        raise _map_verify_error(exc) from exc
    except Exception as exc:  # noqa: BLE001 - unknown verify outcome, never retried
        locked = _finish_verify_failure(settings, attempt_id, "NIKE_VERIFY_UNCERTAIN")
        if locked:
            raise NikeAuthError(
                "NIKE_AUTH_RATE_LIMITED", "验证次数过多，请稍后再试", 429, locked
            ) from exc
        raise NikeAuthError(
            "NIKE_VERIFY_UNCERTAIN", "验证码校验结果不确定，请重新获取验证码", 503
        ) from exc

    return _install_tokens(settings, attempt_id, user_id, auth_session_id, tokens)


def _finish_verify_failure(settings: Settings, attempt_id: UUID, code: str) -> int | None:
    """Record a failed verification. Returns lock seconds when the budget ran out."""
    now = datetime.now(UTC)
    locked: int | None = None
    session = SessionLocal()
    try:
        row = session.execute(
            select(NikeAuthAttempt).where(NikeAuthAttempt.id == attempt_id).with_for_update()
        ).scalar_one_or_none()
        if row is None or row.state != NikeAuthState.VERIFYING.value:
            return None
        if code == "NIKE_CODE_INVALID":
            if row.expires_at is not None and row.expires_at <= now:
                _terminate(row, NikeAuthState.EXPIRED, now)
            else:
                row.state = NikeAuthState.CODE_SENT.value
            row.last_error_code = code
        elif code == "NIKE_CODE_EXPIRED":
            _terminate(row, NikeAuthState.EXPIRED, now)
            row.last_error_code = code
        elif code == "NIKE_CHALLENGE_REQUIRED":
            _terminate(row, NikeAuthState.CHALLENGE_REQUIRED, now)
            row.last_error_code = code
        else:
            # Uncertain / rate limited / contract change: the single-use code
            # may already be consumed, so the attempt cannot continue.
            _terminate(row, NikeAuthState.FAILED, now)
            row.last_error_code = code
        row.updated_at = now

        if code in ("NIKE_CODE_INVALID", "NIKE_VERIFY_UNCERTAIN"):
            limit = session.execute(
                select(NikeAuthLimit)
                .where(NikeAuthLimit.owner_user_id == row.owner_user_id)
                .with_for_update()
            ).scalar_one_or_none()
            if (
                limit is not None
                and limit.verify_window_started_at is not None
                and (now - limit.verify_window_started_at) < VERIFY_WINDOW
                and limit.verify_attempts_in_window >= settings.nike_sms_verify_limit
            ):
                limit.verify_locked_until = now + timedelta(
                    seconds=settings.nike_sms_lock_seconds
                )
                locked = settings.nike_sms_lock_seconds
        session.commit()
    finally:
        session.close()
    return locked


def _install_tokens(
    settings: Settings,
    attempt_id: UUID,
    user_id: UUID,
    auth_session_id: UUID,
    tokens: PhoneAuthTokens,
) -> dict:
    now = datetime.now(UTC)
    session = SessionLocal()
    try:
        _lock_owner_row(session, user_id)
        row = session.execute(
            select(NikeAuthAttempt).where(NikeAuthAttempt.id == attempt_id).with_for_update()
        ).scalar_one_or_none()
        auth = session.execute(
            select(AuthSession).where(AuthSession.id == auth_session_id)
        ).scalar_one_or_none()
        record = session.execute(
            select(Credential).where(Credential.owner_user_id == user_id).with_for_update()
        ).scalar_one_or_none()

        valid = (
            row is not None
            and row.owner_user_id == user_id
            and row.auth_session_id == auth_session_id
            and row.state == NikeAuthState.VERIFYING.value
            and (row.operation_deadline_at is None or row.operation_deadline_at >= now)
            and auth is not None
            and auth.revoked_at is None
            and row.base_credential_id == (record.id if record else None)
            and row.base_generation == (record.binding_generation if record else 0)
        )
        if not valid:
            if row is not None and row.state == NikeAuthState.VERIFYING.value:
                _terminate(row, NikeAuthState.FAILED, now)
                row.last_error_code = "NIKE_VERIFY_UNCERTAIN"
                session.commit()
            raise NikeAuthError("NIKE_CREDENTIAL_CHANGED", "请求已失效，请重新发起", 409)

        assert row is not None  # validated above
        conflict = session.execute(
            select(Credential).where(
                Credential.phone_hash == row.phone_hash,
                Credential.owner_user_id != user_id,
                Credential.status != CredentialStatus.REVOKED.value,
            )
        ).scalar_one_or_none()
        if conflict is not None:
            _terminate(row, NikeAuthState.FAILED, now)
            row.last_error_code = "NIKE_PHONE_CONNECT_UNAVAILABLE"
            session.commit()
            raise NikeAuthError("NIKE_PHONE_CONNECT_UNAVAILABLE", "该手机号已绑定其他平台账号", 409)

        if record is None:
            record = Credential(
                owner_user_id=user_id,
                binding_generation=1,
                status=CredentialStatus.ACTIVE.value,
            )
            session.add(record)
            session.flush()
        else:
            record.binding_generation += 1
        apply_tokens_locked(
            record,
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            client_id=tokens.client_id,
        )
        record.phone_masked = row.phone_masked
        record.phone_hash = row.phone_hash
        row.state = NikeAuthState.CONNECTED.value
        row.result_credential_id = record.id
        row.result_generation = record.binding_generation
        row.phone_enc = ""
        row.provider_context_enc = ""
        row.last_error_code = None
        row.updated_at = now

        limit = session.execute(
            select(NikeAuthLimit)
            .where(NikeAuthLimit.owner_user_id == user_id)
            .with_for_update()
        ).scalar_one_or_none()
        if limit is not None:
            limit.verify_attempts_in_window = 0
            limit.verify_window_started_at = None
            limit.verify_locked_until = None
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        _finish_verify_failure(settings, attempt_id, "NIKE_PHONE_CONNECT_UNAVAILABLE")
        raise NikeAuthError("NIKE_PHONE_CONNECT_UNAVAILABLE", "该手机号已绑定其他平台账号", 409) from exc
    finally:
        session.close()
    return credential_view(settings, user_id)


# ----------------------------------------------------------------- lifecycle


def disconnect(*, settings: Settings, user_id: UUID) -> None:
    """Local disconnect: revoke the binding and cancel every pending attempt."""
    now = datetime.now(UTC)
    session = SessionLocal()
    try:
        service = CredentialService(settings, user_id)
        service.lock_for_update(session)
        service.revoke_local(session)
        rows = session.execute(
            select(NikeAuthAttempt).where(
                NikeAuthAttempt.owner_user_id == user_id,
                NikeAuthAttempt.state.in_(ACTIVE_AUTH_STATES),
            )
        ).scalars().all()
        for row in rows:
            _terminate(row, NikeAuthState.CANCELLED, now)
        session.commit()
    finally:
        session.close()


def cancel_for_auth_session(*, user_id: UUID, auth_session_id: UUID) -> int:
    """Logout hook: cancel this platform session's unfinished attempts."""
    now = datetime.now(UTC)
    session = SessionLocal()
    try:
        _lock_owner_row(session, user_id)
        rows = session.execute(
            select(NikeAuthAttempt).where(
                NikeAuthAttempt.owner_user_id == user_id,
                NikeAuthAttempt.auth_session_id == auth_session_id,
                NikeAuthAttempt.state.in_(ACTIVE_AUTH_STATES),
            )
        ).scalars().all()
        for row in rows:
            _terminate(row, NikeAuthState.CANCELLED, now)
        session.commit()
        return len(rows)
    finally:
        session.close()


def expire_attempts(now: datetime | None = None) -> int:
    """Scheduler tick: fail timed-out operations and expire stale codes."""
    moment = now or datetime.now(UTC)
    session = SessionLocal()
    try:
        rows = session.execute(
            select(NikeAuthAttempt).where(NikeAuthAttempt.state.in_(ACTIVE_AUTH_STATES))
        ).scalars().all()
        changed = sum(1 for row in rows if _expire_row(row, moment))
        session.commit()
        return changed
    finally:
        session.close()


def cleanup_old_attempts(now: datetime | None = None) -> int:
    """Delete terminal attempts older than the retention window.

    The auth limit table is never cleaned: an active lock must survive.
    """
    moment = now or datetime.now(UTC)
    cutoff = moment - TERMINAL_ATTEMPT_RETENTION
    session = SessionLocal()
    try:
        result = session.execute(
            delete(NikeAuthAttempt).where(
                NikeAuthAttempt.state.notin_(ACTIVE_AUTH_STATES),
                NikeAuthAttempt.created_at < cutoff,
            )
        )
        session.commit()
        return int(result.rowcount or 0)
    finally:
        session.close()


# ----------------------------------------------------------------- internals


def _terminate(row: NikeAuthAttempt, state: NikeAuthState, now: datetime) -> None:
    row.state = state.value
    row.phone_enc = ""
    row.provider_context_enc = ""
    row.updated_at = now


def _expire_row(row: NikeAuthAttempt, now: datetime) -> bool:
    if row.state in (NikeAuthState.SENDING.value, NikeAuthState.VERIFYING.value):
        if row.operation_deadline_at is not None and row.operation_deadline_at <= now:
            code = (
                "NIKE_SMS_SEND_UNCERTAIN"
                if row.state == NikeAuthState.SENDING.value
                else "NIKE_VERIFY_UNCERTAIN"
            )
            _terminate(row, NikeAuthState.FAILED, now)
            row.last_error_code = code
            return True
    elif (
        row.state == NikeAuthState.CODE_SENT.value
        and row.expires_at is not None
        and row.expires_at <= now
    ):
        _terminate(row, NikeAuthState.EXPIRED, now)
        return True
    return False


def _expire_for_owner(session: Session, user_id: UUID, now: datetime) -> int:
    rows = session.execute(
        select(NikeAuthAttempt).where(
            NikeAuthAttempt.owner_user_id == user_id,
            NikeAuthAttempt.state.in_(ACTIVE_AUTH_STATES),
        )
    ).scalars().all()
    return sum(1 for row in rows if _expire_row(row, now))


def _lock_owner_row(session: Session, user_id: UUID) -> User:
    user = session.execute(
        select(User).where(User.id == user_id).with_for_update()
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        raise NikeAuthError("NIKE_AUTH_SESSION_MISMATCH", "账号不可用", 401)
    return user


def _advisory_locks(session: Session, user_id: UUID, phone_hash: str, ip_hash: str) -> None:
    keys = {f"nike-auth:u:{user_id}", f"nike-auth:p:{phone_hash}"}
    if ip_hash:
        keys.add(f"nike-auth:i:{ip_hash}")
    for key in sorted(keys):
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})


def _base_binding_unchanged(session: Session, row: NikeAuthAttempt) -> bool:
    record = session.execute(
        select(Credential).where(Credential.owner_user_id == row.owner_user_id)
    ).scalar_one_or_none()
    return row.base_credential_id == (record.id if record else None) and row.base_generation == (
        record.binding_generation if record else 0
    )


def _attempt_view(
    session: Session, row: NikeAuthAttempt, settings: Settings, now: datetime
) -> AttemptView:
    remaining = settings.nike_sms_verify_limit
    limit = session.execute(
        select(NikeAuthLimit).where(NikeAuthLimit.owner_user_id == row.owner_user_id)
    ).scalar_one_or_none()
    if limit is not None:
        if limit.verify_locked_until is not None and limit.verify_locked_until > now:
            remaining = 0
        elif (
            limit.verify_window_started_at is not None
            and (now - limit.verify_window_started_at) < VERIFY_WINDOW
        ):
            remaining = max(0, settings.nike_sms_verify_limit - limit.verify_attempts_in_window)
    return AttemptView(
        id=row.id,
        state=row.state,
        phone_masked=row.phone_masked,
        expires_at=row.expires_at,
        resend_after=row.resend_after,
        attempts_remaining=remaining,
        error_code=row.last_error_code,
    )


def _check_send_limits(
    session: Session,
    settings: Settings,
    user_id: UUID,
    phone_hash: str,
    ip_hash: str,
    now: datetime,
) -> None:
    hour_ago = now - timedelta(hours=1)
    day_ago = now - timedelta(hours=24)

    def count(*conditions) -> int:
        return int(
            session.execute(
                select(func.count()).select_from(NikeAuthAttempt).where(*conditions)
            ).scalar_one()
        )

    def retry_after(*conditions, window_seconds: int) -> int:
        oldest = session.execute(
            select(func.min(NikeAuthAttempt.created_at)).where(*conditions)
        ).scalar_one()
        if oldest is None:
            return 1
        return max(1, int((oldest + timedelta(seconds=window_seconds) - now).total_seconds()) + 1)

    if count(
        NikeAuthAttempt.owner_user_id == user_id,
        NikeAuthAttempt.created_at >= hour_ago,
    ) >= settings.nike_sms_user_hour_limit:
        raise NikeAuthError(
            "NIKE_AUTH_RATE_LIMITED", "发送次数过多，请稍后再试", 429,
            retry_after(
                NikeAuthAttempt.owner_user_id == user_id,
                NikeAuthAttempt.created_at >= hour_ago,
                window_seconds=3600,
            ),
        )
    if count(
        NikeAuthAttempt.phone_hash == phone_hash,
        NikeAuthAttempt.created_at >= hour_ago,
    ) >= settings.nike_sms_phone_hour_limit:
        raise NikeAuthError(
            "NIKE_AUTH_RATE_LIMITED", "该手机号发送次数过多，请稍后再试", 429,
            retry_after(
                NikeAuthAttempt.phone_hash == phone_hash,
                NikeAuthAttempt.created_at >= hour_ago,
                window_seconds=3600,
            ),
        )
    if count(
        NikeAuthAttempt.phone_hash == phone_hash,
        NikeAuthAttempt.created_at >= day_ago,
    ) >= settings.nike_sms_phone_day_limit:
        raise NikeAuthError(
            "NIKE_AUTH_RATE_LIMITED", "该手机号今日发送次数过多，请明天再试", 429,
            retry_after(
                NikeAuthAttempt.phone_hash == phone_hash,
                NikeAuthAttempt.created_at >= day_ago,
                window_seconds=86400,
            ),
        )
    if ip_hash and count(
        NikeAuthAttempt.ip_hash == ip_hash,
        NikeAuthAttempt.created_at >= hour_ago,
    ) >= settings.nike_sms_ip_hour_limit:
        raise NikeAuthError(
            "NIKE_AUTH_RATE_LIMITED", "请求过于频繁，请稍后再试", 429,
            retry_after(
                NikeAuthAttempt.ip_hash == ip_hash,
                NikeAuthAttempt.created_at >= hour_ago,
                window_seconds=3600,
            ),
        )


def _error_code(exc: PhoneAuthError) -> str:
    return str(getattr(exc, "code", "NIKE_AUTH_CONTRACT_CHANGED"))


def _map_phone_error(exc: PhoneAuthError) -> NikeAuthError:
    code = _error_code(exc)
    retry = getattr(exc, "retry_after_seconds", None)
    if code == "NIKE_SMS_SEND_UNCERTAIN":
        return NikeAuthError(code, "短信发送结果不确定，请稍后重试", 503, retry)
    if code == "NIKE_AUTH_RATE_LIMITED":
        return NikeAuthError(code, "请求过于频繁，请稍后再试", 429, retry)
    if code == "NIKE_AUTH_REQUEST_CONFLICT":
        return NikeAuthError(code, "该手机号没有 Nike 账号，请先注册或更换手机号", 409, retry)
    if code == "NIKE_UPSTREAM_UNAVAILABLE":
        return NikeAuthError(code, "Nike 服务暂不可用，请稍后重试", 503, retry)
    return NikeAuthError(code, "Nike 登录暂不可用，请稍后再试", 503, retry)


def _map_verify_error(exc: PhoneAuthError) -> NikeAuthError:
    code = _error_code(exc)
    retry = getattr(exc, "retry_after_seconds", None)
    mapping = {
        "NIKE_CODE_INVALID": (400, "验证码不正确"),
        "NIKE_CODE_EXPIRED": (410, "验证码已过期，请重新获取"),
        "NIKE_VERIFY_UNCERTAIN": (503, "验证码校验结果不确定，请重新获取验证码"),
        "NIKE_CHALLENGE_REQUIRED": (409, "需要完成额外验证后才能继续"),
        "NIKE_AUTH_CONTRACT_CHANGED": (502, "Nike 登录协议已变化，暂不可用"),
        "NIKE_AUTH_RATE_LIMITED": (429, "请求过于频繁，请稍后再试"),
        "NIKE_SMS_SEND_UNCERTAIN": (503, "验证结果不确定，请重新获取验证码"),
        "NIKE_PHONE_CONNECT_UNAVAILABLE": (409, "该手机号已绑定其他平台账号"),
    }
    status, detail = mapping.get(code, (502, "Nike 登录暂不可用，请稍后再试"))
    return NikeAuthError(code, detail, status, retry)
