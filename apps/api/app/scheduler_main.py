"""Scheduler: turn due monitor tasks into queued scans."""

from __future__ import annotations

import logging
import signal
import time

from app.config import get_settings
from app.db import SessionLocal
from app.services.scheduler import dispatch_due_tasks

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("nike_mp.scheduler")

_running = True


def _stop(*_args: object) -> None:
    global _running
    _running = False


def main() -> None:
    settings = get_settings()
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    logger.info("scheduler started")
    while _running:
        session = SessionLocal()
        try:
            dispatched = dispatch_due_tasks(session)
            session.commit()
            if dispatched:
                logger.info("dispatched %s scan job(s)", dispatched)
        except Exception:  # noqa: BLE001 - keep the scheduler alive
            session.rollback()
            logger.exception("scheduler tick failed")
        finally:
            session.close()
        time.sleep(settings.scheduler_interval_seconds)
    logger.info("scheduler stopped")


if __name__ == "__main__":
    main()
