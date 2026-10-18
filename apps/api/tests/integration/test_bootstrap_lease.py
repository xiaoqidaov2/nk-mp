"""Bootstrap plan leases (PostgreSQL): multi-worker ownership.

Only the worker holding ``owner_bootstrap.leased_by`` may renew the lease or
write state; a takeover (expired lease claimed by another worker) means the old
worker must stop without publishing progress.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.config import get_settings
from app.db import SessionLocal
from app.models import BootstrapState, OwnerBootstrap
from app.services.bootstrap import (
    BootstrapLeaseLost,
    _checkpoint,
    ensure_plan,
    lease_next_plan,
    run_plan,
)

from .helpers import make_tenant, make_user, store_credential


def _seed_plan() -> dict:
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant, email=f"lease-{uuid.uuid4().hex[:8]}@example.com")
        db.commit()
        user_id = user.id
    settings = get_settings()
    store_credential(settings, user_id)
    with SessionLocal() as db:
        plan = ensure_plan(db, user_id)
        db.commit()
        return {"user_id": user_id, "owner_id": plan.owner_user_id}


def test_plan_lease_records_the_owner_and_rejects_another_worker() -> None:
    settings = get_settings()
    seed = _seed_plan()
    with SessionLocal() as db:
        plan = lease_next_plan(db, settings, "worker-A")
        db.commit()
        assert plan is not None and plan.owner_user_id == seed["user_id"]
        assert plan.leased_by == "worker-A"
        assert plan.state == BootstrapState.RUNNING.value

    # B 拿着 A 租约中的计划调用 run_plan：不写状态、不计失败（新持有者 A 不受影响）。
    with SessionLocal() as db:
        plan = db.get(OwnerBootstrap, seed["owner_id"])
        before = (plan.state, plan.progress, plan.lease_until)
        run_plan(db, settings, plan, "worker-B")
        db.commit()
    with SessionLocal() as db:
        plan = db.get(OwnerBootstrap, seed["owner_id"])
        assert (plan.state, plan.progress, plan.lease_until) == before
        assert plan.leased_by == "worker-A"
        assert plan.attempts == 1


def test_checkpoint_renews_only_for_the_lease_holder() -> None:
    settings = get_settings()
    seed = _seed_plan()
    with SessionLocal() as db:
        plan = lease_next_plan(db, settings, "worker-A")
        db.commit()
        lease_before = plan.lease_until

    with SessionLocal() as db:
        plan = db.get(OwnerBootstrap, seed["owner_id"])
        plan.lease_until = datetime.now(UTC) + timedelta(seconds=1)
        db.commit()

    with SessionLocal() as db:
        plan = db.get(OwnerBootstrap, seed["owner_id"])
        _checkpoint(
            db, settings, plan, progress=0.25, stage="正在获取门店清单", worker_id="worker-A"
        )
    with SessionLocal() as db:
        plan = db.get(OwnerBootstrap, seed["owner_id"])
        assert plan.progress == 0.25
        assert plan.stage == "正在获取门店清单"
        assert plan.lease_until > datetime.now(UTC) + timedelta(
            seconds=settings.bootstrap_lease_seconds - 60
        )
        assert plan.lease_until > lease_before

    # 租约已被 B 接管时，A 的检查点必须抛错且不覆盖进度。
    with SessionLocal() as db:
        plan = db.get(OwnerBootstrap, seed["owner_id"])
        plan.leased_by = "worker-B"
        db.commit()
    with SessionLocal() as db:
        plan = db.get(OwnerBootstrap, seed["owner_id"])
        with pytest.raises(BootstrapLeaseLost):
            _checkpoint(
                db, settings, plan, progress=0.9, stage="被偷走的进度", worker_id="worker-A"
            )
        db.rollback()
    with SessionLocal() as db:
        plan = db.get(OwnerBootstrap, seed["owner_id"])
        assert plan.progress == 0.25
        assert plan.stage == "正在获取门店清单"
