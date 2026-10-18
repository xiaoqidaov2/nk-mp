"""Schedule monitor report (PostgreSQL).

The report is derived in real time from instance heartbeats and job state:
a stale heartbeat is not alive; an overdue task is only reported when it could
actually run (a usable account exists and no job is queued/running).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Credential, JobStatus, MonitorTask, ScanJob, ServiceHeartbeat
from app.services import monitor

from .helpers import make_store, make_style, make_tenant, make_user, store_credential


def _heartbeat_row(service: str, instance_id: str, *, age_seconds: float) -> None:
    with SessionLocal() as db:
        db.add(
            ServiceHeartbeat(
                service=service,
                instance_id=instance_id,
                last_seen_at=datetime.now(UTC) - timedelta(seconds=age_seconds),
                detail={},
            )
        )
        db.commit()


def _seed_task(*, age_minutes: int = 10) -> dict:
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant, email=f"monitor-{uuid.uuid4().hex[:8]}@example.com")
        make_store(db, owner=user, code="S1")
        make_style(db, owner=user, code="DN3707-202")
        task = MonitorTask(
            tenant_id=tenant.id,
            name="迟到任务",
            store_codes=["S1"],
            style_codes=["DN3707-202"],
            all_stores=False,
            cadence_seconds=1800,
            is_active=True,
            next_run_at=datetime.now(UTC) - timedelta(minutes=age_minutes),
            created_by=user.id,
        )
        db.add(task)
        db.commit()
        seed = {"tenant_id": tenant.id, "user_id": user.id, "task_id": task.id}
    seed["binding"] = store_credential(get_settings(), seed["user_id"])
    return seed


def test_stale_scheduler_heartbeat_is_not_alive() -> None:
    settings = get_settings()
    _heartbeat_row("scheduler", "sched-a", age_seconds=120)
    _heartbeat_row("worker", "worker-a", age_seconds=1)
    with SessionLocal() as db:
        report = monitor.build_report(db, settings)
        assert [item["instance_id"] for item in report["scheduler"]] == ["sched-a"]
        assert report["scheduler"][0]["alive"] is False
        assert report["scheduler"][0]["age_seconds"] >= 100
        assert [item["instance_id"] for item in report["workers"]] == ["worker-a"]
        assert report["workers"][0]["alive"] is True
        assert report["workers"][0]["running_jobs"] == 0


def test_overdue_task_is_reported_only_when_it_could_actually_run() -> None:
    settings = get_settings()
    seed = _seed_task()
    with SessionLocal() as db:
        report = monitor.build_report(db, settings)
        assert [item["task_id"] for item in report["overdue_tasks"]] == [str(seed["task_id"])]
        item = report["overdue_tasks"][0]
        assert item["overdue_seconds"] >= 600
        assert item["last_finished_at"] is None
        assert report["cooling_accounts"] == []
        assert report["queue"]["QUEUED"] == 0

    # 唯一账号冷却 → 这是预期暂停：不算迟到，改为出现在冷却账号里。
    until = datetime.now(UTC) + timedelta(minutes=20)
    with SessionLocal() as db:
        record = db.get(Credential, seed["binding"].credential_id)
        record.cooldown_until = until
        record.cooldown_reason = "throttled"
        db.commit()
        report = monitor.build_report(db, settings)
        assert report["overdue_tasks"] == []
        assert len(report["cooling_accounts"]) == 1
        assert report["cooling_accounts"][0]["credential_id"] == str(record.id)
        assert report["cooling_accounts"][0]["reason"] == "throttled"
        assert report["cooling_accounts"][0]["owner_user_id"] == str(seed["user_id"])


def test_task_with_a_queued_job_is_not_overdue() -> None:
    settings = get_settings()
    seed = _seed_task()

    with SessionLocal() as db:
        db.add(
            ScanJob(
                tenant_id=seed["tenant_id"],
                task_id=seed["task_id"],
                kind="MONITOR",
                status=JobStatus.QUEUED.value,
                store_codes=["S1"],
                style_codes=["DN3707-202"],
                total_stores=1,
                owner_user_id=seed["user_id"],
                credential_id=seed["binding"].credential_id,
                credential_generation=seed["binding"].generation,
            )
        )
        db.commit()
        report = monitor.build_report(db, settings)
        assert report["overdue_tasks"] == []
        assert report["queue"]["QUEUED"] == 1


def test_report_degrades_cleanly_with_no_heartbeats_at_all() -> None:
    settings = get_settings()
    with SessionLocal() as db:
        report = monitor.build_report(db, settings)
        assert report["scheduler"] == []
        assert report["workers"] == []
        assert report["overdue_tasks"] == []
        assert report["cooling_accounts"] == []
        assert set(report["queue"]) == {
            "QUEUED",
            "RUNNING",
            "SUCCEEDED",
            "PARTIAL",
            "FAILED",
            "CANCELLED",
        }
        assert db.execute(select(ServiceHeartbeat)).scalars().all() == []


def test_a_running_job_keeps_the_worker_instance_alive() -> None:
    """长作业期间由作业心跳续上节点心跳：执行中的 worker 不会被判成掉线。"""
    from app.services import jobs as jobs_module
    from app.services.jobs import enqueue_job, lease_next_job

    settings = get_settings()
    seed = _seed_task()
    with SessionLocal() as db:
        job = enqueue_job(
            db,
            tenant_id=seed["tenant_id"],
            store_codes=["S1"],
            style_codes=["DN3707-202"],
            owner_user_id=seed["user_id"],
            credential_binding=seed["binding"],
        )
        db.commit()
        job_id = job.id
    with SessionLocal() as db:
        leased = lease_next_job(db, settings, "node-1")
        db.commit()
        assert leased is not None and leased.id == job_id

    # 节点心跳停在 5 分钟前（作业已跑很久）→ 掉线；作业心跳后立刻恢复为在线。
    with SessionLocal() as db:
        db.add(
            ServiceHeartbeat(
                service="worker",
                instance_id="node-1",
                last_seen_at=datetime.now(UTC) - timedelta(seconds=300),
                detail={},
            )
        )
        db.commit()
        report = monitor.build_report(db, settings)
        assert next(i for i in report["workers"] if i["instance_id"] == "node-1")["alive"] is False

    with SessionLocal() as db:
        job = db.get(ScanJob, job_id)
        jobs_module.heartbeat(db, job, settings, "node-1")
        db.commit()

    with SessionLocal() as db:
        report = monitor.build_report(db, settings)
        entry = next(i for i in report["workers"] if i["instance_id"] == "node-1")
        assert entry["alive"] is True
        assert entry["running_jobs"] == 1
        row = db.execute(
            select(ServiceHeartbeat).where(ServiceHeartbeat.instance_id == "node-1")
        ).scalar_one()
        assert row.detail.get("job_id") == str(job_id)


def test_long_dead_instances_drop_out_of_the_report() -> None:
    """掉线实例在窗口内仍可见（alive=False），窗口之后自动退场。

    否则每次发版换代都会在面板上留下永久的"调度心跳超时"幽灵实例（客户实测看到 8 行）。
    """
    settings = get_settings()
    _heartbeat_row("scheduler", "fresh", age_seconds=5)
    _heartbeat_row("scheduler", "stale-in-window", age_seconds=300)
    _heartbeat_row("scheduler", "long-dead", age_seconds=7200)
    with SessionLocal() as db:
        report = monitor.build_report(db, settings)
        rows = {item["instance_id"]: item for item in report["scheduler"]}
        assert set(rows) == {"fresh", "stale-in-window"}
        assert rows["fresh"]["alive"] is True
        assert rows["stale-in-window"]["alive"] is False


def test_stale_heartbeats_are_pruned_beyond_retention() -> None:
    """超过硬保留期的心跳行会被监控服务删除（表按实例累积，必须有人收尸）。"""
    settings = get_settings()
    _heartbeat_row("worker", "keep-me", age_seconds=10)
    _heartbeat_row("worker", "ancient", age_seconds=settings.monitor_heartbeat_retention_seconds + 3600)
    with SessionLocal() as db:
        pruned = monitor.prune_stale_heartbeats(db, settings)
        assert pruned == 1
        remaining = {
            row.instance_id
            for row in db.execute(select(ServiceHeartbeat)).scalars().all()
        }
        assert remaining == {"keep-me"}


def test_report_carries_host_disk_usage_with_a_warning_line() -> None:
    """报告带根分区水位（85% 起 warning）：磁盘写满会让 Postgres 崩溃循环。"""
    settings = get_settings()
    with SessionLocal() as db:
        report = monitor.build_report(db, settings)
    disk = report["disk"]
    assert set(disk) == {"total_gb", "used_gb", "free_gb", "used_percent", "warning"}
    assert 0 <= disk["used_percent"] <= 100
    assert disk["warning"] == (disk["used_percent"] >= 85)
    assert disk["total_gb"] > 0 and disk["free_gb"] >= 0


def test_disk_warning_notifies_ops_admins_at_most_once_per_window() -> None:
    """磁盘水位告警推给管理后台管理员，同类 6 小时内只发一次（避免刷屏）。"""
    from app.models import NotificationChannel, NotificationDelivery, Role
    from app.security import encrypt_secret, secret_fingerprint

    settings = get_settings()
    with SessionLocal() as db:
        tenant = make_tenant(db, is_platform=True)
        admin = make_user(db, tenant, email="disk-ops@example.com", role=Role.OPS_ADMIN.value)
        make_user(db, tenant, email="disk-viewer@example.com", role=Role.TENANT_ADMIN.value)
        db.add(
            NotificationChannel(
                owner_user_id=admin.id,
                platform="custom",
                name="运维告警",
                target_enc=encrypt_secret("json://127.0.0.1:19100/__push"),
                target_fingerprint=secret_fingerprint(
                    "json://127.0.0.1:19100/__push", purpose="push"
                ),
                target_hint="••••push",
                enabled=True,
            )
        )
        db.commit()
        admin_id = admin.id

    with SessionLocal() as db:
        first = monitor.notify_disk_warning(db, settings)
        assert first == 1
        second = monitor.notify_disk_warning(db, settings)
        assert second == 0, "同类告警在 6 小时窗口内不得重复"
        rows = db.execute(select(NotificationDelivery)).scalars().all()
        assert len(rows) == 1
        assert rows[0].owner_user_id == admin_id
        assert rows[0].title == monitor.DISK_NOTICE_TITLE
        assert "磁盘" in rows[0].body
