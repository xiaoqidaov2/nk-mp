from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import SESSION_COOKIE, get_current_session, get_db
from app.config import Settings, get_settings
from app.models import AuthSession, Tenant, User
from app.schemas import CurrentUser, LoginRequest, LoginResponse
from app.security import (
    csrf_token_for,
    new_csrf_secret,
    new_session_token,
    token_fingerprint,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _to_current_user(user: User, tenant: Tenant) -> CurrentUser:
    return CurrentUser(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        tenant_id=tenant.id,
        tenant_name=tenant.name,
        tenant_slug=tenant.slug,
        is_platform=tenant.is_platform,
    )


@router.post("/login", response_model=LoginResponse)
def login(
    payload: LoginRequest,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> LoginResponse:
    user = db.execute(
        select(User).where(User.email == payload.email.strip().lower())
    ).scalar_one_or_none()
    if user is None or not user.is_active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "邮箱或密码不正确")
    tenant = db.execute(select(Tenant).where(Tenant.id == user.tenant_id)).scalar_one_or_none()
    if tenant is None or not tenant.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "租户不可用")

    token = new_session_token()
    csrf_secret = new_csrf_secret()
    record = AuthSession(
        user_id=user.id,
        token_hash=token_fingerprint(token),
        csrf_secret=csrf_secret,
        user_agent="",
        expires_at=datetime.now(UTC) + timedelta(hours=settings.session_ttl_hours),
    )
    db.add(record)
    user.last_login_at = datetime.now(UTC)
    db.flush()

    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        max_age=settings.session_ttl_hours * 3600,
        path="/",
    )
    return LoginResponse(
        user=_to_current_user(user, tenant),
        csrf_token=csrf_token_for(token, csrf_secret),
    )


@router.post("/logout")
def logout(
    response: Response,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    payload: tuple[User, AuthSession] = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    _, record = payload
    record.revoked_at = datetime.now(UTC)
    response.delete_cookie(SESSION_COOKIE, path="/")
    db.flush()
    return {"ok": True}


@router.get("/me", response_model=CurrentUser)
def me(
    payload: tuple[User, AuthSession] = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> CurrentUser:
    user, _ = payload
    tenant = db.execute(select(Tenant).where(Tenant.id == user.tenant_id)).scalar_one()
    return _to_current_user(user, tenant)
