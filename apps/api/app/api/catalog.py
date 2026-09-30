from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models import GtinStructure, Store, Style, User
from app.schemas import StoreOut, StyleOut

router = APIRouter(tags=["catalog"])


@router.get("/stores", response_model=list[StoreOut])
def list_stores(
    search: str = Query(default="", max_length=80),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[StoreOut]:
    # 目录是连接缓存：只返回本用户自己拉取到的门店。
    stmt = (
        select(Store)
        .where(Store.owner_user_id == user.id, Store.is_active.is_(True))
        .order_by(Store.store_code)
    )
    rows = db.execute(stmt).scalars().all()
    if search:
        needle = search.strip().lower()
        rows = [
            row
            for row in rows
            if needle in row.store_code.lower()
            or needle in row.display_name.lower()
            or needle in row.address.lower()
        ]
    return [StoreOut.model_validate(row) for row in rows]


@router.get("/styles", response_model=list[StyleOut])
def list_styles(
    search: str = Query(default="", max_length=80),
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[StyleOut]:
    rows = db.execute(
        select(Style)
        .where(Style.owner_user_id == user.id)
        .order_by(Style.style_code)
        .limit(limit)
    ).scalars().all()
    if search:
        needle = search.strip().upper()
        rows = [
            row for row in rows if needle in row.style_code or needle in (row.title or "").upper()
        ]
    return [StyleOut.model_validate(row) for row in rows]


@router.get("/styles/{style_code}/structure")
def style_structure(
    style_code: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, object]:
    style = db.execute(
        select(Style).where(Style.owner_user_id == user.id, Style.style_code == style_code.upper())
    ).scalar_one_or_none()
    if style is None:
        return {"style_code": style_code.upper(), "status": "MISSING", "stores": []}
    rows = db.execute(
        select(GtinStructure, Store)
        .join(Store, Store.id == GtinStructure.store_id)
        .where(
            GtinStructure.owner_user_id == user.id,
            GtinStructure.style_id == style.id,
        )
        .order_by(Store.store_code)
    ).all()
    return {
        "style_code": style.style_code,
        "title": style.title,
        "status": "OK" if any(row[0].status == "OK" for row in rows) else "MISSING",
        "stores": [
            {
                "store_code": row[1].store_code,
                "store_name": row[1].display_name,
                "status": row[0].status,
                "fetched_at": row[0].fetched_at,
                "expires_at": row[0].expires_at,
                "sizes": row[0].sizes,
            }
            for row in rows
        ],
    }
