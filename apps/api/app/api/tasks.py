from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.api.deps import get_current_session, get_db, require_write, settings_dep
from app.api.http_errors import credential_error_response, task_name_conflict_response
from app.config import Settings
from app.models import AuthSession, MonitorTask, Store, Style, User
from app.schemas import TaskIn, TaskOut, TaskUpdate
from app.services import throttle
from app.services.credentials import CredentialError, CredentialService

router = APIRouter(prefix="/tasks", tags=["tasks"])


def _to_out(task: MonitorTask, cooldown: throttle.Cooldown | None = None) -> TaskOut:
    return TaskOut(
        id=task.id,
        name=task.name,
        store_codes=list(task.store_codes or []),
        style_codes=list(task.style_codes or []),
        size_labels=list(task.size_labels or []),
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
        cooldown_until=cooldown.until if cooldown is not None else None,
        cooldown_reason=cooldown.reason if cooldown is not None else None,
    )


def _load(db: Session, user: User, task_id: UUID) -> MonitorTask:
    """Tasks belong to their creator: tenant + owner are both required."""
    task = db.execute(
        select(MonitorTask).where(
            MonitorTask.id == task_id,
            MonitorTask.tenant_id == user.tenant_id,
            MonitorTask.created_by == user.id,
        )
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    return task


@router.get("", response_model=list[TaskOut])
def list_tasks(
    db: Session = Depends(get_db),
    payload: tuple[User, AuthSession] = Depends(get_current_session),
) -> list[TaskOut]:
    user = payload[0]
    rows = db.execute(
        select(MonitorTask)
        .where(
            MonitorTask.tenant_id == user.tenant_id,
            MonitorTask.created_by == user.id,
        )
        .order_by(MonitorTask.created_at.desc())
    ).scalars().all()
    # 冷却属于账号（不是单个任务）：一用户多账号时，只要还有可用账号就不显示冷却；
    # 全部不可用才给出最早结束的窗口。一次查询复用给整个列表。
    cooldown = throttle.owner_cooldowns(db, [user.id]).get(user.id)
    return [_to_out(row, cooldown) for row in rows]


@router.post("", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
def create_task(
    payload: TaskIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
    settings: Settings = Depends(settings_dep),
):
    if not payload.style_codes:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "请至少提供一个货号")
    try:
        CredentialService(settings, user.id).binding()
    except CredentialError as exc:
        return credential_error_response(exc)
    store_codes = list(payload.store_codes)
    if store_codes:
        known = set(
            db.execute(
                select(Store.store_code).where(
                    Store.owner_user_id == user.id, Store.store_code.in_(store_codes)
                )
            ).scalars()
        )
        unknown = [code for code in store_codes if code not in known]
        if unknown:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"以下门店不存在：{'、'.join(unknown[:5])}",
            )
    for code in payload.style_codes:
        existing = db.execute(
            select(Style).where(Style.owner_user_id == user.id, Style.style_code == code)
        ).scalar_one_or_none()
        if existing is None:
            db.add(Style(style_code=code, title=code, owner_user_id=user.id))
    task = MonitorTask(
        tenant_id=user.tenant_id,
        name=payload.name.strip(),
        store_codes=store_codes,
        style_codes=payload.style_codes,
        size_labels=list(payload.size_labels),
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
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        detail = str(getattr(exc, "orig", exc))
        if "uq_monitor_task_owner_name" in detail or "monitor_task.created_by" in detail:
            return task_name_conflict_response()
        raise
    return _to_out(task, throttle.owner_cooldowns(db, [user.id]).get(user.id))


@router.get("/{task_id}", response_model=TaskOut)
def get_task(
    task_id: UUID,
    db: Session = Depends(get_db),
    payload: tuple[User, AuthSession] = Depends(get_current_session),
) -> TaskOut:
    user = payload[0]
    return _to_out(_load(db, user, task_id), throttle.owner_cooldowns(db, [user.id]).get(user.id))


@router.patch("/{task_id}", response_model=TaskOut)
def update_task(
    task_id: UUID,
    payload: TaskUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
    settings: Settings = Depends(settings_dep),
):
    task = _load(db, user, task_id)
    if payload.is_active is True and not task.is_active:
        # Enabling a task will start scans with this user's own account.
        try:
            CredentialService(settings, user.id).binding()
        except CredentialError as exc:
            return credential_error_response(exc)
    if payload.name is not None:
        task.name = payload.name.strip()
    if payload.cadence_seconds is not None:
        task.cadence_seconds = payload.cadence_seconds
    if payload.is_active is not None:
        task.is_active = payload.is_active
        task.next_run_at = datetime.now(UTC) if payload.is_active else None
    if payload.store_codes is not None:
        # 空列表表示"全部门店"：与创建语义一致，置空并打开 all_stores。
        task.store_codes = list(payload.store_codes)
        task.all_stores = not payload.store_codes
    if payload.size_labels is not None:
        # 空列表表示"全部尺码"，直接存空集即可（探针按非空集合过滤）。
        task.size_labels = list(payload.size_labels)
    if payload.notify_on_restock is not None:
        task.notify_on_restock = payload.notify_on_restock
    if payload.notify_on_sold_out is not None:
        task.notify_on_sold_out = payload.notify_on_sold_out
    if payload.notify_on_price_change is not None:
        task.notify_on_price_change = payload.notify_on_price_change
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        detail = str(getattr(exc, "orig", exc))
        if "uq_monitor_task_owner_name" in detail or "monitor_task.created_by" in detail:
            return task_name_conflict_response()
        raise
    except StaleDataError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在") from exc
    return _to_out(task, throttle.owner_cooldowns(db, [user.id]).get(user.id))


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(
    task_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> None:
    task = _load(db, user, task_id)
    db.delete(task)
    # Commit before the 204: the row must already be gone for the next read.
    # 并发删除只会少删一行（SQLAlchemy 对 0 行 DELETE 仅告警），删除本身就是幂等的。
    db.commit()
