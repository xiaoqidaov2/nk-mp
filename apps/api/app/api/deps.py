"""Session-cookie authentication and tenant-scoped dependencies."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import SessionLocal
from app.models import AuthSession, Role, Tenant, User
from app.security import csrf_matches, token_fingerprint

SESSION_COOKIE = "nike_mp_session"
CSRF_HEADER = "X-CSRF-Token"

OPS_ROLES = {Role.OPS_ADMIN.value, Role.OPS_OPERATOR.value}
WRITE_ROLES = {Role.OPS_ADMIN.value, Role.OPS_OPERATOR.value, Role.TENANT_ADMIN.value}


def get_db() -> Session:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_current_session(
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    db: Session = Depends(get_db),
) -> tuple[User, AuthSession]:
    if not session_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")
    record = db.execute(
        select(AuthSession).where(AuthSession.token_hash == token_fingerprint(session_token))
    ).scalar_one_or_none()
    if record is None or record.revoked_at is not None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session not found")
    if record.expires_at <= datetime.now(UTC):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session expired")
    user = db.execute(select(User).where(User.id == record.user_id)).scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user disabled")
    return user, record


def get_current_user(
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    payload: tuple[User, AuthSession] = Depends(get_current_session),
) -> User:
    return payload[0]


def _assert_csrf(
    session_token: str | None,
    record: AuthSession,
    provided: str | None,
) -> None:
    if not session_token or not csrf_matches(session_token, record.csrf_secret, provided or ""):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid CSRF token")


def require_write(
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    csrf: str | None = Header(default=None, alias=CSRF_HEADER),
    payload: tuple[User, AuthSession] = Depends(get_current_session),
) -> User:
    user, record = payload
    _assert_csrf(session_token, record, csrf)
    if user.role not in WRITE_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "read-only role")
    return user


def require_ops(user: User = Depends(require_write)) -> User:
    if user.role not in OPS_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "operations role required")
    return user


def tenant_scope(user: User, db: Session) -> tuple[Tenant, bool]:
    """Return the tenant to query and whether the caller sees every tenant."""
    tenant = db.execute(select(Tenant).where(Tenant.id == user.tenant_id)).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "tenant not found")
    return tenant, bool(tenant.is_platform)


def settings_dep() -> Settings:
    return get_settings()


def client_hint(request: Request) -> str:
    return (request.headers.get("user-agent") or "")[:280]
