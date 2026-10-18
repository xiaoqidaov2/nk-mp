"""Scan worker: lease queued jobs and execute them with heartbeats.

Multiple replicas are supported by construction: ``lease_next_job`` uses
``FOR UPDATE SKIP LOCKED`` plus a ``leased_by``/``lease_expires_at`` lease, and
bootstrap plans are leased the same way (``owner_bootstrap.leased_by``).
"""

from __future__ import annotations

import logging
import signal
import time

from app.config import get_settings
from app.db import SessionLocal
from app.services.bootstrap import lease_next_plan, run_plan
from app.services.heartbeat import heartbeat as write_heartbeat
from app.services.jobs import LeaseLost, lease_next_job, run_job
from app.services.process_identity import process_id
from app.services.push import deliver, lease_due_delivery
from app.services.retention import prune_idle_slice

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("nike_mp.worker")

#: httpx 默认每个请求一行 INFO：worker 实测单进程 300MB+ 日志（曾写满磁盘）。
#: 上游响应汇总改由 jobs._log_upstream_stats 每轮一行输出（含 405 挑战占比）。
logging.getLogger("httpx").setLevel(logging.WARNING)


_running = True


def _stop(*_args: object) -> None:
    global _running
    _running = False


def main() -> None:
    settings = get_settings()
    worker_id = process_id()
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    logger.info("worker %s started", worker_id)
    jobs_done = 0
    while _running:
        session = SessionLocal()
        try:
            try:
                write_heartbeat(
                    session,
                    service="worker",
                    instance_id=worker_id,
                    detail={"jobs_done": jobs_done},
                )
            except Exception:  # noqa: BLE001 - 心跳失败不影响租约与执行
                session.rollback()
                logger.exception("worker heartbeat failed")
            plan = lease_next_plan(session, settings, worker_id)
            if plan is not None:
                session.commit()
                logger.info("bootstrap started for owner %s", plan.owner_user_id)
                run_plan(session, settings, plan, worker_id)
                continue
            drained = 0
            while drained < settings.push_drain_batch:
                delivery = lease_due_delivery(session, settings)
                if delivery is None:
                    break
                session.commit()
                deliver(session, settings, delivery)
                drained += 1
            if drained:
                continue
            job = lease_next_job(session, settings, worker_id)
            if job is None:
                session.commit()
                # 空闲时推进一小片历史清理（独立会话，绝不参与作业事务）。
                prune_idle_slice(keep=settings.observation_history_limit)
                time.sleep(settings.worker_poll_seconds)
                continue
            session.commit()
            logger.info("leasing job %s (%s stores)", job.id, job.total_stores)
            try:
                run_job(session, settings, job, worker_id)
            except LeaseLost:
                session.rollback()
                logger.warning("job %s lease lost; another worker owns it", job.id)
                continue
            jobs_done += 1
            session.commit()
            logger.info("job %s finished with status %s", job.id, job.status)
        except Exception:  # noqa: BLE001 - a worker must survive any single job
            session.rollback()
            logger.exception("job execution failed")
            time.sleep(settings.worker_poll_seconds)
        finally:
            session.close()
    logger.info("worker stopped")


if __name__ == "__main__":
    main()

