"""Seed the isolated QA database.

Run inside the api container after the migrate gate:

    docker compose -p nike-mp-qa -f compose.yaml -f qa/compose.qa.yaml \
      exec -T api python - < qa/seed_qa.py

Creates the platform operator, one customer tenant, a crypto placeholder
credential (fake tokens only) and one registered style.
"""

from __future__ import annotations

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Role, Style, Tenant, User
from app.security import hash_password
from app.services.credentials import CredentialService

PASSWORD = "qa-password"


def _tenant(db, slug: str, name: str, is_platform: bool) -> Tenant:
    tenant = db.execute(select(Tenant).where(Tenant.slug == slug)).scalar_one_or_none()
    if tenant is None:
        tenant = Tenant(slug=slug, name=name, is_platform=is_platform, daily_review_budget=60000)
        db.add(tenant)
        db.flush()
    return tenant


def _user(db, tenant: Tenant, email: str, role: str) -> None:
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None:
        db.add(
            User(
                tenant_id=tenant.id,
                email=email,
                display_name=email,
                password_hash=hash_password(PASSWORD),
                role=role,
            )
        )


def main() -> None:
    settings = get_settings()
    with SessionLocal() as db:
        platform = _tenant(db, "platform", "服务方", is_platform=True)
        _user(db, platform, "ops@qa.local", Role.OPS_ADMIN.value)

        customer = _tenant(db, "qa-customer", "QA 客户", is_platform=False)
        _user(db, customer, "qa@qa.local", Role.TENANT_ADMIN.value)

        if db.execute(select(Style).where(Style.style_code == "DN3707-202")).scalar_one_or_none() is None:
            db.add(Style(style_code="DN3707-202", title="离线测试货号"))

        # Distinct fake tokens; the stub accepts any of them.
        CredentialService(settings, db).store_tokens(
            access_token="qa-access-token", refresh_token="qa-refresh-token", client_id="qa"
        )
        db.commit()
    print("qa seed ready: ops@qa.local / qa@qa.local, password qa-password")


if __name__ == "__main__":
    main()
