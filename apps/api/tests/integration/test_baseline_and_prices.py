"""T06: baseline protection and price honesty.

Every observation and latest-state row is personal: it carries the owner user
and the exact credential generation that produced it. Baselines are therefore
per-owner; a re-bound account only rebuilds its own baseline.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import ChangeEvent, GtinStructure, Observation, ObservationLatest, Store, Style, Trust
from app.services.observations import persist_observations
from app.services.probe import _ensure_structure_row

from .helpers import make_store, make_style, make_tenant, make_user, store_credential

SEVEN_DAYS = 7 * 24 * 3600


@pytest.fixture
def world():
    settings = get_settings()
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant, email=f"base-{uuid.uuid4().hex[:8]}@example.com")
        store = make_store(db, owner=user, code="S001")
        style = make_style(db, owner=user, code="DN3707-202")
        db.commit()
        world = {
            "tenant_id": tenant.id,
            "store_id": store.id,
            "style_id": style.id,
            "user_id": user.id,
        }
    world["binding"] = store_credential(settings, world["user_id"])
    return world


def _observation(world, *, availability, checked_at, gtin="G1", price=None):
    return Observation(
        store_id=world["store_id"],
        style_id=world["style_id"],
        size_label="42",
        gtin=gtin,
        availability=availability,
        trust=Trust.TRUSTED.value,
        price_status="OK" if price is not None else "MISSING",
        total_price=price,
        price=price,
        promotions=[],
        checked_at=checked_at,
    )


def _persist(world, **kwargs):
    with SessionLocal() as db:
        summary = persist_observations(
            db,
            [_observation(world, **kwargs)],
            binding=world["binding"],
            max_gap_seconds=SEVEN_DAYS,
        )
        db.commit()
        return summary


def _events() -> list[ChangeEvent]:
    with SessionLocal() as db:
        return list(db.execute(select(ChangeEvent)).scalars())


def test_first_collection_only_builds_baseline(world) -> None:
    summary = _persist(world, availability="OUT_OF_STOCK", checked_at=datetime.now(UTC))
    assert summary.restock == summary.sold_out == summary.price_change == 0
    assert _events() == []
    with SessionLocal() as db:
        latest = db.execute(select(ObservationLatest)).scalar_one()
        assert latest.availability == "OUT_OF_STOCK"
        assert latest.baseline_ready is True
        assert latest.owner_user_id == world["user_id"]
        assert latest.credential_generation == world["binding"].generation
        observation = db.execute(select(Observation)).scalar_one()
        assert observation.owner_user_id == world["user_id"]
        assert observation.credential_id == world["binding"].credential_id


def test_gap_longer_than_max_rebuilds_baseline_without_events(world) -> None:
    eight_days_ago = datetime.now(UTC) - timedelta(days=8)
    _persist(world, availability="OUT_OF_STOCK", checked_at=eight_days_ago)
    summary = _persist(world, availability="IN_STOCK", checked_at=datetime.now(UTC))
    assert summary.restock == 0
    assert _events() == [], "a long gap must never look like a restock"
    with SessionLocal() as db:
        latest = db.execute(select(ObservationLatest)).scalar_one()
        assert latest.availability == "IN_STOCK"  # baseline rebuilt silently


def test_gtin_change_is_treated_as_a_new_baseline(world) -> None:
    earlier = datetime.now(UTC) - timedelta(minutes=30)
    _persist(world, availability="OUT_OF_STOCK", checked_at=earlier, gtin="old-gtin")
    summary = _persist(world, availability="IN_STOCK", checked_at=datetime.now(UTC), gtin="new-gtin")
    assert summary.restock == 0
    assert _events() == []


def test_structure_change_round_records_diagnostics_but_no_restock(world) -> None:
    earlier = datetime.now(UTC) - timedelta(minutes=30)
    _persist(world, availability="OUT_OF_STOCK", checked_at=earlier)
    with SessionLocal() as db:
        summary = persist_observations(
            db,
            [_observation(world, availability="IN_STOCK", checked_at=datetime.now(UTC), gtin="new")],
            binding=world["binding"],
            max_gap_seconds=SEVEN_DAYS,
            suppress_structures={world["style_id"]: "new-hash"},
        )
        db.commit()
    assert summary.restock == 0
    events = _events()
    assert [event.event_type for event in events] == ["STRUCTURE_CHANGED"]
    assert events[0].owner_user_id == world["user_id"]
    assert events[0].tenant_id == world["tenant_id"]


def test_failed_round_keeps_the_last_trusted_inventory(world) -> None:
    _persist(world, availability="OUT_OF_STOCK", checked_at=datetime.now(UTC) - timedelta(minutes=30))
    with SessionLocal() as db:
        db.add(
            Observation(
                store_id=world["store_id"],
                style_id=world["style_id"],
                size_label="42",
                gtin="G1",
                availability="UNKNOWN",
                trust=Trust.UNTRUSTED.value,
                price_status="MISSING",
                checked_at=datetime.now(UTC),
            )
        )
        db.commit()
    with SessionLocal() as db:
        latest = db.execute(select(ObservationLatest)).scalar_one()
        assert latest.availability == "OUT_OF_STOCK"
        assert latest.trust == Trust.TRUSTED.value


def test_missing_price_clears_stale_price_and_restores_without_false_change(world) -> None:
    t0 = datetime.now(UTC) - timedelta(minutes=60)
    _persist(world, availability="IN_STOCK", checked_at=t0, price=999.0)
    with SessionLocal() as db:
        assert db.execute(select(ObservationLatest)).scalar_one().total_price == 999.0

    # Price visibility is lost this round: the cached price must not linger.
    _persist(world, availability="IN_STOCK", checked_at=datetime.now(UTC) - timedelta(minutes=30))
    with SessionLocal() as db:
        latest = db.execute(select(ObservationLatest)).scalar_one()
        assert latest.total_price is None
        assert latest.price_status == "MISSING"

    # The same price comes back later: no PRICE_CHANGE should be reported.
    summary = _persist(world, availability="IN_STOCK", checked_at=datetime.now(UTC), price=999.0)
    assert summary.price_change == 0
    with SessionLocal() as db:
        assert db.execute(select(ObservationLatest)).scalar_one().total_price == 999.0


def test_structure_row_creation_survives_a_concurrent_insert(world) -> None:
    """两个 worker 同时抓同一 (owner, store, style) 时，后到的插入不能炸唯一约束。"""
    with SessionLocal() as db:
        store = db.get(Store, world["store_id"])
        style = db.get(Style, world["style_id"])
        first = _ensure_structure_row(db, store, style)
        db.commit()
        first_id = first.id

    with SessionLocal() as db:
        store = db.get(Store, world["store_id"])
        style = db.get(Style, world["style_id"])
        second = _ensure_structure_row(db, store, style)
        db.commit()
        assert second.id == first_id

    with SessionLocal() as db:
        rows = db.execute(select(GtinStructure)).scalars().all()
        assert len(rows) == 1


def test_not_carried_to_in_stock_records_a_first_time_arrival(world) -> None:
    """未铺货 → 有货（新上架）是真实到货：必须落事件，推送侧再按补货开关发出。"""
    _persist(world, availability="NOT_CARRIED", checked_at=datetime.now(UTC) - timedelta(minutes=60))
    summary = _persist(
        world, availability="IN_STOCK", checked_at=datetime.now(UTC), price=899.0
    )
    assert summary.new_arrival == 1
    with SessionLocal() as db:
        event = db.execute(select(ChangeEvent)).scalar_one()
        assert event.event_type == "NEW_ARRIVAL_AT_STORE"
        assert event.before_status == "NOT_CARRIED"
        assert event.after_status == "IN_STOCK"
