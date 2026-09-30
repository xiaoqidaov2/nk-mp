"""Shared JSON error helpers for API routes."""

from __future__ import annotations

from fastapi.responses import JSONResponse

from app.services.credentials import CredentialChanged, CredentialError, CredentialRequired

_CREDENTIAL_DETAILS: dict[str, tuple[str, int]] = {
    "NIKE_CREDENTIAL_REQUIRED": ("需要先连接 Nike 账号", 409),
    "NIKE_CREDENTIAL_CHANGED": ("绑定已变更，请发起新的扫描", 409),
    "NIKE_REAUTH_REQUIRED": ("Nike 账号需要重新验证", 409),
    "NIKE_UPSTREAM_UNAVAILABLE": ("Nike 服务暂不可用，请稍后重试", 503),
}


def credential_error_response(exc: CredentialError) -> JSONResponse:
    """Map a stable credential error onto the shared JSON error shape."""
    code = str(getattr(exc, "code", "NIKE_CREDENTIAL_REQUIRED"))
    detail, status = _CREDENTIAL_DETAILS.get(code, ("需要先连接 Nike 账号", 409))
    return JSONResponse(
        status_code=status,
        content={"detail": detail, "error_code": code, "retry_after_seconds": None},
    )


def credential_required_response() -> JSONResponse:
    return credential_error_response(CredentialRequired("需要先连接 Nike 账号"))


def binding_changed_response() -> JSONResponse:
    return credential_error_response(CredentialChanged("绑定已变更，请发起新的扫描"))
