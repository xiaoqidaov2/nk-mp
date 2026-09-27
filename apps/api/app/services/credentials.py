"""Access-token lifecycle with a single-flight refresh guard."""

from __future__ import annotations

import base64
import json
import time
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Credential, CredentialStatus
from app.security import decrypt_secret, encrypt_secret

REFRESH_MARGIN_SECONDS = 300


class CredentialNotConfigured(RuntimeError):
    pass


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


class CredentialService:
    def __init__(self, settings: Settings, session: Session) -> None:
        self.settings = settings
        self.session = session

    def get_record(self, account_key: str = "default", for_update: bool = False) -> Credential | None:
        stmt = select(Credential).where(Credential.account_key == account_key)
        if for_update:
            stmt = stmt.with_for_update()
        return self.session.execute(stmt).scalar_one_or_none()

    def access_token(self, account_key: str = "default") -> str:
        record = self.get_record(account_key)
        if record is None or not record.access_token_enc:
            raise CredentialNotConfigured("no credential stored; run the import command")
        token = decrypt_secret(record.access_token_enc)
        expires_at = record.access_expires_at
        margin = timedelta(seconds=REFRESH_MARGIN_SECONDS)
        if (
            expires_at is not None
            and expires_at - margin < datetime.now(UTC)
            and self.try_refresh(account_key)
        ):
            refreshed = self.get_record(account_key)
            if refreshed is not None:
                token = decrypt_secret(refreshed.access_token_enc)
        return token

    def try_refresh(self, account_key: str = "default") -> bool:
        """Refresh the access token once, serialised across workers."""
        record = self.get_record(account_key, for_update=True)
        if record is None or not record.refresh_token_enc:
            self._mark_needs_authorization(record, "refresh token missing")
            return False
        now = datetime.now(UTC)
        if record.access_expires_at and record.access_expires_at - timedelta(
            seconds=REFRESH_MARGIN_SECONDS
        ) > now:
            # Another worker refreshed while this one waited for the lock.
            return True
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
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 - any failure means "needs authorization"
            self._mark_needs_authorization(record, str(exc)[:280])
            return False

        access_token = payload.get("access_token")
        if not access_token:
            self._mark_needs_authorization(record, "token endpoint returned no access_token")
            return False
        record.access_token_enc = encrypt_secret(access_token)
        rotated = payload.get("refresh_token")
        if rotated:
            record.refresh_token_enc = encrypt_secret(rotated)
        record.access_expires_at = _decode_expiry(access_token)
        record.last_refreshed_at = datetime.now(UTC)
        record.status = CredentialStatus.ACTIVE.value
        record.last_error = ""
        self.session.flush()
        return True

    def store_tokens(
        self,
        access_token: str,
        refresh_token: str | None,
        client_id: str = "",
        account_key: str = "default",
    ) -> Credential:
        record = self.get_record(account_key)
        if record is None:
            record = Credential(account_key=account_key)
            self.session.add(record)
        record.access_token_enc = encrypt_secret(access_token)
        if refresh_token:
            record.refresh_token_enc = encrypt_secret(refresh_token)
        record.client_id = client_id or self.settings.oauth_client_id
        record.access_expires_at = _decode_expiry(access_token)
        record.status = CredentialStatus.ACTIVE.value
        record.last_error = ""
        record.updated_at = datetime.now(UTC)
        self.session.flush()
        return record

    def status(self, account_key: str = "default") -> dict[str, object]:
        record = self.get_record(account_key)
        if record is None:
            return {
                "account_key": account_key,
                "status": CredentialStatus.NEEDS_AUTHORIZATION.value,
                "access_expires_at": None,
                "last_refreshed_at": None,
                "last_error": "credential not imported",
                "has_refresh_token": False,
            }
        expires_at = record.access_expires_at
        status = record.status
        if status == CredentialStatus.ACTIVE.value and expires_at is not None:
            remaining = (expires_at - datetime.now(UTC)).total_seconds()
            if remaining < 0:
                status = CredentialStatus.NEEDS_AUTHORIZATION.value
            elif remaining < 3600:
                status = CredentialStatus.EXPIRING.value
        return {
            "account_key": record.account_key,
            "status": status,
            "access_expires_at": expires_at,
            "last_refreshed_at": record.last_refreshed_at,
            "last_error": record.last_error,
            "has_refresh_token": bool(record.refresh_token_enc),
        }

    def _mark_needs_authorization(self, record: Credential | None, message: str) -> None:
        if record is None:
            return
        record.status = CredentialStatus.NEEDS_AUTHORIZATION.value
        record.last_error = message
        record.updated_at = datetime.now(UTC)
        self.session.flush()


def monotonic() -> float:
    return time.monotonic()
