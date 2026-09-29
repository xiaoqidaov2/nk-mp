from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db, tenant_scope
from app.models import ChangeEvent, Store, Style, User
from app.schemas import ChangeEventOut

router = APIRouter(prefix="/changes", tags=["changes"])


@router.get("", response_model=list[ChangeEventOut])
def list_changes(
    event_type: str = Query(default=""),
    days: int = Query(default=7, ge=1, le=90),
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ChangeEventOut]:
    tenant, sees_all = tenant_scope(user, db)
    since = datetime.now(UTC) - timedelta(days=days)
    stmt = (
        select(ChangeEvent, Store, Style)
        .join(Store, Store.id == ChangeEvent.store_id)
        .outerjoin(Style, Style.id == ChangeEvent.style_id)
        .where(ChangeEvent.detected_at >= since)
        .order_by(ChangeEvent.detected_at.desc())
        .limit(limit)
    )
    if not sees_all:
        # Shared observation events (tenant_id is NULL) plus this tenant's
        # private events (for example scan failures). Another tenant's private
        # events are never returned.
        stmt = stmt.where(
            or_(ChangeEvent.tenant_id.is_(None), ChangeEvent.tenant_id == tenant.id)
        )
    if event_type:
        stmt = stmt.where(ChangeEvent.event_type == event_type.upper())
    rows = db.execute(stmt).all()
    return [
        ChangeEventOut(
            id=event.id,
            event_type=event.event_type,
            store_code=store.store_code,
            store_name=store.display_name,
            style_code=style.style_code if style else "",
            style_title=style.title if style else "",
            size_label=event.size_label,
            before_status=event.before_status,
            after_status=event.after_status,
            before_total_price=float(event.before_total_price)
            if event.before_total_price is not None
            else None,
            after_total_price=float(event.after_total_price)
            if event.after_total_price is not None
            else None,
            promotions=list(event.promotions or []),
            checked_at=event.checked_at,
            detected_at=event.detected_at,
        )
        for event, store, style in rows
    ]
