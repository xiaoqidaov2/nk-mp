"""Build the egress pool from the paid 1024proxy API (whitelist mode).

Usage (from the deployment host; stdout is the pool list, diagnostics go to stderr):

    cat qa/fetch_paid_proxies.py | ssh <host> 'cd /opt/nk-mp && \
        docker compose exec -T api python - > runtime/proxies.txt.tmp \
        && mv runtime/proxies.txt.tmp runtime/proxies.txt'

Why the API instead of a static list: the provider hands out ``IP:PORT`` entry
points (no credentials — the deployment host's egress IP is whitelisted) and
each endpoint is short-lived, so the pool has to be refreshed continuously.
The compose sidecar ``proxy-refresh`` runs exactly this on a short loop; the
worker hot-reloads the resulting file (mtime watched every 60s).

Environment:
    PAID_PROXY_NUM          endpoints to request per refresh (default 200)
    PAID_PROXY_TTL          ``time`` parameter sent to the API (default 10)
    PAID_PROXY_REGION       ``region`` parameter (default Rand)
    PAID_PROXY_VERIFY       "1" to probe each endpoint once (default 1)
    PAID_PROXY_CONCURRENCY  verification threads (default 60)
"""

from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

API = "https://white.1024proxy.com/white/api"
NUM = int(os.environ.get("PAID_PROXY_NUM", "200"))
TTL = os.environ.get("PAID_PROXY_TTL", "10")
REGION = os.environ.get("PAID_PROXY_REGION", "Rand")
VERIFY = os.environ.get("PAID_PROXY_VERIFY", "1") != "0"
CONCURRENCY = int(os.environ.get("PAID_PROXY_CONCURRENCY", "60"))
PROBE_URL = "https://api.nike.com.cn/store/home/v1/storeNumber/4075"
PROBE_HEADERS = {
    "appid": "com.nike.nvs.comerce.wo.wechat.prod",
    "nike-api-caller-id": "nike:wechat:web:1.0",
    "x-nike-partner": "NDS",
    "x-nike-region": "NIKE0S088",
    "accept": "application/json",
    "user-agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.49(0x1800312b) "
        "NetType/WIFI Language/zh_CN"
    ),
}


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def fetch_endpoints() -> list[str]:
    params = {"region": REGION, "num": NUM, "time": TTL, "format": "1", "type": "txt"}
    response = httpx.get(API, params=params, timeout=30.0)
    response.raise_for_status()
    endpoints: list[str] = []
    for raw in response.text.splitlines():
        line = raw.strip()
        if not line or ":" not in line:
            continue
        host, _, port = line.rpartition(":")
        if host and port.isdigit():
            endpoints.append(f"http://{host}:{port}")
    return list(dict.fromkeys(endpoints))


def usable(endpoint: str) -> str | None:
    try:
        with httpx.Client(proxy=endpoint, timeout=8.0, http2=True) as client:
            response = client.get(PROBE_URL, headers=PROBE_HEADERS)
        return endpoint if response.status_code == 200 else None
    except Exception:  # noqa: BLE001
        return None


def main() -> None:
    started = time.time()
    endpoints = fetch_endpoints()
    log(f"API 返回 {len(endpoints)} 个端点（{time.time() - started:.1f}s）")
    if not endpoints:
        return
    if VERIFY:
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            endpoints = [ep for ep in pool.map(usable, endpoints) if ep]
        log(f"校验通过 {len(endpoints)} 个（{time.time() - started:.1f}s）")
    for endpoint in endpoints:
        print(endpoint)


if __name__ == "__main__":
    main()
