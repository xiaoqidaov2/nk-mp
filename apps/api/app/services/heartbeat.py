"""Service-instance heartbeats (``service_heartbeat`` upsert).

Kept in its own module so the job-execution path can refresh the worker's
instance heartbeat without importing the monitor report (which imports the
scheduler, which imports jobs: a cycle).

One row per ``(service, instance_id)``; the monitor service and
``/ops/scheduler-health`` derive liveness from ``last_seen_at`` in real time.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import ServiceHeartbeat


def heartbeat(
    session: Session,
    *,
    service: str,
    instance_id: str,
    detail: dict | None = None,
) -> None:
    """Upsert this instance's heartbeat (commits its own小事务)."""
    now = datetime.now(UTC)
    payload = dict(detail or {})
    session.execute(
        pg_insert(ServiceHeartbeat)
        .values(
            service=service,
            instance_id=instance_id,
            last_seen_at=now,
            detail=payload,
        )
        .on_conflict_do_update(
            index_elements=["service", "instance_id"],
            set_={"last_seen_at": now, "detail": payload},
        )
    )
    session.commit()
