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
from sqlalchemy import func, select, update
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
    id: UUID
    status: str
    phone_masked: str | None
    access_expires_at: datetime | None
    last_refreshed_at: datetime | None
    last_error_code: str | None
    binding_generation: int
    cooldown_until: datetime | None
    cooldown_reason: str | None


#: ``pick_ready`` 的"从未用过"占位时刻：空值排前面（先轮换到没用过的账号）。
_LRU_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def pick_ready(session: Session, owner_user_id: UUID | None) -> Credential | None:
    """给一个平台用户选一个可派发的 Nike 账号。

    条件：状态 ACTIVE/EXPIRING、不在冷却窗口内；排序：最近最少使用（``last_used_at``
    升序、空值最前）→ 稳定按 id。全部冷却/不可用时返回 ``None``：调用方暂停该账号的
    派发，到点自动恢复。冷却落在 ``credential.cooldown_until``（见 services/throttle.py）。
    """
    if owner_user_id is None:
        return None
    now = datetime.now(UTC)
    return session.execute(
        select(Credential)
        .where(
            Credential.owner_user_id == owner_user_id,
            Credential.status.in_(
                [CredentialStatus.ACTIVE.value, CredentialStatus.EXPIRING.value]
            ),
            (Credential.cooldown_until.is_(None)) | (Credential.cooldown_until <= now),
        )
        .order_by(func.coalesce(Credential.last_used_at, _LRU_EPOCH).asc(), Credential.id.asc())
        .limit(1)
    ).scalars().first()


def fallback_credential(session: Session, owner_user_id: UUID | None) -> Credential | None:
    """没有可用账号时的回落：仍存在、但需要处理（需重验/冷却）的账号。

    只服务"读一个具体账号"的路径（刷新/绑定/状态），让稳定错误码精确到
    「需要重新验证」而不是笼统的「尚未连接」；派发只用 :func:`pick_ready`。
    """
    if owner_user_id is None:
        return None
    return session.execute(
        select(Credential)
        .where(
            Credential.owner_user_id == owner_user_id,
            Credential.status.notin_(
                [CredentialStatus.REVOKED.value, CredentialStatus.UNASSIGNED.value]
            ),
        )
        .order_by(Credential.updated_at.desc(), Credential.id.asc())
        .limit(1)
    ).scalars().first()


def effective_status(record: Credential) -> str:
    """存储状态按"过期且无法续期"修正后的展示状态。

    只有"已过期且没有 refresh token"才是真的需要用户重新验证；可自动续期的账号
    永远不会因为闲置一小时而被读成"需要重验"。
    """
    status = record.status
    expires_at = record.access_expires_at
    if (
        status == CredentialStatus.ACTIVE.value
        and expires_at is not None
        and (expires_at - datetime.now(UTC)).total_seconds() < 0
        and not record.refresh_token_enc
    ):
        return CredentialStatus.NEEDS_AUTHORIZATION.value
    return status


def status_info_of(record: Credential) -> CredentialStatusInfo:
    """把一行 credential 映射成稳定的展示对象（含冷却窗口）。"""
    return CredentialStatusInfo(
        id=record.id,
        status=effective_status(record),
        phone_masked=record.phone_masked,
        access_expires_at=record.access_expires_at,
        last_refreshed_at=record.last_refreshed_at,
        last_error_code=record.last_error_code,
        binding_generation=record.binding_generation,
        cooldown_until=record.cooldown_until,
        cooldown_reason=record.cooldown_reason,
    )


def current_binding_condition(owner_user_id: UUID, credential_column, generation_column):
    """SQL 谓词：该行写自该用户**当前**的某个账号（未断开且 generation 未变）。

    一用户多账号下"当前绑定"是账号集合：任一账号按其当前 generation 写入的行仍然
    属于该用户；换绑（generation+1）或断开（REVOKED）之后旧行不再展示。
    """
    return (
        select(Credential.id)
        .where(
            Credential.id == credential_column,
            Credential.owner_user_id == owner_user_id,
            Credential.status.notin_(
                [CredentialStatus.REVOKED.value, CredentialStatus.UNASSIGNED.value]
            ),
            Credential.binding_generation == generation_column,
        )
        .exists()
    )


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
        credential_id: UUID | None = None,
        session_factory: Callable[[], Session] = SessionLocal,
    ) -> None:
        self.settings = settings
        self.owner_user_id = owner_user_id
        #: 明确指定账号时只操作那一行；为空时按 ``pick_ready`` 自动选号。
        self.credential_id = credential_id
        self._session_factory = session_factory

    # ------------------------------------------------------------------ reads

    def binding(self) -> CredentialBinding:
        """Return a usable binding for this owner (explicit id or auto-picked).

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
            record = self._load(session, binding=binding)
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
            return status_info_of(record)

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
            record = self._load(session, for_update=True, binding=binding)
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
            record = self._load(session, for_update=True, binding=binding)
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

        An explicit ``credential_id`` updates that account and bumps its
        generation so old jobs can never use the new tokens. Without an id the
        owner must have at most one account: none inserts generation 1, one is
        replaced, several refuse (the operator must pick a target).
        """
        with _lock_for(self.owner_user_id), self._session_factory() as session:
            self._lock_owner(session)
            if self.credential_id is not None:
                record = self._load(session, for_update=True)
                if record is None:
                    raise CredentialRequired("指定的 Nike 账号不存在")
            else:
                records = session.execute(
                    select(Credential)
                    .where(Credential.owner_user_id == self.owner_user_id)
                    .with_for_update()
                ).scalars().all()
                if len(records) > 1:
                    raise CredentialUnavailable("该用户有多个 Nike 账号，请指定账号")
                record = records[0] if records else None
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

    def mark_used(self, credential_id: UUID) -> None:
        """LRU 前进：派发成功后记录该账号刚被使用（供 ``pick_ready`` 轮换）。

        只写 ``last_used_at``；``last_attempt_at`` 是续期退避的时间戳，派发不能碰它。
        """
        with self._session_factory() as session:
            session.execute(
                update(Credential)
                .where(
                    Credential.id == credential_id,
                    Credential.owner_user_id == self.owner_user_id,
                )
                .values(last_used_at=datetime.now(UTC))
            )
            session.commit()

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
        # 断开后不再有任何冷却语义：残留窗口不该暂停该用户其它账号的派发。
        record.cooldown_until = None
        record.cooldown_level = 0
        record.cooldown_reason = None
        record.cooldown_notified_at = None
        record.updated_at = datetime.now(UTC)
        session.flush()
        return True

    # --------------------------------------------------------------- internals

    def lock_for_update(self, session: Session) -> None:
        """Lock the owner row then the credential row (fixed platform order)."""
        self._lock_owner(session)
        self._load(session, for_update=True)

    def _load(
        self,
        session: Session,
        *,
        for_update: bool = False,
        binding: CredentialBinding | None = None,
    ) -> Credential | None:
        """Load the addressed credential row (binding > explicit id > auto-pick)."""
        credential_id = binding.credential_id if binding is not None else self.credential_id
        if credential_id is None:
            picked = pick_ready(session, self.owner_user_id)
            if picked is None:
                picked = fallback_credential(session, self.owner_user_id)
            if picked is None or not for_update:
                return picked
            credential_id = picked.id
        stmt = select(Credential).where(
            Credential.id == credential_id,
            Credential.owner_user_id == self.owner_user_id,
        )
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
        rows = list(
            session.execute(
                select(Credential.id, Credential.owner_user_id)
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
            ).all()
        )
    renewed = 0
    for credential_id, owner_user_id in rows:
        try:
            if CredentialService(
                settings, owner_user_id, credential_id=credential_id
            ).try_refresh():
                renewed += 1
        except CredentialError:
            # A transient upstream problem is retried on the next tick; a
            # permanent rejection is recorded on the row by the refresh itself.
            continue
    return renewed
