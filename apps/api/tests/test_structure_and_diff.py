"""Structure handling and change detection rules."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.models import ChangeEvent, Observation, ObservationLatest, User
from app.services.credentials import CredentialBinding
from app.services.observations import _detect_events, persist_observations


class _ScalarResult:
    def __init__(self, value):  # noqa: ANN001 - duck-typed for the test
        self._value = value

    def scalar_one_or_none(self):  # noqa: ANN201
        return self._value


class _FakeSession:
    """Minimal stand-in for the SQLAlchemy session used by the diff helpers.

    ``persist_observations`` queries three different entities, so the fake
    dispatches on the selected entity instead of counting calls: first the
    owner's ``User.tenant_id``, then the stored ``ObservationLatest`` row per
    observation, then the ``ChangeEvent`` dedupe lookup. The dispatch keeps
    working for any number of observations.
    """

    def __init__(
        self, latest: ObservationLatest | None = None, *, tenant_id=None  # noqa: ANN001
    ) -> None:
        self.added: list[object] = []
        self._latest = latest
        self._tenant_id = tenant_id

    def add(self, obj: object) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        return None

    def execute(self, stmt):  # noqa: ANN001 - duck-typed for the test
        entity = stmt.column_descriptions[0].get("entity")
        if entity is User:
            return _ScalarResult(self._tenant_id)
        if entity is ObservationLatest:
            return _ScalarResult(self._latest)
        return _ScalarResult(None)


def _binding(*, generation: int = 1, **overrides):  # noqa: ANN003
    fields: dict = {
        "owner_user_id": uuid.uuid4(),
        "credential_id": uuid.uuid4(),
        "generation": generation,
    }
    fields.update(overrides)
    return CredentialBinding(**fields)


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
    credential_id=None,  # noqa: ANN001
    generation: int = 1,
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
        credential_id=credential_id,
        credential_generation=generation,
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
    persist_observations(session, [observation], binding=_binding())
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
    binding = _binding(generation=2)
    previous = _latest(
        "OUT_OF_STOCK",
        None,
        now - timedelta(minutes=30),
        store_id,
        style_id,
        credential_id=binding.credential_id,
        generation=binding.generation,
    )
    tenant_id = uuid.uuid4()
    session = _FakeSession(previous, tenant_id=tenant_id)
    current = _observation("IN_STOCK", None, now, store_id, style_id, gtin="new-gtin")

    summary = persist_observations(
        session, [current], binding=binding, suppress_structures={style_id: "new-hash"}
    )

    assert summary.restock == 0
    events = [obj for obj in session.added if isinstance(obj, ChangeEvent)]
    assert [event.event_type for event in events] == ["STRUCTURE_CHANGED"]
    assert events[0].tenant_id == tenant_id
    assert events[0].owner_user_id == binding.owner_user_id
    assert events[0].credential_id == binding.credential_id
    assert events[0].credential_generation == binding.generation


def test_stock_event_is_owned_by_tenant_and_binding() -> None:
    """Events are personal: they carry the owner, binding and its tenant."""
    store_id, style_id = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    binding = _binding(generation=4)
    previous = _latest(
        "OUT_OF_STOCK",
        None,
        now - timedelta(minutes=30),
        store_id,
        style_id,
        credential_id=binding.credential_id,
        generation=binding.generation,
    )
    tenant_id = uuid.uuid4()
    session = _FakeSession(previous, tenant_id=tenant_id)
    current = _observation("IN_STOCK", None, now, store_id, style_id)

    summary = persist_observations(session, [current], binding=binding)

    assert summary.restock == 1
    events = [obj for obj in session.added if isinstance(obj, ChangeEvent)]
    assert len(events) == 1
    event = events[0]
    assert event.event_type == "RESTOCK"
    assert event.tenant_id == tenant_id
    assert event.owner_user_id == binding.owner_user_id
    assert event.credential_id == binding.credential_id
    assert event.credential_generation == binding.generation
    assert event.after_observation_id is None or event.after_observation_id == current.id


def test_older_observation_never_clobbers_a_newer_baseline() -> None:
    """Rounds must be applied in checked_at order, regardless of arrival."""
    store_id, style_id = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    binding = _binding()
    previous = _latest(
        "IN_STOCK",
        999.0,
        now,
        store_id,
        style_id,
        credential_id=binding.credential_id,
        generation=binding.generation,
    )
    session = _FakeSession(previous, tenant_id=uuid.uuid4())
    stale = _observation("OUT_OF_STOCK", None, now - timedelta(minutes=5), store_id, style_id)

    summary = persist_observations(session, [stale], binding=binding)

    assert summary.sold_out == 0
    assert [obj for obj in session.added if isinstance(obj, ChangeEvent)] == []
    assert previous.availability == "IN_STOCK"
    assert previous.checked_at == now
