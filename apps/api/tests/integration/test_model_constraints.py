"""T05: data model constraints and event traceability.

PostgreSQL only: foreign keys, unique constraints and JSONB behaviour cannot
be validated with SQLite or a fake session.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.models import (
    ChangeEvent,
    Observation,
    ObservationLatest,
    ScanJobStore,
    StoreJobStatus,
    Trust,
)
from app.services.jobs import enqueue_job
from app.services.observations import mark_failed_job_event, persist_observations

from .helpers import make_store, make_style, make_tenant, make_user


@pytest.fixture
def world():
    with SessionLocal() as db:
        tenant = make_tenant(db)
        store = make_store(db, code="S001")
        style = make_style(db, code="DN3707-202")
        user = make_user(db, tenant, email="owner@example.com")
        db.commit()
        return {
            "tenant_id": tenant.id,
            "store_id": store.id,
            "style_id": style.id,
            "user_id": user.id,
        }


def _observation(world, *, gtin="G1", availability="IN_STOCK", checked_at=None, price=None):
    return Observation(
        store_id=world["store_id"],
        style_id=world["style_id"],
        size_label="42",
        gtin=gtin,
        availability=availability,
        trust=Trust.TRUSTED.value,
        price_status="OK" if price is not None else "MISSING",
        total_price=price,
        promotions=[],
        checked_at=checked_at or datetime.now(UTC),
    )


def test_observation_job_reference_is_foreign_key_enforced(world) -> None:
    with SessionLocal() as db:
        db.add(
            Observation(
                store_id=world["store_id"],
                style_id=world["style_id"],
                size_label="42",
                gtin="G1",
                availability="IN_STOCK",
                trust="TRUSTED",
                price_status="MISSING",
                job_id=uuid.uuid4(),  # no such scan job
                checked_at=datetime.now(UTC),
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_task_creator_reference_is_foreign_key_enforced(world) -> None:
    from app.models import MonitorTask

    with SessionLocal() as db:
        db.add(
            MonitorTask(
                tenant_id=world["tenant_id"],
                name="坏引用",
                style_codes=["DN3707-202"],
                store_codes=[],
                all_stores=True,
                created_by=uuid.uuid4(),
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_scan_job_store_unique_constraint(world) -> None:
    with SessionLocal() as db:
        job = enqueue_job(
            db, tenant_id=world["tenant_id"], store_codes=["S001"], style_codes=["DN3707-202"]
        )
        db.commit()
        db.add(
            ScanJobStore(
                job_id=job.id, store_id=world["store_id"], status=StoreJobStatus.PENDING.value
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_event_dedupe_key_is_unique(world) -> None:
    now = datetime.now(UTC)
    key = f"RESTOCK:{uuid.uuid4()}"
    with SessionLocal() as db:
        for _ in range(2):
            db.add(
                ChangeEvent(
                    store_id=world["store_id"],
                    style_id=world["style_id"],
                    size_label="42",
                    event_type="RESTOCK",
                    dedupe_key=key,
                    checked_at=now,
                )
            )
        with pytest.raises(IntegrityError):
            db.commit()


def test_unknown_stores_are_rejected_at_creation(client, world) -> None:
    from .helpers import auth_headers, login

    csrf = login(client, "owner@example.com")
    response = client.post(
        "/api/v1/tasks",
        json={
            "name": "不存在的门店",
            "store_codes": ["NO-SUCH-STORE"],
            "style_codes": ["DN3707-202"],
        },
        headers=auth_headers(csrf),
    )
    assert response.status_code == 422


def test_stock_events_link_before_and_after_observations(world) -> None:
    earlier = datetime.now(UTC) - timedelta(minutes=30)
    with SessionLocal() as db:
        persist_observations(db, [_observation(world, availability="OUT_OF_STOCK", checked_at=earlier)])
        db.commit()
        source = db.execute(
            select(Observation).order_by(Observation.id.desc()).limit(1)
        ).scalar_one()

    with SessionLocal() as db:
        summary = persist_observations(db, [_observation(world, availability="IN_STOCK")])
        db.commit()
        assert summary.restock == 1
        event = db.execute(select(ChangeEvent)).scalar_one()
        assert event.tenant_id is None
        assert event.before_observation_id == source.id
        assert event.after_observation_id is not None
        latest = db.execute(select(ObservationLatest)).scalar_one()
        assert latest.last_observation_id == event.after_observation_id


def test_out_of_order_observation_does_not_roll_state_back(world) -> None:
    now = datetime.now(UTC)
    with SessionLocal() as db:
        persist_observations(db, [_observation(world, availability="OUT_OF_STOCK", checked_at=now)])
        db.commit()
    with SessionLocal() as db:
        stale = _observation(world, availability="IN_STOCK", checked_at=now - timedelta(hours=1))
        summary = persist_observations(db, [stale])
        db.commit()
        assert summary.restock == 0
        latest = db.execute(select(ObservationLatest)).scalar_one()
        assert latest.availability == "OUT_OF_STOCK"


def test_scan_failure_events_are_private_to_the_tenant(world) -> None:
    with SessionLocal() as db:
        job = enqueue_job(
            db, tenant_id=world["tenant_id"], store_codes=["S001"], style_codes=["DN3707-202"]
        )
        db.commit()
        mark_failed_job_event(
            db, world["tenant_id"], job, world["store_id"], "EMPTY_COMPLETED"
        )
        db.commit()
        event = db.execute(select(ChangeEvent)).scalar_one()
        assert event.tenant_id == world["tenant_id"]
        assert event.event_type == "SCAN_FAILED"
