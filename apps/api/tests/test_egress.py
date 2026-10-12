"""出口池与指纹轮换：一请求一出口、一请求一特征、坏出口冷却、列表热重载。

这些行为直接决定并发规模（"多出口、多特征"）能否按预期工作：轮转必须真的换出口、
坏出口必须被冷却而不是让请求失败、指纹必须每请求变化（且可关）。
"""

from __future__ import annotations

import httpx

from app.nike import fingerprints
from app.nike.client import NikeClient
from app.nike.egress import EgressPool, load_urls_from_file


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _json(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def test_round_robin_uses_every_exit_before_repeating() -> None:
    pool = EgressPool(["http://a:1", "http://b:2", "http://c:3"])
    first, second, third = pool.next(), pool.next(), pool.next()
    assert len({id(first), id(second), id(third)}) == 3
    assert pool.next() is first  # 一圈之后回到起点
    pool.close()


def test_failed_exit_cools_down_then_recovers() -> None:
    clock = _Clock()
    pool = EgressPool(["http://a:1", "http://b:2", "http://c:3"], cooldown_seconds=30.0, clock=clock)
    a, b, c = pool.next(), pool.next(), pool.next()
    pool.report_failure(a)
    assert pool.cooling == ["http://a:1"]
    assert pool.next() is b
    assert pool.next() is c
    clock.advance(31.0)
    assert pool.cooling == []
    assert pool.next() is a
    pool.close()


def test_all_exits_cooling_still_returns_the_soonest_recovery() -> None:
    clock = _Clock()
    pool = EgressPool(["http://a:1", "http://b:2"], cooldown_seconds=30.0, clock=clock)
    a, b = pool.next(), pool.next()
    pool.report_failure(a)
    clock.advance(10.0)
    pool.report_failure(b)
    assert pool.next() is a  # a 更早恢复
    pool.close()


def test_replace_urls_keeps_existing_clients_and_drops_stale() -> None:
    pool = EgressPool(["http://a:1", "http://b:2"])
    keep = pool.next()
    assert pool.replace_urls(["http://a:1", "http://c:3"]) == 2
    assert sorted(pool.urls) == ["http://a:1", "http://c:3"]
    assert any(pool.next() is keep for _ in range(len(pool)))
    pool.close()


def test_load_urls_from_file_ignores_comments_and_blanks(tmp_path) -> None:
    path = tmp_path / "proxies.txt"
    path.write_text("# 注释\nhttp://1.2.3.4:8080\n\n  http://5.6.7.8:3128  \n", encoding="utf-8")
    assert load_urls_from_file(path) == ["http://1.2.3.4:8080", "http://5.6.7.8:3128"]
    assert load_urls_from_file(tmp_path / "missing.txt") == []


def _structure_client(settings, *, handler, **kwargs) -> NikeClient:
    return NikeClient(
        settings,
        "token",
        sleep=lambda _seconds: None,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_fingerprints_rotate_per_request(settings) -> None:
    settings.fingerprint_rotation = True
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["user-agent"])
        return _json({"productGroupings": []})

    client = _structure_client(settings, handler=handler)
    for _ in range(len(fingerprints.PROFILES)):
        client.fetch_structure("3402", "CD7069-003")
    assert len(set(seen)) == len(fingerprints.PROFILES)


def test_fingerprint_rotation_can_be_disabled(settings) -> None:
    settings.fingerprint_rotation = False
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["user-agent"])
        return _json({"productGroupings": []})

    client = _structure_client(settings, handler=handler)
    for _ in range(3):
        client.fetch_structure("3402", "CD7069-003")
    assert len(set(seen)) == 1


def test_pool_serves_consecutive_requests_from_different_exits(settings) -> None:
    served: list[str] = []

    def factory(url: str) -> httpx.BaseTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            served.append(url)
            return _json({"productGroupings": []})

        return httpx.MockTransport(handler)

    pool = EgressPool(["http://a:1", "http://b:2"], transport_factory=factory)
    client = _structure_client(settings, handler=lambda request: _json({}), egress=pool)
    client.fetch_structure("3402", "CD7069-003")
    client.fetch_structure("3402", "CD7069-003")
    assert served == ["http://a:1", "http://b:2"]
    client.close()


def test_transport_failure_cools_that_exit_and_retries_elsewhere(settings) -> None:
    served: list[str] = []

    def factory(url: str) -> httpx.BaseTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            served.append(url)
            if url == "http://a:1":
                raise httpx.ConnectError("boom", request=request)
            return _json({"productGroupings": []})

        return httpx.MockTransport(handler)

    pool = EgressPool(["http://a:1", "http://b:2"], transport_factory=factory, cooldown_seconds=60.0)
    client = _structure_client(settings, handler=lambda request: _json({}), egress=pool)
    structure = client.fetch_structure("3402", "CD7069-003")
    assert structure.status == "MISSING"  # 空载荷 → 结构缺失，但请求本身成功
    assert served == ["http://a:1", "http://b:2"]  # 第一次失败后换出口重试
    assert pool.cooling == ["http://a:1"]
    client.close()


def test_sticky_pool_keeps_one_exit_and_only_moves_on_cooldown() -> None:
    """粘性模式（稳定住宅出口：一账号一 IP）：不轮换；只有坏出口才让位。"""
    clock = _Clock()
    pool = EgressPool(
        ["http://a:1", "http://b:2"], cooldown_seconds=60.0, clock=clock, sticky=True
    )
    first = pool.next()
    assert pool.next() is first
    assert pool.next() is first  # 三次都钉在同一条

    pool.report_failure(first)  # 一号出口坏了 → 临时让位给二号
    second = pool.next()
    assert second is not first
    assert pool.next() is second

    clock.advance(61.0)  # 冷却结束 → 回到钉住的那条
    assert pool.next() is first
    pool.close()
