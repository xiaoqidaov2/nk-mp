from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db, require_write
from app.models import MonitorTask, Store, Style, User
from app.schemas import TaskIn, TaskOut, TaskUpdate

router = APIRouter(prefix="/tasks", tags=["tasks"])


def _to_out(task: MonitorTask) -> TaskOut:
    return TaskOut(
        id=task.id,
        name=task.name,
        store_codes=list(task.store_codes or []),
        style_codes=list(task.style_codes or []),
        all_stores=bool(task.all_stores) or not (task.store_codes or []),
        cadence_seconds=task.cadence_seconds,
        is_active=task.is_active,
        notify_on_restock=task.notify_on_restock,
        notify_on_sold_out=task.notify_on_sold_out,
        notify_on_price_change=task.notify_on_price_change,
        next_run_at=task.next_run_at,
        last_run_at=task.last_run_at,
        created_at=task.created_at,
        store_count=len(task.store_codes or []),
        style_count=len(task.style_codes or []),
    )


def _load(db: Session, tenant_id, task_id: UUID) -> MonitorTask:
    task = db.execute(
        select(MonitorTask).where(
            MonitorTask.id == task_id, MonitorTask.tenant_id == tenant_id
        )
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    return task


@router.get("", response_model=list[TaskOut])
def list_tasks(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[TaskOut]:
    rows = db.execute(
        select(MonitorTask)
        .where(MonitorTask.tenant_id == user.tenant_id)
        .order_by(MonitorTask.created_at.desc())
    ).scalars().all()
    return [_to_out(row) for row in rows]


@router.post("", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
def create_task(
    payload: TaskIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> TaskOut:
    if not payload.style_codes:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "请至少提供一个货号")
    store_codes = list(payload.store_codes)
    if store_codes:
        known = set(
            db.execute(select(Store.store_code).where(Store.store_code.in_(store_codes))).scalars()
        )
        unknown = [code for code in store_codes if code not in known]
        if unknown:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"以下门店不存在：{'、'.join(unknown[:5])}",
            )
    for code in payload.style_codes:
        existing = db.execute(
            select(Style).where(Style.style_code == code)
        ).scalar_one_or_none()
        if existing is None:
            db.add(Style(style_code=code, title=code))
    task = MonitorTask(
        tenant_id=user.tenant_id,
        name=payload.name.strip(),
        store_codes=store_codes,
        style_codes=payload.style_codes,
        all_stores=not store_codes,
        cadence_seconds=payload.cadence_seconds,
        is_active=payload.is_active,
        notify_on_restock=payload.notify_on_restock,
        notify_on_sold_out=payload.notify_on_sold_out,
        notify_on_price_change=payload.notify_on_price_change,
        next_run_at=datetime.now(UTC) if payload.is_active else None,
        created_by=user.id,
    )
    db.add(task)
    db.flush()
    return _to_out(task)


@router.get("/{task_id}", response_model=TaskOut)
def get_task(
    task_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TaskOut:
    return _to_out(_load(db, user.tenant_id, task_id))


@router.patch("/{task_id}", response_model=TaskOut)
def update_task(
    task_id: UUID,
    payload: TaskUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> TaskOut:
    task = _load(db, user.tenant_id, task_id)
    if payload.name is not None:
        task.name = payload.name.strip()
    if payload.cadence_seconds is not None:
        task.cadence_seconds = payload.cadence_seconds
    if payload.is_active is not None:
        task.is_active = payload.is_active
        task.next_run_at = datetime.now(UTC) if payload.is_active else None
    if payload.notify_on_restock is not None:
        task.notify_on_restock = payload.notify_on_restock
    if payload.notify_on_sold_out is not None:
        task.notify_on_sold_out = payload.notify_on_sold_out
    if payload.notify_on_price_change is not None:
        task.notify_on_price_change = payload.notify_on_price_change
    db.flush()
    return _to_out(task)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(
    task_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> None:
    task = _load(db, user.tenant_id, task_id)
    db.delete(task)
    db.flush()
