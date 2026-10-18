from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_session, get_db
from app.models import AuthSession, ChangeEvent, EventType, Store, Style, User
from app.schemas import ChangeEventOut

router = APIRouter(prefix="/changes", tags=["changes"])

#: 变化流水默认列出的**真实变化**；诊断事件（SCAN_FAILED/STRUCTURE_CHANGED）不算变化。
CHANGE_EVENT_TYPES = (
    EventType.RESTOCK.value,
    EventType.SOLD_OUT.value,
    EventType.PRICE_CHANGE.value,
    EventType.NEW_ARRIVAL_AT_STORE.value,
)


@router.get("", response_model=list[ChangeEventOut])
def list_changes(
    event_type: str = Query(default=""),
    days: int = Query(default=7, ge=1, le=90),
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
    payload: tuple[User, AuthSession] = Depends(get_current_session),
) -> list[ChangeEventOut]:
    """Change events of the caller only.

    There is no shared broadcast and no platform-wide exception: a user sees
    exactly the events produced by their own account (historical rows keep
    their original owner; a re-bound account simply starts a new baseline).

    ``event_type`` 为空时只返回**真实变化**（补货/售罄/变价/到店上新）：诊断事件
    （检查失败/尺码表变化）不是"变化"，混在变化流水里会把补货/售罄埋掉——客户实测
    反馈"门店页整页都是检查失败"。要看诊断就显式传 ``event_type=SCAN_FAILED``。
    """
    user = payload[0]
    since = datetime.now(UTC) - timedelta(days=days)
    stmt = (
        select(ChangeEvent, Store, Style)
        .join(Store, Store.id == ChangeEvent.store_id)
        .outerjoin(Style, Style.id == ChangeEvent.style_id)
        .where(
            ChangeEvent.detected_at >= since,
            ChangeEvent.owner_user_id == user.id,
            ChangeEvent.tenant_id == user.tenant_id,
        )
        .order_by(ChangeEvent.detected_at.desc())
        .limit(limit)
    )
    if event_type:
        stmt = stmt.where(ChangeEvent.event_type == event_type.upper())
    else:
        stmt = stmt.where(ChangeEvent.event_type.in_(CHANGE_EVENT_TYPES))
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
