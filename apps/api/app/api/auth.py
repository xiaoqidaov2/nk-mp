from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CSRF_COOKIE, SESSION_COOKIE, get_current_session, get_db
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
from app.services import nike_auth

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
        # 行锁把同一账号的并发登录串行化：否则失败次数会丢更新，锁定形同虚设。
        select(User).where(User.email == payload.email.strip().lower()).with_for_update()
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if user is not None and user.login_locked_until is not None and user.login_locked_until > now:
        retry = int((user.login_locked_until - now).total_seconds()) + 1
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "登录尝试过多，请稍后再试",
            headers={"Retry-After": str(retry)},
        )
    if user is None or not user.is_active or not verify_password(payload.password, user.password_hash):
        if user is not None:
            # 只对已知账号计数：不存在的邮箱无从锁定，也不泄漏账号是否存在。
            user.failed_login_attempts = int(user.failed_login_attempts or 0) + 1
            if user.failed_login_attempts >= settings.login_max_attempts:
                user.failed_login_attempts = 0
                user.login_locked_until = now + timedelta(seconds=settings.login_lock_seconds)
                db.commit()
                raise HTTPException(
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    "登录尝试过多，请稍后再试",
                    headers={"Retry-After": str(settings.login_lock_seconds)},
                )
            db.commit()
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
    user.last_login_at = now
    user.failed_login_attempts = 0
    user.login_locked_until = None
    db.flush()
    # Commit before returning: the session row must be visible to the very
    # next request; teardown-time commits can land after the response.
    db.commit()

    csrf_token = csrf_token_for(token, csrf_secret)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        max_age=settings.session_ttl_hours * 3600,
        path="/",
    )
    response.set_cookie(
        # Readable by the SPA so a page refresh can restore the CSRF token
        # without keeping it in a module variable.
        CSRF_COOKIE,
        csrf_token,
        httponly=False,
        samesite="lax",
        secure=settings.cookie_secure,
        max_age=settings.session_ttl_hours * 3600,
        path="/",
    )
    return LoginResponse(
        user=_to_current_user(user, tenant),
        csrf_token=csrf_token,
    )


@router.post("/logout")
def logout(
    response: Response,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    payload: tuple[User, AuthSession] = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    user, record = payload
    record.revoked_at = datetime.now(UTC)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    db.flush()
    db.commit()
    # Unfinished phone-binding attempts belong to this platform session;
    # an already bound Nike credential stays connected.
    with contextlib.suppress(nike_auth.NikeAuthError):
        nike_auth.cancel_for_auth_session(user_id=user.id, auth_session_id=record.id)
    return {"ok": True}


@router.get("/me", response_model=CurrentUser)
def me(
    payload: tuple[User, AuthSession] = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> CurrentUser:
    user, _ = payload
    tenant = db.execute(select(Tenant).where(Tenant.id == user.tenant_id)).scalar_one()
    return _to_current_user(user, tenant)
