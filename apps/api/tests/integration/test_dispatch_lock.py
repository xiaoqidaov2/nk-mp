"""多 scheduler 派发互斥（PostgreSQL）：advisory lock 抢不到就跳过这一拍。

主备自动接替：不需要 leader 状态机，持有事务级 advisory lock 的实例负责派发，
另一个实例发现锁被占用时直接返回 0，绝不重复派发同一任务。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.config import get_settings
from app.db import SessionLocal
from app.models import MonitorTask, ScanJob
from app.services.scheduler import dispatch_due_tasks

from .helpers import make_store, make_style, make_tenant, make_user, store_credential


def _seed() -> dict:
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant, email=f"lock-{uuid.uuid4().hex[:8]}@example.com")
        make_store(db, owner=user, code="S1")
        make_style(db, owner=user, code="DN3707-202")
        task = MonitorTask(
            tenant_id=tenant.id,
            name="派发互斥任务",
            store_codes=["S1"],
            style_codes=["DN3707-202"],
            all_stores=False,
            cadence_seconds=1800,
            is_active=True,
            next_run_at=datetime.now(UTC),
            created_by=user.id,
        )
        db.add(task)
        db.commit()
        seed = {"tenant_id": tenant.id, "user_id": user.id, "task_id": task.id}
    store_credential(get_settings(), seed["user_id"])
    return seed


def test_second_scheduler_skips_the_tick_while_the_lock_is_held() -> None:
    settings = get_settings()
    _seed()

    holder = SessionLocal()
    other = SessionLocal()
    try:
        # A 持锁（事务级：提交/回滚即释放）。
        acquired = holder.execute(
            select(func.pg_try_advisory_xact_lock(settings.dispatch_lock_key))
        ).scalar()
        assert acquired is True

        assert dispatch_due_tasks(other, settings) == 0
        other.commit()
        with SessionLocal() as db:
            assert db.execute(select(ScanJob)).scalars().all() == []

        # A 提交释放锁 → B 正常派发一次。
        holder.commit()
        assert dispatch_due_tasks(other, settings) == 1
        other.commit()
        with SessionLocal() as db:
            assert db.execute(select(func.count()).select_from(ScanJob)).scalar_one() == 1
    finally:
        holder.close()
        other.close()
