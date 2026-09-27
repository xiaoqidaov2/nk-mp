"""Create tables and the first platform tenant without running migrations."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from app.db import Base, SessionLocal, engine
from app.models import MonitorTask, Role, Store, Style, Tenant, User
from app.security import hash_password


def create_schema() -> None:
    Base.metadata.create_all(bind=engine)


def ensure_platform_tenant(email: str, password: str, name: str = "服务方") -> Tenant:
    with SessionLocal() as db:
        tenant = db.execute(select(Tenant).where(Tenant.slug == "platform")).scalar_one_or_none()
        if tenant is None:
            tenant = Tenant(slug="platform", name=name, is_platform=True, daily_review_budget=60000)
            db.add(tenant)
            db.flush()
        user = db.execute(select(User).where(User.email == email.lower())).scalar_one_or_none()
        if user is None:
            db.add(
                User(
                    tenant_id=tenant.id,
                    email=email.lower(),
                    display_name=email,
                    password_hash=hash_password(password),
                    role=Role.OPS_ADMIN.value,
                )
            )
        db.commit()
    return tenant


def seed_isolated_demo() -> None:
    """Insert a tiny offline dataset so the UI can be reviewed without credentials."""
    now = datetime.now(UTC)
    with SessionLocal() as db:
        tenant = db.execute(select(Tenant).where(Tenant.slug == "demo")).scalar_one_or_none()
        if tenant is None:
            tenant = Tenant(slug="demo", name="演示租户", daily_review_budget=2000)
            db.add(tenant)
            db.flush()
        user = db.execute(select(User).where(User.email == "demo@example.com")).scalar_one_or_none()
        if user is None:
            db.add(
                User(
                    tenant_id=tenant.id,
                    email="demo@example.com",
                    display_name="演示账号",
                    password_hash=hash_password("demo-password"),
                    role=Role.TENANT_ADMIN.value,
                )
            )
        for code, name in (("3752", "南昌明矾"), ("3875", "西安门店")):
            if db.execute(select(Store).where(Store.store_code == code)).scalar_one_or_none() is None:
                db.add(
                    Store(
                        store_code=code,
                        display_name=name,
                        address="演示数据",
                        region_id=f"NIKE0S{code}",
                        last_seen_at=now,
                    )
                )
        for code in ("DN3707-202", "IO5031-600"):
            if db.execute(select(Style).where(Style.style_code == code)).scalar_one_or_none() is None:
                db.add(Style(style_code=code, title=f"演示货号 {code}"))
        if db.execute(select(MonitorTask).where(MonitorTask.name == "演示任务")).scalar_one_or_none() is None:
            db.add(
                MonitorTask(
                    tenant_id=tenant.id,
                    name="演示任务",
                    store_codes=["3752", "3875"],
                    style_codes=["DN3707-202", "IO5031-600"],
                    cadence_seconds=1800,
                    is_active=False,
                    next_run_at=None,
                )
            )
        db.commit()
