"""Seed the isolated QA database.

Run inside the api container after the migrate gate:

    docker compose -p nike-mp-auth-qa -f qa/compose.auth.yaml \
      exec -T api python - < qa/seed_qa.py

Creates three platform users - the operator and the two customer accounts A/B -
plus one registered style. No credential is installed: the smoke test binds
every account itself through the self-service phone flow, so nothing here fakes
a Nike token.
"""

from __future__ import annotations

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Role, Style, Tenant, User
from app.security import hash_password

PASSWORD = "qa-password"

USERS = (
    # email, tenant slug, role
    ("ops@qa.local", "platform", Role.OPS_ADMIN.value),
    ("qa-a@qa.local", "qa-customer", Role.TENANT_ADMIN.value),
    ("qa-b@qa.local", "qa-customer", Role.TENANT_ADMIN.value),
)


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
    with SessionLocal() as db:
        tenant_by_slug = {
            "platform": _tenant(db, "platform", "服务方", is_platform=True),
            "qa-customer": _tenant(db, "qa-customer", "QA 客户", is_platform=False),
        }
        for email, slug, role in USERS:
            _user(db, tenant_by_slug[slug], email, role)
        db.flush()

        # 货号自 0003 起按用户归属：给操作员登记一条，其余用户按需自建。
        ops = db.execute(select(User).where(User.email == "ops@qa.local")).scalar_one_or_none()
        if ops is not None and (
            db.execute(
                select(Style).where(
                    Style.owner_user_id == ops.id, Style.style_code == "DN3707-202"
                )
            ).scalar_one_or_none()
            is None
        ):
            db.add(Style(style_code="DN3707-202", title="离线测试货号", owner_user_id=ops.id))

        db.commit()
    print("qa seed ready: ops@qa.local / qa-a@qa.local / qa-b@qa.local, password qa-password")


if __name__ == "__main__":
    main()
