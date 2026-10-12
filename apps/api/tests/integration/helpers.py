"""Shared helpers for PostgreSQL integration tests."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.api.deps import CSRF_HEADER, SESSION_COOKIE
from app.models import Role, Store, Style, Tenant, User
from app.security import hash_password


def make_tenant(
    db,
    *,
    slug: str | None = None,
    name: str = "测试租户",
    is_platform: bool = False,
    budget: int = 5000,
) -> Tenant:
    tenant = Tenant(
        slug=slug or f"t-{uuid.uuid4().hex[:8]}",
        name=name,
        is_platform=is_platform,
        daily_review_budget=budget,
    )
    db.add(tenant)
    db.flush()
    return tenant


def make_user(
    db,
    tenant,
    *,
    email: str | None = None,
    password: str = "test-password",
    role: str | None = None,
) -> User:
    user = User(
        tenant_id=tenant.id,
        email=email or f"u-{uuid.uuid4().hex[:8]}@example.com",
        display_name="测试用户",
        password_hash=hash_password(password),
        role=role or Role.TENANT_ADMIN.value,
    )
    db.add(user)
    db.flush()
    return user


def make_store(
    db,
    *,
    owner: User,
    code: str | None = None,
    name: str = "换季优惠测试店",
    region_id: str | None = None,
) -> Store:
    store = Store(
        owner_user_id=owner.id,
        store_code=code or f"S{uuid.uuid4().hex[:6]}",
        display_name=name,
        region_id=region_id or f"NIKE0S{uuid.uuid4().hex[:4]}",
    )
    db.add(store)
    db.flush()
    return store


def make_style(db, *, owner: User, code: str | None = None) -> Style:
    style = Style(
        owner_user_id=owner.id,
        style_code=code or f"ST{uuid.uuid4().hex[:6]}",
        title="测试货号",
    )
    db.add(style)
    db.flush()
    return style


def login(client: TestClient, email: str, password: str = "test-password") -> str:
    """Log in and return the CSRF token (also stored in a readable cookie)."""
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


def auth_headers(csrf_token: str) -> dict[str, str]:
    return {"X-CSRF-Token": csrf_token}


def login_session(
    client: TestClient, email: str, password: str = "test-password"
) -> dict[str, str]:
    """Log in and return the raw platform session token and CSRF token.

    The session token lets a test address an *older* login session explicitly
    (per-request cookies) while a newer login has replaced the client cookie.
    """
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    session = response.cookies.get(SESSION_COOKIE)
    assert session, "login did not set a session cookie"
    return {"session": session, "csrf": response.json()["csrf_token"]}


def session_cookies(session_token: str) -> dict[str, str]:
    return {SESSION_COOKIE: session_token}


def csrf_headers(csrf_token: str) -> dict[str, str]:
    return {CSRF_HEADER: csrf_token}


def store_credential(
    settings,
    user_id,
    *,
    access_token: str = "access-token",
    refresh_token: str = "refresh-token",
):
    """Install a credential binding for a user; returns the frozen binding."""
    from app.services.credentials import CredentialService

    return CredentialService(settings, user_id).store_tokens(access_token, refresh_token)


def credential_binding(settings, user_id):
    from app.services.credentials import CredentialService

    return CredentialService(settings, user_id).binding()
