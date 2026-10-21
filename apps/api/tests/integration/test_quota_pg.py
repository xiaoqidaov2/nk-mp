"""T07: quota metering and hard caps against real PostgreSQL.

Concurrency, row locking and cross-day boundaries cannot be verified with a
fake session, so these tests run on a real database.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import PlatformUsageDaily, PlatformUsageHourly, UsageDaily
from app.nike.client import NikeClient
from app.services import quota as quota_module
from app.services.quota import QuotaGuard

from .helpers import login, make_tenant, make_user


@pytest.fixture
def tenants():
    with SessionLocal() as db:
        tenant_a = make_tenant(db, name="甲", budget=3)
        tenant_b = make_tenant(db, name="乙", budget=100)
        db.commit()
        return {"a": tenant_a.id, "b": tenant_b.id}


def _tenant_row(tenant_id) -> UsageDaily:
    with SessionLocal() as db:
        return db.execute(
            select(UsageDaily).where(UsageDaily.tenant_id == tenant_id)
        ).scalar_one()


def test_tenant_bucket_is_shared_across_kinds(tenants) -> None:
    """Availability and price traffic draw from one tenant budget."""
    guard = QuotaGuard(tenants["a"], global_budget=100, tenant_budget=3, hourly_cap=100)
    with SessionLocal() as db:
        assert guard.consume_transaction(db, "availability") is True
        assert guard.consume_transaction(db, "availability") is True
        assert guard.consume_transaction(db, "price") is True
        assert guard.consume_transaction(db, "price") is False
    row = _tenant_row(tenants["a"])
    assert row.review_requests == 2
    assert row.price_requests == 1
    with SessionLocal() as db:
        assert db.execute(select(PlatformUsageDaily)).scalar_one().transactions == 3


def test_global_daily_gate_blocks_every_tenant(tenants) -> None:
    guard_a = QuotaGuard(tenants["a"], global_budget=2, tenant_budget=50, hourly_cap=50)
    guard_b = QuotaGuard(tenants["b"], global_budget=2, tenant_budget=50, hourly_cap=50)
    with SessionLocal() as db:
        assert guard_a.consume_transaction(db) is True
        assert guard_b.consume_transaction(db) is True
        assert guard_a.consume_transaction(db) is False
        assert guard_b.consume_transaction(db) is False
    with SessionLocal() as db:
        assert db.execute(select(PlatformUsageDaily)).scalar_one().transactions == 2


def test_hourly_cap_blocks_before_the_daily_limit(tenants) -> None:
    guard = QuotaGuard(tenants["b"], global_budget=1000, tenant_budget=1000, hourly_cap=2)
    with SessionLocal() as db:
        assert guard.consume_transaction(db) is True
        assert guard.consume_transaction(db) is True
        assert guard.consume_transaction(db) is False
    with SessionLocal() as db:
        assert db.execute(select(PlatformUsageHourly)).scalar_one().transactions == 2


def test_hourly_cap_scales_with_the_night_window(tenants) -> None:
    """夜间降速：小时上限按比例压低（8 × 0.25 = 2 次）。"""
    guard = QuotaGuard(
        tenants["b"],
        global_budget=1000,
        tenant_budget=1000,
        hourly_cap=8,
        cap_scale=lambda: 0.25,
    )
    with SessionLocal() as db:
        assert guard.consume_transaction(db) is True
        assert guard.consume_transaction(db) is True
        assert guard.consume_transaction(db) is False
        assert guard.last_rejection == "platform_hourly_budget"


def test_concurrent_consumers_never_exceed_the_budget(tenants) -> None:
    budget = 10
    successes: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        guard = QuotaGuard(
            tenants["a"], global_budget=1000, tenant_budget=budget, hourly_cap=1000
        )
        for _ in range(5):
            with SessionLocal() as db:
                ok = guard.consume_transaction(db)
            if ok:
                with lock:
                    successes.append(1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(successes) == budget
    row = _tenant_row(tenants["a"])
    assert row.review_requests + row.price_requests == budget
    with SessionLocal() as db:
        assert db.execute(select(PlatformUsageDaily)).scalar_one().transactions == budget


def test_new_day_resets_the_tenant_bucket(tenants, monkeypatch) -> None:
    guard = QuotaGuard(tenants["a"], global_budget=1000, tenant_budget=1, hourly_cap=1000)
    with SessionLocal() as db:
        assert guard.consume_transaction(db) is True
        assert guard.consume_transaction(db) is False

    tomorrow = datetime.now(UTC) + timedelta(days=1)
    monkeypatch.setattr(
        quota_module, "_today", lambda: tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
    )
    monkeypatch.setattr(
        quota_module, "_hour", lambda: tomorrow.replace(minute=0, second=0, microsecond=0)
    )
    with SessionLocal() as db:
        assert guard.consume_transaction(db) is True


def test_client_counts_every_attempt_including_retries(tenants) -> None:
    """到达业务层的应答（含 401 重试）每次都计费；只有 405 挑战不计。"""
    settings = get_settings()
    guard = QuotaGuard(tenants["b"], global_budget=100, tenant_budget=100, hourly_cap=100)
    responses = [
        httpx.Response(401, json={"error": "unauthorized"}),
        httpx.Response(202, json={}),
        httpx.Response(
            200,
            json={"response": {"fulfillmentGroups": [{"items": [{"skuId": "G1", "itemCosts": {}}]}]}},
        ),
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    with SessionLocal() as db:
        client = NikeClient(
            settings,
            "old-token",
            token_provider=lambda rejected: "new-token",
            sleep=lambda _seconds: None,
            transport=httpx.MockTransport(handler),
            transaction_listener=lambda kind, charge: guard.consume_transaction(db, kind, charge=charge),
        )
        batch = client.review(["G1"], region_id="R1")
        client.close()
    assert batch.trusted is True
    with SessionLocal() as db:
        assert db.execute(select(PlatformUsageDaily)).scalar_one().transactions == 3


def test_dashboard_reports_total_transactions(client) -> None:
    """The overview shows availability + price consumption from one bucket."""
    with SessionLocal() as db:
        tenant = make_tenant(db)
        make_user(db, tenant, email="dash@example.com")
        day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        db.add(UsageDaily(tenant_id=tenant.id, day=day, review_requests=3, price_requests=17))
        db.commit()

    login(client, "dash@example.com")
    body = client.get("/api/v1/dashboard").json()
    assert body["review_requests_today"] == 20
    assert body["daily_review_budget"] == 5000
