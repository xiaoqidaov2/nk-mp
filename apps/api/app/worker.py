"""Scan worker: lease queued jobs and execute them with heartbeats."""

from __future__ import annotations

import logging
import signal
import time

from app.config import get_settings
from app.db import SessionLocal
from app.services.jobs import lease_next_job, run_job

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("nike_mp.worker")

_running = True


def _stop(*_args: object) -> None:
    global _running
    _running = False


def main() -> None:
    settings = get_settings()
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    logger.info("worker started")
    while _running:
        session = SessionLocal()
        try:
            job = lease_next_job(session, settings)
            if job is None:
                session.commit()
                time.sleep(settings.worker_poll_seconds)
                continue
            session.commit()
            logger.info("leasing job %s (%s stores)", job.id, job.total_stores)
            run_job(session, settings, job)
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
