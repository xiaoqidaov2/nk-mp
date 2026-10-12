"""Scan worker: lease queued jobs and execute them with heartbeats."""

from __future__ import annotations

import logging
import os
import signal
import socket
import time
import uuid

from app.config import get_settings
from app.db import SessionLocal
from app.services.bootstrap import lease_next_plan, run_plan
from app.services.jobs import LeaseLost, lease_next_job, run_job
from app.services.push import deliver, lease_due_delivery
from app.services.retention import prune_idle_slice

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("nike_mp.worker")

_running = True


def _stop(*_args: object) -> None:
    global _running
    _running = False


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def main() -> None:
    settings = get_settings()
    worker_id = _worker_id()
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    logger.info("worker %s started", worker_id)
    while _running:
        session = SessionLocal()
        try:
            plan = lease_next_plan(session, settings)
            if plan is not None:
                session.commit()
                logger.info("bootstrap started for owner %s", plan.owner_user_id)
                run_plan(session, settings, plan)
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
