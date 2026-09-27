"""Store catalogue sync with regionId de-duplication."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Store, StoreRegionAlias
from app.nike.types import StoreRecord


def sync_stores(session: Session, records: list[StoreRecord], region_id: str) -> int:
    """Upsert stores, merging duplicate regionIds into one storeCode."""
    now = datetime.now(UTC)
    touched = 0
    by_code: dict[str, StoreRecord] = {}
    for record in records:
        existing = by_code.get(record.store_code)
        if existing is None or (not existing.region_id and record.region_id):
            by_code[record.store_code] = record

    for store_code, record in by_code.items():
        store = session.execute(
            select(Store).where(Store.store_code == store_code)
        ).scalar_one_or_none()
        if store is None:
            store = Store(store_code=store_code)
            session.add(store)
        store.display_name = record.display_name or store.display_name
        store.address = record.address or store.address
        store.concept = record.concept or store.concept
        if record.region_id:
            store.region_id = record.region_id
        store.is_active = True
        store.last_seen_at = now
        store.updated_at = now
        session.flush()

        alias = session.execute(
            select(StoreRegionAlias).where(StoreRegionAlias.region_id == record.region_id)
        ).scalar_one_or_none()
        if alias is None:
            session.add(
                StoreRegionAlias(
                    store_id=store.id,
                    region_id=record.region_id,
                    source=region_id,
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
        else:
            alias.last_seen_at = now
        touched += 1

    session.flush()
    return touched


def resolve_region(session: Session, store_code: str) -> str | None:
    store = session.execute(
        select(Store).where(Store.store_code == store_code)
    ).scalar_one_or_none()
    if store is None:
        return None
    if store.region_id:
        return store.region_id
    alias = session.execute(
        select(StoreRegionAlias).where(StoreRegionAlias.store_id == store.id)
    ).scalars().first()
    return alias.region_id if alias else None
