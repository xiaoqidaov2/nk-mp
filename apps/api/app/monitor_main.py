"""Independent scheduler monitor: heartbeats, overdue checks, ops report.

Runs as its own process next to api/worker/scheduler. Every tick it writes its
own heartbeat, rebuilds the health report and logs warnings when no scheduler
is live or a task is overdue past the grace window. It never dispatches or
executes anything: purely read-only observation plus its own heartbeat.
"""

from __future__ import annotations

import logging
import signal
import time

from app.config import get_settings
from app.db import SessionLocal
from app.services import monitor
from app.services.heartbeat import heartbeat as write_heartbeat
from app.services.process_identity import process_id

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("nike_mp.monitor")

_running = True


def _stop(*_args: object) -> None:
    global _running
    _running = False


def main() -> None:
    settings = get_settings()
    instance_id = process_id()
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    logger.info("monitor %s started", instance_id)
    while _running:
        session = SessionLocal()
        try:
            write_heartbeat(
                session,
                service="monitor",
                instance_id=instance_id,
                detail={"interval_seconds": settings.monitor_interval_seconds},
            )
            report = monitor.build_report(session, settings)
            pruned = monitor.prune_stale_heartbeats(session, settings)
            if pruned:
                logger.info("pruned %s stale heartbeat row(s)", pruned)
            stale = [item["instance_id"] for item in report["scheduler"] if not item["alive"]]
            if not report["scheduler"]:
                logger.warning("scheduler health: no scheduler instance has ever reported")
            elif stale:
                logger.warning("scheduler health: stale scheduler instance(s): %s", ", ".join(stale))
            if report["disk"]["warning"]:
                logger.warning(
                    "host disk is %.0f%% full (%.1fG free); prune images/logs before "
                    "Postgres runs out of space",
                    report["disk"]["used_percent"],
                    report["disk"]["free_gb"],
                )
                queued = monitor.notify_disk_warning(session, settings)
                if queued:
                    logger.warning("queued %s disk warning notice(s)", queued)
            if report["overdue_tasks"]:
                logger.warning(
                    "scheduler health: %s overdue task(s), oldest %ss",
                    len(report["overdue_tasks"]),
                    report["overdue_tasks"][0]["overdue_seconds"],
                )
        except Exception:  # noqa: BLE001 - keep the monitor alive
            session.rollback()
            logger.exception("monitor tick failed")
        finally:
            session.close()
        time.sleep(settings.monitor_interval_seconds)
    logger.info("monitor stopped")


if __name__ == "__main__":
    main()
