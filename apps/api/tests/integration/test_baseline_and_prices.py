"""T06: baseline protection and price honesty."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import ChangeEvent, Observation, ObservationLatest, Trust
from app.services.observations import persist_observations

from .helpers import make_store, make_style, make_tenant

SEVEN_DAYS = 7 * 24 * 3600


@pytest.fixture
def world():
    with SessionLocal() as db:
        tenant = make_tenant(db)
        store = make_store(db, code="S001")
        style = make_style(db, code="DN3707-202")
        db.commit()
        return {"tenant_id": tenant.id, "store_id": store.id, "style_id": style.id}


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
        summary = persist_observations(db, [_observation(world, **kwargs)], max_gap_seconds=SEVEN_DAYS)
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
            max_gap_seconds=SEVEN_DAYS,
            suppress_structures={world["style_id"]: "new-hash"},
        )
        db.commit()
    assert summary.restock == 0
    events = _events()
    assert [event.event_type for event in events] == ["STRUCTURE_CHANGED"]


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
