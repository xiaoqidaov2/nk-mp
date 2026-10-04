"""Per-user access-token lifecycle with single-flight refresh.

Every read and write opens its own short session: callers never hand in a job
or request session, so a credential rotation can never be rolled back with an
unrelated transaction, and a job transaction can never leak credential state.
The row-lock order is fixed everywhere in the platform:

    owner user row -> credential row -> auth attempt / auth limit rows

Bindings are immutable value objects. Long-running work (a scan job) captures
one binding up front and re-validates it with :meth:`CredentialService.assert_binding`
around every authoritative action; when the user re-binds or disconnects, the
old generation becomes invalid and ongoing work must stop instead of silently
using the new account.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import SessionLocal
from app.models import Credential, CredentialStatus, Tenant, User
from app.security import decrypt_secret, encrypt_secret
from app.services import push

REFRESH_MARGIN_SECONDS = 300

logger = logging.getLogger("nike_mp.credentials")

_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


def _lock_for(owner_user_id: UUID) -> threading.Lock:
    key = str(owner_user_id)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


class CredentialError(RuntimeError):
    """Base class for stable credential errors mapped to NIKE_* codes."""

    code = "NIKE_UPSTREAM_UNAVAILABLE"


class CredentialRequired(CredentialError):
    code = "NIKE_CREDENTIAL_REQUIRED"


class CredentialChanged(CredentialError):
    code = "NIKE_CREDENTIAL_CHANGED"


class CredentialReauthRequired(CredentialError):
    code = "NIKE_REAUTH_REQUIRED"


class CredentialUnavailable(CredentialError):
    code = "NIKE_UPSTREAM_UNAVAILABLE"


@dataclass(frozen=True)
class CredentialBinding:
    owner_user_id: UUID
    credential_id: UUID
    generation: int


@dataclass(frozen=True)
class CredentialStatusInfo:
    status: str
    phone_masked: str | None
    access_expires_at: datetime | None
    last_refreshed_at: datetime | None
    last_error_code: str | None
    binding_generation: int


def _decode_expiry(access_token: str) -> datetime | None:
    parts = access_token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    try:
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        exp = claims.get("exp")
    except Exception:  # noqa: BLE001 - a malformed token is simply "unknown expiry"
        return None
    if not isinstance(exp, (int, float)):
        return None
    return datetime.fromtimestamp(exp, tz=UTC)


def apply_tokens_locked(
    record: Credential,
    *,
    access_token: str,
    refresh_token: str | None,
    client_id: str = "",
) -> None:
    """Write a token pair onto an already-locked row. Never commits."""
    record.access_token_enc = encrypt_secret(access_token)
    if refresh_token:
        record.refresh_token_enc = encrypt_secret(refresh_token)
    if client_id:
        record.client_id = client_id
    record.access_expires_at = _decode_expiry(access_token)
    record.last_refreshed_at = datetime.now(UTC)
    record.status = CredentialStatus.ACTIVE.value
    record.last_error = ""
    record.last_error_code = None
    record.updated_at = datetime.now(UTC)


class CredentialService:
    def __init__(
        self,
        settings: Settings,
        owner_user_id: UUID,
        *,
        session_factory: Callable[[], Session] = SessionLocal,
    ) -> None:
        self.settings = settings
        self.owner_user_id = owner_user_id
        self._session_factory = session_factory

    # ------------------------------------------------------------------ reads

    def binding(self) -> CredentialBinding:
        """Return the owner's current binding, or raise a stable error.

        Confirms local ownership and status only; it never talks to Nike.
        """
        with self._session_factory() as session:
            self._require_active_scope(session)
            record = self._load(session)
            if record is None or record.status == CredentialStatus.REVOKED.value:
                raise CredentialRequired("Nike 账号尚未连接")
            if record.status == CredentialStatus.NEEDS_AUTHORIZATION.value:
                raise CredentialReauthRequired("Nike 账号需要重新验证")
            if (
                record.status == CredentialStatus.UNASSIGNED.value
                or (not record.access_token_enc and not record.refresh_token_enc)
            ):
                raise CredentialRequired("Nike 账号尚未连接")
            return CredentialBinding(
                owner_user_id=record.owner_user_id,
                credential_id=record.id,
                generation=record.binding_generation,
            )

    def assert_binding(self, binding: CredentialBinding) -> None:
        """Cheap guard executed before every authoritative upstream action."""
        with self._session_factory() as session:
            self._require_active_scope(session)
            record = self._load(session)
            if record is None:
                raise CredentialRequired("Nike 账号尚未连接")
            if record.status == CredentialStatus.REVOKED.value:
                raise CredentialChanged("绑定已断开，请重新发起扫描")
            if (
                record.owner_user_id != binding.owner_user_id
                or record.id != binding.credential_id
                or record.binding_generation != binding.generation
            ):
                raise CredentialChanged("绑定已变更，请重新发起扫描")
            if record.status == CredentialStatus.NEEDS_AUTHORIZATION.value:
                raise CredentialReauthRequired("Nike 账号需要重新验证")
            if (
                record.status == CredentialStatus.UNASSIGNED.value
                or (not record.access_token_enc and not record.refresh_token_enc)
            ):
                raise CredentialRequired("Nike 账号尚未连接")

    def status(self) -> CredentialStatusInfo | None:
        with self._session_factory() as session:
            record = self._load(session)
            if record is None:
                return None
            status = record.status
            expires_at = record.access_expires_at
            if status == CredentialStatus.ACTIVE.value and expires_at is not None:
                remaining = (expires_at - datetime.now(UTC)).total_seconds()
                if remaining < 0 and not record.refresh_token_enc:
                    # Only an expired token without a refresh token genuinely
                    # needs the user; a renewable one is renewed automatically
                    # and never reads as "expiring".
                    status = CredentialStatus.NEEDS_AUTHORIZATION.value
            return CredentialStatusInfo(
                status=status,
                phone_masked=record.phone_masked,
                access_expires_at=expires_at,
                last_refreshed_at=record.last_refreshed_at,
                last_error_code=record.last_error_code,
                binding_generation=record.binding_generation,
            )

    # ----------------------------------------------------------------- tokens

    def token_provider(self, binding: CredentialBinding) -> Callable[[str | None], str]:
        """Return a callback suitable for ``NikeClient.token_provider``."""

        def provider(rejected: str | None) -> str:
            return self.access_token(binding=binding, rejected_token=rejected)

        return provider

    def access_token(
        self,
        *,
        binding: CredentialBinding | None = None,
        rejected_token: str | None = None,
    ) -> str:
        """Return a usable access token for this owner.

        ``rejected_token`` is the credential the upstream just rejected with a
        401; when the stored token still matches, a forced rotation is
        attempted. A token that is already expired is never returned.
        """
        with _lock_for(self.owner_user_id), self._session_factory() as session:
            self._lock_owner(session)
            record = self._load(session, for_update=True)
            if record is None:
                raise CredentialRequired("Nike 账号尚未连接")
            self._match_binding(record, binding)

            now = datetime.now(UTC)
            token = decrypt_secret(record.access_token_enc) if record.access_token_enc else ""

            if rejected_token is not None and (token == rejected_token or not token):
                self._refresh_locked(
                    session, record, force=True, expected_token=token or None
                )
                token = decrypt_secret(record.access_token_enc) if record.access_token_enc else ""
                if not token or token == rejected_token:
                    raise self._auth_failure(record, "令牌刷新失败，请稍后重试")
            elif not token:
                if self._refresh_locked(session, record, force=True, expected_token=None):
                    token = decrypt_secret(record.access_token_enc)
                else:
                    raise self._auth_failure(record, "凭证暂不可用，请稍后重试")
            elif (
                record.access_expires_at is not None
                and record.access_expires_at - timedelta(seconds=REFRESH_MARGIN_SECONDS) < now
            ):
                if self._refresh_locked(session, record, force=False, expected_token=token):
                    token = decrypt_secret(record.access_token_enc)

            if not token:
                raise CredentialRequired("Nike 账号尚未连接")
            expires_at = record.access_expires_at
            if expires_at is not None and expires_at <= now:
                # Never hand out an expired token: a failed refresh must
                # surface as a stable error instead.
                raise self._auth_failure(record, "访问令牌已过期且刷新失败，请稍后重试")
            return token

    def try_refresh(
        self,
        *,
        binding: CredentialBinding | None = None,
        force: bool = False,
        expected_token: str | None = None,
    ) -> bool:
        """Refresh once, serialised in-process and across workers."""
        with _lock_for(self.owner_user_id), self._session_factory() as session:
            self._lock_owner(session)
            record = self._load(session, for_update=True)
            if record is None:
                raise CredentialRequired("Nike 账号尚未连接")
            self._match_binding(record, binding)
            return self._refresh_locked(
                session, record, force=force, expected_token=expected_token
            )

    # ---------------------------------------------------------------- install

    def store_tokens(
        self,
        access_token: str,
        refresh_token: str | None,
        client_id: str = "",
    ) -> CredentialBinding:
        """Install a token pair for this owner (CLI / internal use).

        First binding installs generation 1; replacing an existing credential
        bumps the generation so old jobs can never use the new account.
        """
        with _lock_for(self.owner_user_id), self._session_factory() as session:
            self._lock_owner(session)
            record = self._load(session, for_update=True)
            if record is None:
                record = Credential(
                    owner_user_id=self.owner_user_id,
                    binding_generation=1,
                    status=CredentialStatus.ACTIVE.value,
                )
                session.add(record)
                session.flush()
            else:
                record.binding_generation = record.binding_generation + 1
            apply_tokens_locked(
                record,
                access_token=access_token,
                refresh_token=refresh_token,
                client_id=client_id or self.settings.oauth_client_id,
            )
            session.commit()
            return CredentialBinding(
                owner_user_id=self.owner_user_id,
                credential_id=record.id,
                generation=record.binding_generation,
            )

    def revoke_local(self, session: Session, *, only_if_active: bool = True) -> bool:
        """Mark this owner's credential as locally disconnected.

        Must be called inside an existing transaction that already locked the
        owner row and the credential row via :meth:`lock_for_update`. Clears
        stored token ciphertext and bumps the generation so in-flight work
        stops; returns False when the credential was already revoked.
        """
        record = self._load(session, for_update=True)
        if record is None:
            return False
        if only_if_active and record.status == CredentialStatus.REVOKED.value:
            return False
        record.binding_generation = record.binding_generation + 1
        record.status = CredentialStatus.REVOKED.value
        record.access_token_enc = ""
        record.refresh_token_enc = ""
        record.access_expires_at = None
        record.last_error = ""
        record.last_error_code = None
        record.updated_at = datetime.now(UTC)
        session.flush()
        return True

    # --------------------------------------------------------------- internals

    def lock_for_update(self, session: Session) -> None:
        """Lock the owner row then the credential row (fixed platform order)."""
        self._lock_owner(session)
        self._load(session, for_update=True)

    def _load(self, session: Session, *, for_update: bool = False) -> Credential | None:
        stmt = select(Credential).where(Credential.owner_user_id == self.owner_user_id)
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return session.execute(stmt).scalar_one_or_none()

    def _lock_owner(self, session: Session) -> User:
        user = session.execute(
            select(User).where(User.id == self.owner_user_id).with_for_update()
        ).scalar_one_or_none()
        if user is None or not user.is_active:
            raise CredentialUnavailable("账号不可用")
        return user

    def _require_active_scope(self, session: Session) -> None:
        user = session.execute(
            select(User).where(User.id == self.owner_user_id)
        ).scalar_one_or_none()
        if user is None or not user.is_active:
            raise CredentialUnavailable("账号不可用")
        tenant = session.execute(
            select(Tenant).where(Tenant.id == user.tenant_id)
        ).scalar_one_or_none()
        if tenant is None or not tenant.is_active:
            raise CredentialUnavailable("租户不可用")

    def _match_binding(self, record: Credential, binding: CredentialBinding | None) -> None:
        if binding is None:
            if record.status == CredentialStatus.REVOKED.value:
                raise CredentialChanged("绑定已断开，请重新发起扫描")
            return
        if (
            record.owner_user_id != binding.owner_user_id
            or record.id != binding.credential_id
            or record.binding_generation != binding.generation
        ):
            raise CredentialChanged("绑定已变更，请重新发起扫描")
        if record.status == CredentialStatus.REVOKED.value:
            raise CredentialChanged("绑定已断开，请重新发起扫描")

    def _refresh_locked(
        self,
        session: Session,
        record: Credential,
        *,
        force: bool,
        expected_token: str | None,
    ) -> bool:
        """Refresh under the credential row lock. Commits its own outcome."""
        now = datetime.now(UTC)
        self._match_binding(record, None)

        if record.status == CredentialStatus.NEEDS_AUTHORIZATION.value and not force:
            # A previously revoked grant stays revoked until the user re-binds.
            return False
        if not record.refresh_token_enc:
            if record.status != CredentialStatus.NEEDS_AUTHORIZATION.value:
                self._notify_reauth_required(session, record)
            self._mark_error(record, CredentialStatus.NEEDS_AUTHORIZATION.value, "NIKE_REAUTH_REQUIRED")
            session.commit()
            return False

        current = decrypt_secret(record.access_token_enc) if record.access_token_enc else ""
        if expected_token is not None and current != expected_token:
            # Another worker refreshed while this one waited for the lock.
            return True
        if (
            not force
            and record.access_expires_at is not None
            and record.access_expires_at - timedelta(seconds=REFRESH_MARGIN_SECONDS) > now
        ):
            return True

        cooldown = self.settings.credential_refresh_cooldown_seconds
        if (
            cooldown > 0
            and record.last_error_code is not None
            and record.last_attempt_at is not None
            and (now - record.last_attempt_at).total_seconds() < cooldown
        ):
            # Inside the cooldown no upstream call is made, for both temporary
            # and permanent failures; the remembered outcome stands.
            return False

        record.last_attempt_at = now
        refresh_token = decrypt_secret(record.refresh_token_enc)
        try:
            response = httpx.post(
                f"{self.settings.nike_oauth_base}/token/v1",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": record.client_id or self.settings.oauth_client_id,
                },
                headers={
                    "User-Agent": self.settings.nike_user_agent,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": "https://www.nike.com.cn/",
                },
                timeout=25.0,
            )
        except Exception:  # noqa: BLE001 - network failure is transient, not a revocation
            self._mark_error(record, record.status, "NIKE_UPSTREAM_UNAVAILABLE")
            session.commit()
            return False

        if response.status_code >= 500:
            self._mark_error(record, record.status, "NIKE_UPSTREAM_UNAVAILABLE")
            session.commit()
            return False

        if response.status_code != 200:
            payload: dict = {}
            try:
                payload = response.json()
            except Exception:  # noqa: BLE001 - an unparsable body is still a rejection
                payload = {}
            error = str(payload.get("error") or "")
            if response.status_code in (400, 401) and error in (
                "invalid_grant",
                "invalid_request",
                "unauthorized_client",
                "invalid_client",
            ):
                # Verified permanent rejection: the grant was revoked or the
                # client registration changed; only the user can re-authorize.
                if record.status != CredentialStatus.NEEDS_AUTHORIZATION.value:
                    self._notify_reauth_required(session, record)
                self._mark_error(record, CredentialStatus.NEEDS_AUTHORIZATION.value, "NIKE_REAUTH_REQUIRED")
                session.commit()
                return False
            self._mark_error(record, record.status, "NIKE_UPSTREAM_UNAVAILABLE")
            session.commit()
            return False

        try:
            payload = response.json()
        except Exception:  # noqa: BLE001
            payload = {}
        access_token = str(payload.get("access_token") or "")
        if not access_token:
            self._mark_error(record, record.status, "NIKE_UPSTREAM_UNAVAILABLE")
            session.commit()
            return False
        apply_tokens_locked(
            record,
            access_token=access_token,
            refresh_token=str(payload.get("refresh_token") or "") or None,
            client_id=record.client_id,
        )
        session.commit()
        return True

    def _mark_error(self, record: Credential, status: str, code: str) -> None:
        record.status = status
        record.last_error_code = code
        record.updated_at = datetime.now(UTC)
        logger.info("credential refresh outcome owner=%s code=%s", self.owner_user_id, code)

    def _notify_reauth_required(self, session: Session, record: Credential) -> None:
        """入队一条系统通知：自动续期已停止，只能由用户重新验证。"""
        link = f"{self.settings.public_base_url.rstrip('/')}/account"
        push.enqueue_system_notification(
            session,
            self.settings,
            owner_user_id=record.owner_user_id,
            title=push.REAUTH_REQUIRED_TITLE,
            body=f"{push.REAUTH_REQUIRED_BODY}\n{link}",
        )

    def _auth_failure(self, record: Credential, message: str) -> CredentialError:
        """Map the current credential state onto the stable error contract."""
        if record.status == CredentialStatus.NEEDS_AUTHORIZATION.value:
            return CredentialReauthRequired(message)
        if record.status == CredentialStatus.REVOKED.value:
            return CredentialChanged("绑定已断开，请重新发起扫描")
        if record.status == CredentialStatus.UNASSIGNED.value:
            return CredentialRequired("Nike 账号尚未连接")
        return CredentialUnavailable(message)


def refresh_expiring(settings: Settings, *, limit: int = 50) -> int:
    """Scheduler hook: renew credentials near to expiry without user action.

    A renewable credential must never age into the "needs re-verify" look just
    because nobody used it for an hour; idle accounts stay warm so the account
    card keeps showing a live connection and scans never start on a dead token.
    """
    threshold = datetime.now(UTC) + timedelta(seconds=REFRESH_MARGIN_SECONDS)
    with SessionLocal() as session:
        owners = list(
            session.execute(
                select(Credential.owner_user_id)
                .where(
                    Credential.owner_user_id.is_not(None),
                    Credential.status.in_(
                        [
                            CredentialStatus.ACTIVE.value,
                            CredentialStatus.EXPIRING.value,
                        ]
                    ),
                    Credential.refresh_token_enc != "",
                    Credential.access_expires_at.is_not(None),
                    Credential.access_expires_at <= threshold,
                )
                .order_by(Credential.access_expires_at)
                .limit(limit)
            ).scalars()
        )
    renewed = 0
    for owner_user_id in owners:
        try:
            if CredentialService(settings, owner_user_id).try_refresh():
                renewed += 1
        except CredentialError:
            # A transient upstream problem is retried on the next tick; a
            # permanent rejection is recorded on the row by the refresh itself.
            continue
    return renewed
