"""Egress pool: round-robin HTTP(S) proxies for upstream traffic.

Why a pool: the 405 challenge rate is ~40% per request *and flat* up to ~94
requests/second from a single exit (measured 2026-10-06), so a single IP is
not the limiter for correctness — but it is the limiter for *concurrency*:
one origin connection pool saturates (we saw 408/protocol errors at 16 local
threads) and one IP is a single point of throttling. Spreading the same
request rate over many exits raises the achievable request count without
changing the per-request odds.

Design notes:

* Proxy URLs come from settings (``NIKE_MP_PROXY_URLS``) and/or a list file
  (``NIKE_MP_PROXY_URLS_FILE``); the file is re-read periodically so a cron
  job can refresh a validated list without a redeploy.
* Clients are created lazily per proxy and reused (keep-alive per exit).
* A transport failure cools that exit down for ``NIKE_MP_PROXY_COOLDOWN_SECONDS``
  (default 120s) instead of failing the request: the caller retries on the next
  healthy exit. WAF 405s are *not* failures (they are the normal ~40% pass
  rate) and never cool an exit down.
* With no proxies configured the pool is ``None`` and the client keeps using
  its direct connection — identical to the previous behaviour.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

logger = logging.getLogger("nike_mp.egress")

DEFAULT_COOLDOWN_SECONDS = 120.0
FILE_RELOAD_SECONDS = 60.0


@dataclass
class _Entry:
    url: str
    client: httpx.Client
    cooldown_until: float = 0.0
    failures: int = field(default=0)


class EgressPool:
    """Round-robin over a set of proxies (falling back to the direct client)."""

    def __init__(
        self,
        urls: Iterable[str],
        *,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        transport_factory: Callable[[str], httpx.BaseTransport | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        client_kwargs: dict | None = None,
        sticky: bool = False,
    ) -> None:
        self._cooldown = cooldown_seconds
        #: True = 钉住同一个出口（一账号一 IP 的"稳定住宅出口"模式）；
        #: False = 老的"一请求一出口"轮转。列表里放一条即整账号走那一条。
        self._sticky = sticky
        self._transport_factory = transport_factory
        self._clock = clock
        self._client_kwargs = dict(client_kwargs or {})
        self._lock = threading.Lock()
        self._entries: list[_Entry] = [self._make_entry(url) for url in dict.fromkeys(urls) if url]
        self._cursor = 0

    # ------------------------------------------------------------------ setup
    def _make_entry(self, url: str) -> _Entry:
        transport = self._transport_factory(url) if self._transport_factory else None
        kwargs: dict = {
            "http2": True,
            "timeout": 30.0,
            "follow_redirects": False,
            **self._client_kwargs,
        }
        if transport is not None:
            # 注入的 transport 直接代表"经该出口"的链路（测试用），此时不能再传 proxy，
            # 否则 httpx 仍会去解析代理主机。
            kwargs["transport"] = transport
        else:
            kwargs["proxy"] = url
        return _Entry(url=url, client=httpx.Client(**kwargs))

    # ----------------------------------------------------------------- public
    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    @property
    def urls(self) -> list[str]:
        with self._lock:
            return [entry.url for entry in self._entries]

    @property
    def cooling(self) -> list[str]:
        """URLs currently in cooldown (observability/tests)."""
        now = self._clock()
        with self._lock:
            return [entry.url for entry in self._entries if entry.cooldown_until > now]

    def next(self) -> httpx.Client:
        """Return the next healthy proxy client.

        Round-robin by default; with ``sticky`` the same exit is reused for the
        lifetime of the pool (an account keeps one IP — rotating exits per
        request is what gets accounts flagged, see docs). When every exit is
        cooling down: return the one that recovers soonest so the caller can
        still make progress instead of blocking.
        """
        now = self._clock()
        with self._lock:
            if not self._entries:
                raise RuntimeError("egress pool is empty")
            count = len(self._entries)
            for offset in range(count):
                index = offset if self._sticky else (self._cursor + offset) % count
                entry = self._entries[index]
                if entry.cooldown_until <= now:
                    self._cursor = (index + 1) % count
                    return entry.client
            soonest = min(self._entries, key=lambda item: item.cooldown_until)
            self._cursor = (self._entries.index(soonest) + 1) % count
            return soonest.client

    def report_failure(self, client: httpx.Client) -> None:
        """Cool down the exit behind ``client`` (transport errors only)."""
        with self._lock:
            for entry in self._entries:
                if entry.client is client:
                    entry.failures += 1
                    entry.cooldown_until = self._clock() + self._cooldown
                    logger.warning(
                        "egress %s failed %d time(s); cooling down for %.0fs",
                        entry.url,
                        entry.failures,
                        self._cooldown,
                    )
                    return

    def replace_urls(self, urls: Iterable[str]) -> int:
        """Swap the proxy list (used by the file reloader). Returns the size."""
        wanted = [url for url in dict.fromkeys(urls) if url]
        with self._lock:
            existing = {entry.url: entry for entry in self._entries}
            self._entries = []
            for url in wanted:
                entry = existing.pop(url, None)
                self._entries.append(entry if entry is not None else self._make_entry(url))
            for stale in existing.values():
                stale.client.close()
            self._cursor = 0
            return len(self._entries)

    def close(self) -> None:
        with self._lock:
            for entry in self._entries:
                entry.client.close()
            self._entries = []


def load_urls_from_file(path: str | Path) -> list[str]:
    """Read a proxy list file: one URL per line, ``#`` comments ignored."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    urls: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(line)
    return urls
