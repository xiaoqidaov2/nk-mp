"""Process identity for leases and heartbeats.

One unique id per running process instance: ``hostname:pid:random8``. Used as
``scan_job.leased_by``, ``owner_bootstrap.leased_by`` and
``service_heartbeat.instance_id``, so a restarted container never inherits the
previous instance's leases.
"""

from __future__ import annotations

import os
import socket
import uuid


def process_id() -> str:
    """Stable within one process; different across restarts and replicas."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
