"""T05: data model constraints and event traceability.

PostgreSQL only: foreign keys, unique constraints and JSONB behaviour cannot
be validated with SQLite or a fake session. Personal ownership is part of the
model now, so latest-state uniqueness is per owner.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
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

from .helpers import make_store, make_style, make_tenant, make_user, store_credential


@pytest.fixture
def world():
    settings = get_settings()
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant, email="owner@example.com")
        store = make_store(db, owner=user, code="S001")
        style = make_style(db, owner=user, code="DN3707-202")
        other = make_user(db, tenant, email="other@example.com")
        db.commit()
        world = {
            "tenant_id": tenant.id,
            "store_id": store.id,
            "style_id": style.id,
            "user_id": user.id,
            "other_user_id": other.id,
        }
    world["binding"] = store_credential(settings, world["user_id"])
    world["other_binding"] = store_credential(settings, world["other_user_id"])
    return world


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


def _latest(world, owner_user_id: uuid.UUID) -> ObservationLatest:
    now = datetime.now(UTC)
    return ObservationLatest(
        owner_user_id=owner_user_id,
        store_id=world["store_id"],
        style_id=world["style_id"],
        size_label="42",
        gtin="G1",
        availability="IN_STOCK",
        trust=Trust.TRUSTED.value,
        price_status="MISSING",
        promotions=[],
        baseline_ready=True,
        checked_at=now,
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
            db,
            tenant_id=world["tenant_id"],
            store_codes=["S001"],
            style_codes=["DN3707-202"],
            owner_user_id=world["user_id"],
            credential_binding=world["binding"],
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


def test_observation_latest_is_unique_per_owner(world) -> None:
    """The same (store, style, size) state coexists across owners but not within one."""
    with SessionLocal() as db:
        db.add(_latest(world, world["user_id"]))
        db.commit()

    with SessionLocal() as db:
        db.add(_latest(world, world["other_user_id"]))
        db.commit()
        owners = set(db.execute(select(ObservationLatest.owner_user_id)).scalars())
        assert owners == {world["user_id"], world["other_user_id"]}

    with SessionLocal() as db:
        db.add(_latest(world, world["user_id"]))
        with pytest.raises(IntegrityError):
            db.commit()


def test_latest_state_is_scoped_to_the_binding_owner(world) -> None:
    now = datetime.now(UTC)
    with SessionLocal() as db:
        persist_observations(
            db,
            [_observation(world, availability="IN_STOCK", checked_at=now)],
            binding=world["binding"],
        )
        db.commit()
    with SessionLocal() as db:
        persist_observations(
            db,
            [_observation(world, availability="OUT_OF_STOCK", checked_at=now)],
            binding=world["other_binding"],
        )
        db.commit()
    with SessionLocal() as db:
        rows = {row.owner_user_id: row for row in db.execute(select(ObservationLatest)).scalars()}
        assert set(rows) == {world["user_id"], world["other_user_id"]}
        assert rows[world["user_id"]].availability == "IN_STOCK"
        assert rows[world["other_user_id"]].availability == "OUT_OF_STOCK"
        assert rows[world["user_id"]].credential_id == world["binding"].credential_id
        assert rows[world["other_user_id"]].credential_id == world["other_binding"].credential_id


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
        persist_observations(
            db,
            [_observation(world, availability="OUT_OF_STOCK", checked_at=earlier)],
            binding=world["binding"],
        )
        db.commit()
        source = db.execute(
            select(Observation).order_by(Observation.id.desc()).limit(1)
        ).scalar_one()

    with SessionLocal() as db:
        summary = persist_observations(
            db, [_observation(world, availability="IN_STOCK")], binding=world["binding"]
        )
        db.commit()
        assert summary.restock == 1
        event = db.execute(select(ChangeEvent)).scalar_one()
        assert event.tenant_id == world["tenant_id"]
        assert event.owner_user_id == world["user_id"]
        assert event.credential_id == world["binding"].credential_id
        assert event.before_observation_id == source.id
        assert event.after_observation_id is not None
        latest = db.execute(select(ObservationLatest)).scalar_one()
        assert latest.owner_user_id == world["user_id"]
        assert latest.last_observation_id == event.after_observation_id


def test_out_of_order_observation_does_not_roll_state_back(world) -> None:
    now = datetime.now(UTC)
    with SessionLocal() as db:
        persist_observations(
            db,
            [_observation(world, availability="OUT_OF_STOCK", checked_at=now)],
            binding=world["binding"],
        )
        db.commit()
    with SessionLocal() as db:
        stale = _observation(world, availability="IN_STOCK", checked_at=now - timedelta(hours=1))
        summary = persist_observations(db, [stale], binding=world["binding"])
        db.commit()
        assert summary.restock == 0
        latest = db.execute(select(ObservationLatest)).scalar_one()
        assert latest.availability == "OUT_OF_STOCK"


def test_scan_failure_events_carry_the_job_owner(world) -> None:
    with SessionLocal() as db:
        job = enqueue_job(
            db,
            tenant_id=world["tenant_id"],
            store_codes=["S001"],
            style_codes=["DN3707-202"],
            owner_user_id=world["user_id"],
            credential_binding=world["binding"],
        )
        db.commit()
        mark_failed_job_event(
            db, world["tenant_id"], job, world["store_id"], "EMPTY_COMPLETED"
        )
        db.commit()
        event = db.execute(select(ChangeEvent)).scalar_one()
        assert event.tenant_id == world["tenant_id"]
        assert event.owner_user_id == world["user_id"]
        assert event.event_type == "SCAN_FAILED"
