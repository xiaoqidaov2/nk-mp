from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models import GtinStructure, ObservationLatest, Store, Style, User
from app.schemas import MatrixCell, MatrixOut

router = APIRouter(prefix="/matrix", tags=["matrix"])


@router.get("", response_model=MatrixOut)
def get_matrix(
    style_code: str = Query(min_length=3),
    store_codes: str = Query(default="", description="comma separated store codes"),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> MatrixOut:
    code = style_code.strip().upper()
    style = db.execute(select(Style).where(Style.style_code == code)).scalar_one_or_none()
    if style is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "货号不存在或尚未采集")

    requested = [item.strip() for item in store_codes.split(",") if item.strip()]
    store_stmt = select(Store).where(Store.is_active.is_(True))
    if requested:
        store_stmt = store_stmt.where(Store.store_code.in_(requested))
    stores = list(db.execute(store_stmt.order_by(Store.store_code)).scalars())
    store_ids = [store.id for store in stores]

    rows = db.execute(
        select(ObservationLatest).where(
            ObservationLatest.style_id == style.id,
            ObservationLatest.store_id.in_(store_ids) if store_ids else False,
        )
    ).scalars().all()

    structures = db.execute(
        select(GtinStructure).where(
            GtinStructure.style_id == style.id,
            GtinStructure.store_id.in_(store_ids) if store_ids else False,
        )
    ).scalars().all()
    structure_status = "OK" if any(item.status == "OK" for item in structures) else "MISSING"
    if not structures:
        structure_status = "MISSING"

    size_order: list[str] = []
    for structure in structures:
        for entry in structure.sizes or []:
            label = str(entry.get("size_label") or "")
            if label and label not in size_order:
                size_order.append(label)
    for row in rows:
        if row.size_label and row.size_label not in size_order:
            size_order.append(row.size_label)

    store_lookup = {store.id: store for store in stores}
    cells: list[MatrixCell] = []
    last_checked = None
    for row in rows:
        store = store_lookup.get(row.store_id)
        if store is None:
            continue
        if last_checked is None or (row.checked_at and row.checked_at > last_checked):
            last_checked = row.checked_at
        cells.append(
            MatrixCell(
                store_code=store.store_code,
                store_name=store.display_name,
                size_label=row.size_label,
                availability=row.availability,
                trust=row.trust,
                price_status=row.price_status,
                price=float(row.price) if row.price is not None else None,
                total_price=float(row.total_price) if row.total_price is not None else None,
                promotions=list(row.promotions or []),
                error_code=row.error_code,
                checked_at=row.checked_at,
            )
        )

    return MatrixOut(
        style_code=style.style_code,
        title=style.title,
        image_url=style.image_url,
        sizes=size_order,
        stores=[
            {"store_code": store.store_code, "store_name": store.display_name}
            for store in stores
        ],
        cells=cells,
        structure_status=structure_status,
        last_checked_at=last_checked,
    )
