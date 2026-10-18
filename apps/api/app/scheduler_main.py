"""Scheduler: turn due monitor tasks into queued scans.

Multiple scheduler replicas are safe: ``dispatch_due_tasks`` takes a
transaction-level advisory lock, so a second replica simply skips the tick
(active/standby without a leader state machine). Every tick writes a
``scheduler`` heartbeat for the monitor service.
"""

from __future__ import annotations

import logging
import signal
import time

from app.config import get_settings
from app.db import SessionLocal
from app.services import nike_auth
from app.services.bootstrap import enqueue_due
from app.services.credentials import refresh_expiring
from app.services.heartbeat import heartbeat as write_heartbeat
from app.services.process_identity import process_id
from app.services.scheduler import dispatch_due_tasks

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("nike_mp.scheduler")

#: httpx 默认每个请求一行 INFO：worker 实测单进程 300MB+ 日志（曾写满磁盘）。
#: 上游响应汇总改由 jobs._log_upstream_stats 每轮一行输出（含 405 挑战占比）。
logging.getLogger("httpx").setLevel(logging.WARNING)


_running = True


def _stop(*_args: object) -> None:
    global _running
    _running = False


def main() -> None:
    settings = get_settings()
    instance_id = process_id()
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    logger.info("scheduler %s started", instance_id)
    tick = 0
    while _running:
        tick += 1
        session = SessionLocal()
        dispatched = 0
        try:
            dispatched = dispatch_due_tasks(session, settings)
            session.commit()
            if dispatched:
                logger.info("dispatched %s scan job(s)", dispatched)
            nike_auth.expire_attempts()
            nike_auth.cleanup_old_attempts()
            refreshed = enqueue_due(session, settings)
            if refreshed:
                session.commit()
                logger.info("enqueued %s catalogue refresh plan(s)", refreshed)
            renewed = refresh_expiring(settings)
            if renewed:
                logger.info("renewed %s credential(s)", renewed)
            write_heartbeat(
                session,
                service="scheduler",
                instance_id=instance_id,
                detail={"dispatched": dispatched, "tick": tick},
            )
        except Exception:  # noqa: BLE001 - keep the scheduler alive
            session.rollback()
            logger.exception("scheduler tick failed")
            try:
                write_heartbeat(
                    session,
                    service="scheduler",
                    instance_id=instance_id,
                    detail={"dispatched": dispatched, "tick": tick, "failed": True},
                )
            except Exception:  # noqa: BLE001 - 心跳失败不能拖垮调度循环
                session.rollback()
                logger.exception("scheduler heartbeat failed")
        finally:
            session.close()
        time.sleep(settings.scheduler_interval_seconds)
    logger.info("scheduler stopped")


if __name__ == "__main__":
    main()
