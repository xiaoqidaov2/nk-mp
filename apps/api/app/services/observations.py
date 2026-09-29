"""Observation persistence, monotonic latest-state updates and diffing.

Stock state is a shared platform fact: change events produced from trusted
observations are stored with ``tenant_id = NULL`` so every subscribed tenant
sees the same transition. Tenant-private events (for example a scan failure)
keep a tenant id and are only visible to that tenant.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

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
    observations: list[Observation],
    *,
    max_gap_seconds: int = 7 * 24 * 3600,
    suppress_structures: dict | None = None,
) -> DiffSummary:
    """Persist observations and derive change events.

    ``max_gap_seconds`` protects the baseline: after a longer gap the round
    only rebuilds the baseline and never reports restock/sold-out.
    ``suppress_structures`` maps ``style_id -> new structure hash`` for stores
    whose size structure changed this round; those styles only rebuild their
    baseline as well and record a single diagnostic STRUCTURE_CHANGED event.
    """
    summary = DiffSummary()
    suppressed = suppress_structures or {}
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
                    last_observation_id=observation.id,
                    checked_at=observation.checked_at,
                )
            )
            session.flush()
            continue

        if latest.checked_at is not None and observation.checked_at <= latest.checked_at:
            # Older rounds must never clobber newer state.
            continue

        events: list[ChangeEvent] = []
        if observation.style_id not in suppressed:
            events = _detect_events(latest, observation, max_gap_seconds)
        for event in events:
            # Shared observation fact: visible to every tenant.
            event.tenant_id = None
            event.before_observation_id = latest.last_observation_id
            event.after_observation_id = observation.id
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
        else:
            # Losing stock or losing price visibility invalidates the cached
            # price; a stale value must never masquerade as the current price.
            latest.price = None
            latest.full_price = None
            latest.discount_amount = None
            latest.total_price = None
            latest.promotions = []
            latest.price_status = PriceStatus.MISSING.value
        latest.error_code = observation.error_code
        latest.baseline_ready = True
        latest.last_observation_id = observation.id
        latest.checked_at = observation.checked_at
        session.flush()

    for style_id, structure_hash in suppressed.items():
        store_ids = {item.store_id for item in observations if item.style_id == style_id}
        for store_id in store_ids:
            _record_structure_change(session, store_id, style_id, structure_hash)
    return summary


def _record_structure_change(session: Session, store_id, style_id, structure_hash: str) -> None:
    key = _payload_hash(["STRUCTURE_CHANGED", str(store_id), str(style_id), structure_hash])
    dedupe_key = f"STRUCTURE_CHANGED:{key}"
    existing = session.execute(
        select(ChangeEvent).where(ChangeEvent.dedupe_key == dedupe_key)
    ).scalar_one_or_none()
    if existing is not None:
        return
    session.add(
        ChangeEvent(
            tenant_id=None,
            store_id=store_id,
            style_id=style_id,
            size_label="",
            event_type=EventType.STRUCTURE_CHANGED.value,
            payload_hash=key,
            dedupe_key=dedupe_key,
            checked_at=datetime.now(UTC),
        )
    )
    session.flush()


def _detect_events(
    latest: ObservationLatest,
    observation: Observation,
    max_gap_seconds: int = 7 * 24 * 3600,
) -> list[ChangeEvent]:
    """Detect transitions between two trusted observations.

    Baseline protection: a gap longer than ``max_gap_seconds`` or a changed
    GTIN for the same size means the two observations are not comparable, so
    the round only refreshes the baseline and reports nothing.
    """
    if (
        latest.checked_at is not None
        and observation.checked_at is not None
        and observation.checked_at - latest.checked_at > timedelta(seconds=max_gap_seconds)
    ):
        return []
    if latest.gtin and observation.gtin and latest.gtin != observation.gtin:
        return []

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
    """Record a store-level failure without pretending it is a stock change.

    Scan failures are tenant-private: they describe this tenant's job, not the
    shared store state.
    """
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
