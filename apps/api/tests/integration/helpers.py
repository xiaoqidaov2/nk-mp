"""Shared helpers for PostgreSQL integration tests."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

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
    db, *, code: str | None = None, name: str = "测试门店", region_id: str | None = None
) -> Store:
    store = Store(
        store_code=code or f"S{uuid.uuid4().hex[:6]}",
        display_name=name,
        region_id=region_id or f"NIKE0S{uuid.uuid4().hex[:4]}",
    )
    db.add(store)
    db.flush()
    return store


def make_style(db, *, code: str | None = None) -> Style:
    style = Style(style_code=code or f"ST{uuid.uuid4().hex[:6]}", title="测试货号")
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
