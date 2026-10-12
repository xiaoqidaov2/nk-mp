"""Store catalogue sync with regionId de-duplication.

Every row is owned by the Nike binding that fetched it: the catalogue is a
per-user cache, never a shared pool. All reads and writes therefore carry the
owner's user id.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Store, StoreRegionAlias
from app.nike.types import StoreRecord

#: 「换季优惠店」的判据。上游 `concept` 区分不出来（换季优惠店与普通店同为 `NVS`），
#: 唯一可靠信号是门店名带「换季优惠」（2026-10-09 生产库：160 家中 147 家命中，13 家为
#: 体验店/员工店/Jordan/SKIMS 等）。默认只监控与展示这一类门店——判据集中在这里，
#: 将来上游改名/加字段只需改这一处。
CLEARANCE_MARKER = "换季优惠"


def is_clearance_name(name: str | None) -> bool:
    return CLEARANCE_MARKER in (name or "")


def clearance_filter():
    """SQLAlchemy 条件：只要换季优惠店。"""
    return Store.display_name.like(f"%{CLEARANCE_MARKER}%")


def sync_stores(
    session: Session,
    records: list[StoreRecord],
    region_id: str,
    owner_user_id: uuid.UUID,
) -> int:
    """Upsert the owner's stores, merging duplicate regionIds into one storeCode."""
    now = datetime.now(UTC)
    touched = 0
    by_code: dict[str, StoreRecord] = {}
    for record in records:
        existing = by_code.get(record.store_code)
        if existing is None or (not existing.region_id and record.region_id):
            by_code[record.store_code] = record

    for store_code, record in by_code.items():
        store = session.execute(
            select(Store).where(
                Store.owner_user_id == owner_user_id, Store.store_code == store_code
            )
        ).scalar_one_or_none()
        if store is None:
            store = Store(store_code=store_code, owner_user_id=owner_user_id)
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
            select(StoreRegionAlias).where(
                StoreRegionAlias.owner_user_id == owner_user_id,
                StoreRegionAlias.region_id == record.region_id,
            )
        ).scalar_one_or_none()
        if alias is None:
            session.add(
                StoreRegionAlias(
                    owner_user_id=owner_user_id,
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


def resolve_region(session: Session, store_code: str, owner_user_id: uuid.UUID) -> str | None:
    store = session.execute(
        select(Store).where(Store.owner_user_id == owner_user_id, Store.store_code == store_code)
    ).scalar_one_or_none()
    if store is None:
        return None
    if store.region_id:
        return store.region_id
    alias = session.execute(
        select(StoreRegionAlias).where(StoreRegionAlias.store_id == store.id)
    ).scalars().first()
    return alias.region_id if alias else None
