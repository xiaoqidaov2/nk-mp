"""Observation persistence, monotonic latest-state updates and diffing."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ChangeEvent, EventType, Observation, ObservationLatest, PriceStatus, Trust

PRICE_EPSILON_ABS = 1.0
PRICE_EPSILON_RATIO = 0.01
NEVER_EVENT_STATES = {"UNKNOWN", "STRUCTURE_MISSING"}


@dataclass
class DiffSummary:
    restock: int = 0
    sold_out: int = 0
    price_change: int = 0
    new_arrival: int = 0


def _payload_hash(parts: list[str]) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:40]


def persist_observations(
    session: Session,
    tenant_id,
    observations: list[Observation],
) -> DiffSummary:
    summary = DiffSummary()
    for observation in observations:
        session.add(observation)
        session.flush()
        if observation.trust != Trust.TRUSTED.value:
            continue
        if observation.availability in NEVER_EVENT_STATES:
            # An unknown or structureless result must not overwrite a trusted
            # baseline, otherwise the next round reports a phantom change.
            continue
        latest = session.execute(
            select(ObservationLatest).where(
                ObservationLatest.store_id == observation.store_id,
                ObservationLatest.style_id == observation.style_id,
                ObservationLatest.size_label == observation.size_label,
            )
        ).scalar_one_or_none()

        if latest is None:
            session.add(
                ObservationLatest(
                    store_id=observation.store_id,
                    style_id=observation.style_id,
                    size_label=observation.size_label,
                    gtin=observation.gtin,
                    availability=observation.availability,
                    trust=observation.trust,
                    price_status=observation.price_status,
                    price=observation.price,
                    full_price=observation.full_price,
                    discount_amount=observation.discount_amount,
                    total_price=observation.total_price,
                    promotions=observation.promotions,
                    error_code=observation.error_code,
                    baseline_ready=True,
                    checked_at=observation.checked_at,
                )
            )
            session.flush()
            continue

        if latest.checked_at is not None and observation.checked_at <= latest.checked_at:
            # Older rounds must never clobber newer state.
            continue

        events = _detect_events(latest, observation)
        for event in events:
            event.tenant_id = tenant_id
            session.add(event)
        if events:
            session.flush()
            for event in events:
                if event.event_type == EventType.RESTOCK.value:
                    summary.restock += 1
                elif event.event_type == EventType.SOLD_OUT.value:
                    summary.sold_out += 1
                elif event.event_type == EventType.PRICE_CHANGE.value:
                    summary.price_change += 1
                elif event.event_type == EventType.NEW_ARRIVAL_AT_STORE.value:
                    summary.new_arrival += 1

        latest.gtin = observation.gtin
        latest.availability = observation.availability
        latest.trust = observation.trust
        latest.price_status = observation.price_status
        if observation.price_status == PriceStatus.OK.value:
            latest.price = observation.price
            latest.full_price = observation.full_price
            latest.discount_amount = observation.discount_amount
            latest.total_price = observation.total_price
            latest.promotions = observation.promotions
        elif observation.availability == "OUT_OF_STOCK":
            # Losing stock also invalidates the cached price.
            latest.price = None
            latest.full_price = None
            latest.discount_amount = None
            latest.total_price = None
            latest.promotions = []
            latest.price_status = PriceStatus.MISSING.value
        latest.error_code = observation.error_code
        latest.baseline_ready = True
        latest.checked_at = observation.checked_at
        session.flush()
    return summary


def _detect_events(latest: ObservationLatest, observation: Observation) -> list[ChangeEvent]:
    before = latest.availability
    after = observation.availability
    events: list[ChangeEvent] = []
    base = [
        str(latest.store_id),
        str(latest.style_id),
        latest.size_label,
        after,
        observation.checked_at.isoformat(),
    ]

    if before == "NOT_CARRIED" and after == "IN_STOCK":
        events.append(
            _event(EventType.NEW_ARRIVAL_AT_STORE, latest, observation, base, before, after)
        )
    elif before == "OUT_OF_STOCK" and after == "IN_STOCK":
        events.append(_event(EventType.RESTOCK, latest, observation, base, before, after))
    elif before == "IN_STOCK" and after == "OUT_OF_STOCK":
        events.append(_event(EventType.SOLD_OUT, latest, observation, base, before, after))

    if after == "IN_STOCK" and observation.total_price is not None and latest.total_price is not None:
        before_price = float(latest.total_price)
        after_price = float(observation.total_price)
        delta = abs(after_price - before_price)
        threshold = max(PRICE_EPSILON_ABS, before_price * PRICE_EPSILON_RATIO)
        if delta >= threshold:
            events.append(
                _event(EventType.PRICE_CHANGE, latest, observation, base, before, after)
            )
    return events


def _event(
    event_type: EventType,
    latest: ObservationLatest,
    observation: Observation,
    base: list[str],
    before: str,
    after: str,
) -> ChangeEvent:
    payload_hash = _payload_hash(base)
    return ChangeEvent(
        store_id=observation.store_id,
        style_id=observation.style_id,
        size_label=observation.size_label,
        event_type=event_type.value,
        before_status=before,
        after_status=after,
        before_total_price=latest.total_price,
        after_total_price=observation.total_price,
        promotions=list(observation.promotions or []),
        payload_hash=payload_hash,
        dedupe_key=f"{event_type.value}:{payload_hash}",
        checked_at=observation.checked_at,
    )


def mark_failed_job_event(session: Session, tenant_id, job, store_id, error_code: str) -> None:
    """Record a store-level failure without pretending it is a stock change."""
    key = _payload_hash(["SCAN_FAILED", str(job.id), str(store_id), error_code])
    existing = session.execute(
        select(ChangeEvent).where(ChangeEvent.dedupe_key == f"SCAN_FAILED:{key}")
    ).scalar_one_or_none()
    if existing is not None:
        return
    session.add(
        ChangeEvent(
            tenant_id=tenant_id,
            store_id=store_id,
            style_id=None,
            size_label="",
            event_type=EventType.SCAN_FAILED.value,
            payload_hash=key,
            dedupe_key=f"SCAN_FAILED:{key}",
            checked_at=datetime.now(UTC),
        )
    )
    session.flush()

