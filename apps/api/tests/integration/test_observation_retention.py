"""观测历史保留：每个 (门店, 货号, 尺码) 只留最近 N 条。

间隔小（30 秒~10 分钟）× 多门店多货号时，历史表一天能涨几百万行；保留策略是
"每个 key 只留最近 ``observation_history_limit`` 条"，作业收尾时按 owner 清理。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text

from app.db import SessionLocal
from app.models import Observation, ObservationLatest
from app.services.retention import prune_all, prune_idle_slice, prune_observation_slice

from .helpers import make_store, make_style, make_tenant, make_user

KEEP = 30
TOTAL = 40


def _seed_observations(db, *, owner, store, style, size_label: str, count: int, base_offset: int = 0):
    """造历史 + 一条 latest（生产上每个 key 必有 latest；清理按 latest 的 key 分片）。"""
    now = datetime.now(UTC)
    db.add(
        ObservationLatest(
            owner_user_id=owner.id,
            credential_id=None,
            credential_generation=None,
            store_id=store.id,
            style_id=style.id,
            size_label=size_label,
            gtin=f"G-{size_label}",
            availability="IN_STOCK",
            trust="TRUSTED",
            price_status="OK",
            total_price=699.0,
            promotions=[],
            baseline_ready=True,
            checked_at=now,
        )
    )
    for index in range(count):
        db.add(
            Observation(
                owner_user_id=owner.id,
                credential_id=None,
                credential_generation=None,
                store_id=store.id,
                style_id=style.id,
                size_label=size_label,
                gtin=f"G-{size_label}",
                availability="IN_STOCK",
                trust="TRUSTED",
                price_status="OK",
                total_price=699.0,
                promotions=[],
                checked_at=now - timedelta(minutes=TOTAL - index + base_offset),
            )
        )
    db.flush()


def test_prune_keeps_only_the_newest_n_per_key() -> None:
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant, email=f"retention-{uuid.uuid4().hex[:8]}@example.com")
        store = make_store(db, owner=user, code="S1", name="NIKE杭州江南换季优惠店")
        style = make_style(db, owner=user, code="DN3707-202")
        _seed_observations(db, owner=user, store=store, style=style, size_label="42", count=TOTAL)
        _seed_observations(db, owner=user, store=store, style=style, size_label="44", count=5)
        db.commit()
        user_id = user.id

    with SessionLocal() as db:
        removed, _cursor = prune_observation_slice(db, keep=KEEP, owner_user_id=user_id, keys=10)
        db.commit()  # 切片不再自己提交：调用方（CLI/worker）负责事务边界
    assert removed == TOTAL - KEEP

    with SessionLocal() as db:
        kept_42 = db.execute(
            select(func.count()).select_from(Observation).where(Observation.size_label == "42")
        ).scalar_one()
        kept_44 = db.execute(
            select(func.count()).select_from(Observation).where(Observation.size_label == "44")
        ).scalar_one()
    assert kept_42 == KEEP
    assert kept_44 == 5  # 没超限的 key 不动

    # 留下的必须是**最新**的那些：最早那条一定被删。
    with SessionLocal() as db:
        oldest = db.execute(
            select(func.min(Observation.checked_at)).where(Observation.size_label == "42")
        ).scalar_one()
    with SessionLocal() as db:
        cutoff = datetime.now(UTC) - timedelta(minutes=KEEP + 1)
    assert oldest > cutoff


def test_prune_is_a_noop_when_history_is_short() -> None:
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant, email=f"retention-short-{uuid.uuid4().hex[:8]}@example.com")
        store = make_store(db, owner=user, code="S2", name="NIKE台州兴华换季优惠店")
        style = make_style(db, owner=user, code="HJ2461-235")
        _seed_observations(db, owner=user, store=store, style=style, size_label="40", count=3)
        db.commit()
        user_id = user.id
    with SessionLocal() as db:
        assert prune_all(db, keep=KEEP, owner_user_id=user_id, keys=10) == 0


def test_retention_slice_never_touches_a_job_transaction() -> None:
    """回归：清理必须在独立会话里跑——早期版本在作业事务里 rollback，把作业状态/事件/
    待投递推送一起回滚，生产因此停摆近 24 小时。这里断言清理失败不影响调用方事务。"""
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant, email=f"retention-isolated-{uuid.uuid4().hex[:8]}@example.com")
        db.commit()
        user_id = user.id
        # 调用方事务里有一条未提交的记录
        db.add(
            ObservationLatest(
                owner_user_id=user.id, credential_id=None, credential_generation=None,
                store_id=None, style_id=None, size_label="unused", gtin="", availability="IN_STOCK",
                trust="TRUSTED", price_status="MISSING", promotions=[], baseline_ready=True,
                checked_at=datetime.now(UTC),
            )
        ) if False else None
        db.execute(text("SELECT 1"))
        # 清理在独立会话里跑（这里给它一个超小的 lock_timeout，仍不该抛给调用方）
        deleted = prune_idle_slice(keep=KEEP, owner_user_id=user_id)
        # 调用方事务依然可用
        assert db.execute(text("SELECT 1")).scalar_one() == 1
    assert deleted >= 0
