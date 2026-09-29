"""Structure handling and change detection rules."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.models import ChangeEvent, Observation, ObservationLatest
from app.services.observations import _detect_events, persist_observations


class _FakeSession:
    """Minimal stand-in for the SQLAlchemy session used by the diff helpers."""

    def __init__(self, latest: ObservationLatest | None = None) -> None:
        self.added: list[object] = []
        self._latest = latest
        self._lookups = 0

    def add(self, obj: object) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        return None

    def execute(self, _stmt):  # noqa: ANN001 - duck-typed for the test
        class _Result:
            def __init__(self, value):  # noqa: ANN001
                self._value = value

            def scalar_one_or_none(self):  # noqa: ANN201
                return self._value

        self._lookups += 1
        # The first lookup is the ObservationLatest row; later lookups (for
        # example the STRUCTURE_CHANGED dedupe check) find nothing.
        return _Result(self._latest if self._lookups == 1 else None)


def _observation(
    availability: str,
    total: float | None,
    when: datetime,
    store_id,
    style_id,
    *,
    gtin: str = "0001",
) -> Observation:
    return Observation(
        store_id=store_id,
        style_id=style_id,
        size_label="42",
        gtin=gtin,
        availability=availability,
        trust="TRUSTED",
        price_status="OK" if total is not None else "MISSING",
        total_price=total,
        promotions=[],
        checked_at=when,
    )


def _latest(
    availability: str,
    total: float | None,
    when: datetime,
    store_id,
    style_id,
    *,
    gtin: str = "0001",
) -> ObservationLatest:
    return ObservationLatest(
        store_id=store_id,
        style_id=style_id,
        size_label="42",
        gtin=gtin,
        availability=availability,
        trust="TRUSTED",
        price_status="OK",
        total_price=total,
        baseline_ready=True,
        checked_at=when,
    )


def test_restock_detected_only_on_trusted_transition() -> None:
    now = datetime.now(UTC)
    store_id, style_id = uuid.uuid4(), uuid.uuid4()
    previous = _latest("OUT_OF_STOCK", None, now - timedelta(minutes=30), store_id, style_id)
    current = _observation("IN_STOCK", None, now, store_id, style_id)
    events = _detect_events(previous, current)
    assert len(events) == 1
    assert events[0].event_type == "RESTOCK"


def test_unknown_never_overwrites_baseline() -> None:
    session = _FakeSession()
    now = datetime.now(UTC)
    observation = _observation("UNKNOWN", None, now, uuid.uuid4(), uuid.uuid4())
    persist_observations(session, [observation])
    assert session.added == [observation], "unknown observations must not create events"


def test_small_price_move_is_ignored_but_large_is_flagged() -> None:
    now = datetime.now(UTC)
    store_id, style_id = uuid.uuid4(), uuid.uuid4()
    previous = _latest("IN_STOCK", 999.0, now - timedelta(minutes=30), store_id, style_id)

    tiny = _observation("IN_STOCK", 998.7, now, store_id, style_id)
    assert [event.event_type for event in _detect_events(previous, tiny)] == []

    large = _observation("IN_STOCK", 700.0, now, store_id, style_id)
    events = _detect_events(previous, large)
    assert [event.event_type for event in events] == ["PRICE_CHANGE"]


def test_event_payload_is_idempotent_for_same_transition() -> None:
    now = datetime.now(UTC)
    store_id, style_id = uuid.uuid4(), uuid.uuid4()
    previous = _latest("OUT_OF_STOCK", None, now - timedelta(minutes=30), store_id, style_id)
    current = _observation("IN_STOCK", None, now, store_id, style_id)
    first = _detect_events(previous, current)[0]
    second = _detect_events(previous, current)[0]
    assert first.dedupe_key == second.dedupe_key
    assert isinstance(first, ChangeEvent)


def test_long_gap_rebuilds_baseline_without_events() -> None:
    """A gap longer than max_gap makes the two rounds incomparable."""
    now = datetime.now(UTC)
    store_id, style_id = uuid.uuid4(), uuid.uuid4()
    previous = _latest("OUT_OF_STOCK", None, now - timedelta(days=8), store_id, style_id)
    current = _observation("IN_STOCK", None, now, store_id, style_id)
    assert _detect_events(previous, current, max_gap_seconds=7 * 24 * 3600) == []


def test_changed_gtin_for_same_size_never_reports_restock() -> None:
    now = datetime.now(UTC)
    store_id, style_id = uuid.uuid4(), uuid.uuid4()
    previous = _latest(
        "OUT_OF_STOCK", None, now - timedelta(minutes=30), store_id, style_id, gtin="old-gtin"
    )
    current = _observation("IN_STOCK", None, now, store_id, style_id, gtin="new-gtin")
    assert _detect_events(previous, current) == []


def test_structure_change_suppresses_events_and_emits_diagnostic() -> None:
    store_id, style_id = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    previous = _latest("OUT_OF_STOCK", None, now - timedelta(minutes=30), store_id, style_id)
    session = _FakeSession(previous)
    current = _observation("IN_STOCK", None, now, store_id, style_id, gtin="new-gtin")

    summary = persist_observations(
        session, [current], suppress_structures={style_id: "new-hash"}
    )

    assert summary.restock == 0
    events = [obj for obj in session.added if isinstance(obj, ChangeEvent)]
    assert [event.event_type for event in events] == ["STRUCTURE_CHANGED"]
    assert events[0].tenant_id is None


def test_shared_stock_event_has_no_tenant() -> None:
    """Stock transitions belong to the shared observation, not one tenant."""
    store_id, style_id = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    previous = _latest("OUT_OF_STOCK", None, now - timedelta(minutes=30), store_id, style_id)
    session = _FakeSession(previous)
    current = _observation("IN_STOCK", None, now, store_id, style_id)

    summary = persist_observations(session, [current])

    assert summary.restock == 1
    events = [obj for obj in session.added if isinstance(obj, ChangeEvent)]
    assert len(events) == 1
    assert events[0].tenant_id is None
    assert events[0].after_observation_id is None or events[0].after_observation_id == current.id
