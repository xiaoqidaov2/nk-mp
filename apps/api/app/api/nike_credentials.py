"""Self-service Nike credential endpoints (``/me/...``).

All routes operate on the logged-in user only; no route accepts an owner,
user or tenant id. Attempts are bound to the platform auth session that
created them: other owners or other sessions always see 404.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_session, get_db, require_self_write, settings_dep
from app.config import Settings
from app.models import AuthSession, OwnerBootstrap, User
from app.schemas import (
    NikeAuthAttemptOut,
    NikeAuthSessionIn,
    NikeAuthVerifyIn,
    NikeBootstrapOut,
    NikeCredentialOut,
)
from app.services import bootstrap as bootstrap_service
from app.services import nike_auth
from app.services.credentials import CredentialError, CredentialService

router = APIRouter(prefix="/me", tags=["nike-credential"])


def _error(exc: nike_auth.NikeAuthError) -> JSONResponse:
    body = {
        "detail": exc.detail,
        "error_code": exc.code,
        "retry_after_seconds": exc.retry_after_seconds,
    }
    headers: dict[str, str] = {}
    if exc.status_code == 429 and exc.retry_after_seconds:
        headers["Retry-After"] = str(int(exc.retry_after_seconds))
    return JSONResponse(status_code=exc.status_code, content=body, headers=headers or None)


def _attempt_out(view: nike_auth.AttemptView) -> NikeAuthAttemptOut:
    return NikeAuthAttemptOut(
        id=view.id,
        state=view.state,
        phone_masked=view.phone_masked,
        expires_at=view.expires_at,
        resend_after=view.resend_after,
        attempts_remaining=view.attempts_remaining,
        error_code=view.error_code,
    )


def _credential_out(settings: Settings, user_id: uuid.UUID) -> NikeCredentialOut:
    data = nike_auth.credential_view(settings, user_id)
    data["login_available"] = nike_auth.login_available(settings)
    return NikeCredentialOut(**data)


def _not_available() -> JSONResponse:
    return _error(
        nike_auth.NikeAuthError(
            nike_auth.NOT_AVAILABLE_CODE, "Nike 短信登录尚未开放", 503
        )
    )


@router.get("/nike-credential", response_model=NikeCredentialOut)
def get_nike_credential(
    settings: Settings = Depends(settings_dep),
    payload: tuple[User, AuthSession] = Depends(get_current_session),
) -> NikeCredentialOut:
    user, _ = payload
    return _credential_out(settings, user.id)


@router.post("/nike-auth/sessions", response_model=NikeAuthAttemptOut, status_code=201)
def start_nike_auth_session(
    body: NikeAuthSessionIn,
    request: Request,
    response: Response,
    settings: Settings = Depends(settings_dep),
    payload: tuple[User, AuthSession] = Depends(require_self_write),
):
    user, auth_session = payload
    if not nike_auth.login_available(settings):
        return _not_available()
    try:
        client_ip = nike_auth.client_ip_for(request, settings)
        view, created = nike_auth.create_attempt(
            settings=settings,
            user_id=user.id,
            auth_session_id=auth_session.id,
            phone=body.phone,
            request_id=body.request_id,
            client_ip=client_ip,
        )
    except nike_auth.NikeAuthError as exc:
        return _error(exc)
    if not created:
        response.status_code = 200
    return _attempt_out(view)


@router.get("/nike-auth/sessions/{attempt_id}", response_model=NikeAuthAttemptOut)
def get_nike_auth_session(
    attempt_id: uuid.UUID,
    settings: Settings = Depends(settings_dep),
    payload: tuple[User, AuthSession] = Depends(get_current_session),
):
    user, auth_session = payload
    try:
        view = nike_auth.get_attempt(
            settings=settings,
            user_id=user.id,
            auth_session_id=auth_session.id,
            attempt_id=attempt_id,
        )
    except nike_auth.NikeAuthError as exc:
        return _error(exc)
    return _attempt_out(view)


@router.post(
    "/nike-auth/sessions/{attempt_id}/verify", response_model=NikeCredentialOut
)
def verify_nike_auth_session(
    attempt_id: uuid.UUID,
    body: NikeAuthVerifyIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(settings_dep),
    payload: tuple[User, AuthSession] = Depends(require_self_write),
):
    user, auth_session = payload
    if not nike_auth.login_available(settings):
        return _not_available()
    try:
        nike_auth.verify_attempt(
            settings=settings,
            user_id=user.id,
            auth_session_id=auth_session.id,
            attempt_id=attempt_id,
            code=body.code,
        )
    except nike_auth.NikeAuthError as exc:
        return _error(exc)
    # 绑定成功的瞬间就登记预加载：用该用户自己的连接拉取门店目录并缓存。
    bootstrap_service.ensure_plan(db, user.id)
    db.commit()
    return _credential_out(settings, user.id)


@router.delete("/nike-auth/sessions/{attempt_id}", status_code=204)
def cancel_nike_auth_session(
    attempt_id: uuid.UUID,
    payload: tuple[User, AuthSession] = Depends(require_self_write),
) -> Response:
    user, auth_session = payload
    try:
        nike_auth.cancel_attempt(
            user_id=user.id,
            auth_session_id=auth_session.id,
            attempt_id=attempt_id,
        )
    except nike_auth.NikeAuthError as exc:
        return _error(exc)
    return Response(status_code=204)


@router.get("/bootstrap", response_model=NikeBootstrapOut)
def get_bootstrap(
    db: Session = Depends(get_db),
    payload: tuple[User, AuthSession] = Depends(get_current_session),
) -> NikeBootstrapOut:
    """本用户的绑定预加载进度；从未绑定过时返回 state=NONE。"""
    user, _ = payload
    plan = db.get(OwnerBootstrap, user.id)
    if plan is None:
        return NikeBootstrapOut(
            state="NONE", progress=0.0, stage="", store_count=0, error_code=None, updated_at=None
        )
    return NikeBootstrapOut(
        state=plan.state,
        progress=plan.progress,
        stage=plan.stage,
        store_count=plan.store_count,
        error_code=plan.error_code,
        updated_at=plan.updated_at,
    )


@router.post("/bootstrap", response_model=NikeBootstrapOut, status_code=202)
def start_bootstrap(
    db: Session = Depends(get_db),
    settings: Settings = Depends(settings_dep),
    payload: tuple[User, AuthSession] = Depends(require_self_write),
) -> NikeBootstrapOut | JSONResponse:
    """重新排队预加载（失败重试 / 手动刷新缓存）。"""
    user, _ = payload
    try:
        CredentialService(settings, user.id).binding()
    except CredentialError as exc:
        return _error(exc)
    plan = bootstrap_service.ensure_plan(db, user.id)
    db.commit()
    return NikeBootstrapOut(
        state=plan.state,
        progress=plan.progress,
        stage=plan.stage,
        store_count=plan.store_count,
        error_code=plan.error_code,
        updated_at=plan.updated_at,
    )


@router.delete("/nike-credential", status_code=204)
def disconnect_nike_credential(
    settings: Settings = Depends(settings_dep),
    payload: tuple[User, AuthSession] = Depends(require_self_write),
) -> Response:
    user, _ = payload
    nike_auth.disconnect(settings=settings, user_id=user.id)
    return Response(status_code=204)
