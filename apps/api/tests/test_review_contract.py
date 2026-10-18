"""T01: upstream contract replay.

Replays HTTP-level scenarios through ``NikeClient`` with a mock transport.
No test may ever reach the real Nike endpoints.
"""

from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.nike.client import NikeClient, NikeTransportError
from app.nike.errors import ErrorCode, ScanError

REQUEST_ID = "00000000-0000-0000-0000-000000000000"


class _Replay:
    """Queue-based mock transport; each request pops the next response."""

    def __init__(self, responses: list[httpx.Response | Exception]) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected extra upstream request")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def client(self, settings, *, sleep=None, **kwargs) -> NikeClient:
        return NikeClient(
            settings,
            "token",
            sleep=sleep or (lambda _seconds: None),
            transport=httpx.MockTransport(self.handler),
            **kwargs,
        )


def _json(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _ok_group(gtins: list[str]) -> dict:
    return {
        "response": {
            "fulfillmentGroups": [
                {"items": [{"skuId": gtin, "itemCosts": {}} for gtin in gtins]}
            ]
        }
    }


def _structure_payload(codes: list[str], sizes: int = 2) -> dict:
    return {
        "products": [
            {
                "productCode": code,
                "copy": {"fullTitle": f"{code} 测试"},
                "sizes": [
                    {"localizedLabel": f"4{index}", "gtins": [{"gtin": f"G-{code}-{index}"}]}
                    for index in range(sizes)
                ],
            }
            for code in codes
        ]
    }


def test_batched_structures_chunk_by_thirty_and_split_per_style(settings) -> None:
    """一次带 ≤30 个货号（实测 >30 静默截断、≥80 直接 500）：31 款 = 2 个请求。"""
    codes = [f"ST-{index:02d}" for index in range(31)]
    replay = _Replay(
        [_json(_structure_payload(codes[:30])), _json(_structure_payload(codes[30:]))]
    )
    client = replay.client(settings)

    result = client.fetch_structures("3875", codes)

    assert len(replay.requests) == 2
    first = replay.requests[0]
    assert "internal/v1" in str(first.url)
    assert first.url.params["productCodes"].count(",") == 29
    assert set(result) == set(codes)
    assert result["ST-00"].status == "OK"
    assert [entry.gtin for entry in result["ST-00"].sizes] == ["G-ST-00-0", "G-ST-00-1"]
    assert result["ST-30"].sizes[0].gtin == "G-ST-30-0"


def test_batched_structures_never_invent_a_style_the_store_lacks(settings) -> None:
    """响应里没有的货号 → MISSING（未知不当作结论），其余照常解析。"""
    replay = _Replay([_json(_structure_payload(["ST-01"]))])
    client = replay.client(settings)

    result = client.fetch_structures("3875", ["ST-01", "ST-99"])

    assert result["ST-01"].status == "OK"
    assert result["ST-99"].status == "MISSING"
    assert result["ST-99"].sizes == ()


def test_batched_structures_failure_propagates_for_the_store_retry(settings) -> None:
    """批量端点持续失败时把错误抛给上层：门店记失败/未知并重试，绝不猜成"无货"。

    （与既有约定一致：结构阶段的限流/失败要能让整轮中止，而不是静默变成缺货。）
    """
    replay = _Replay([httpx.Response(500)] * 8)
    client = replay.client(settings)

    with pytest.raises(ScanError):
        client.fetch_structures("3875", ["ST-01", "ST-02"])
    assert len(replay.requests) >= 2  # 按既有重试策略重试后才放弃


def test_empty_completion_is_retried_then_trusted_when_details_arrive(settings) -> None:
    replay = _Replay(
        [
            _json({}, status=202),
            _json({"response": {"fulfillmentGroups": []}}),
            _json(_ok_group(["g0", "g1"])),
        ]
    )
    client = replay.client(settings)
    batch = client.review(["g0", "g1"], region_id="NIKE0S001")
    assert batch.trusted is True
    assert batch.items["g0"].availability == "IN_STOCK"
    assert len(replay.requests) == 3


def test_repeated_empty_completions_end_untrusted(settings) -> None:
    settings.review_poll_tries = 2
    replay = _Replay(
        [
            _json({}, status=202),
            _json({"response": {"fulfillmentGroups": []}}),
            _json({"response": {"fulfillmentGroups": []}}),
        ]
    )
    client = replay.client(settings)
    batch = client.review(["g0"], region_id="NIKE0S001")
    assert batch.trusted is False
    assert batch.error_code == ErrorCode.EMPTY_COMPLETED.value
    assert all(item.availability == "UNKNOWN" for item in batch.items.values())


@pytest.mark.parametrize(
    ("error_payload", "expected_code"),
    [
        ({"errors": []}, ErrorCode.BATCH_SUSPECT.value),
        ({"code": "UPSTREAM_THING"}, ErrorCode.BATCH_SUSPECT.value),
        ("boom", ErrorCode.BATCH_SUSPECT.value),
        ({"errors": ["not-an-object"]}, ErrorCode.BATCH_SUSPECT.value),
        (
            {"errors": [{"field": "/request/items/0/skuId", "code": "SOMETHING_NEW"}]},
            ErrorCode.UNKNOWN_ERROR_CODE.value,
        ),
        (
            {"errors": [{"field": "/request/items/9/skuId", "code": "INVENTORY_INSUFFICIENT"}]},
            ErrorCode.BATCH_SUSPECT.value,
        ),
        (
            {
                "errors": [
                    {"field": "/request/items/0/skuId", "code": "INVENTORY_INSUFFICIENT"},
                    {"field": "/request/items/0/skuId", "code": "PRODUCT_NOT_BUYABLE"},
                ]
            },
            ErrorCode.BATCH_SUSPECT.value,
        ),
        (
            {"errors": [{"field": "/request/items/0/quantity", "code": "ITEM_QUANTITY_LIMIT"}]},
            ErrorCode.ITEM_QUANTITY_LIMIT.value,
        ),
    ],
)
def test_malformed_error_reviews_never_become_stock(settings, error_payload, expected_code) -> None:
    replay = _Replay([_json({}, status=202), _json({"error": error_payload})])
    client = replay.client(settings)
    batch = client.review(["g0", "g1"], region_id="NIKE0S001")
    assert batch.trusted is False
    assert batch.error_code == expected_code
    assert all(item.availability == "UNKNOWN" for item in batch.items.values())


def test_partial_detail_without_errors_is_untrusted(settings) -> None:
    replay = _Replay([_json({}, status=202), _json(_ok_group(["g0"]))])
    client = replay.client(settings)
    batch = client.review(["g0", "g1"], region_id="NIKE0S001")
    assert batch.trusted is False
    assert batch.error_code == ErrorCode.BATCH_SUSPECT.value
    assert batch.items["g1"].availability == "UNKNOWN"


def test_exclusion_rule_maps_item_errors(settings) -> None:
    replay = _Replay(
        [
            _json({}, status=202),
            _json(
                {
                    "error": {
                        "errors": [
                            {"field": "/request/items/0/skuId", "code": "INVENTORY_INSUFFICIENT"},
                            {"field": "/request/items/1/skuId", "code": "PRODUCT_NOT_BUYABLE"},
                        ]
                    }
                }
            ),
        ]
    )
    client = replay.client(settings)
    batch = client.review(["g0", "g1", "g2"], region_id="NIKE0S001")
    assert batch.trusted is True
    assert batch.items["g0"].availability == "OUT_OF_STOCK"
    assert batch.items["g1"].availability == "NOT_CARRIED"
    assert batch.items["g2"].availability == "IN_STOCK"


def test_401_forces_refresh_and_retries_with_new_token(settings) -> None:
    calls: list[str | None] = []

    def provider(rejected: str | None) -> str:
        calls.append(rejected)
        return "fresh-token"

    replay = _Replay(
        [
            _json({"error": "unauthorized"}, status=401),
            _json({}, status=202),
            _json(_ok_group(["g0"])),
        ]
    )
    client = replay.client(settings, token_provider=provider)
    batch = client.review(["g0"], region_id="NIKE0S001")
    assert batch.trusted is True
    assert calls == ["token"]
    assert replay.requests[0].headers["Authorization"] == "Bearer token"
    assert replay.requests[1].headers["Authorization"] == "Bearer fresh-token"


def test_401_without_rotation_fails_fast(settings) -> None:
    replay = _Replay([_json({"error": "unauthorized"}, status=401)])
    client = replay.client(settings, token_provider=lambda rejected: rejected)
    with pytest.raises(ScanError) as excinfo:
        client.review(["g0"], region_id="NIKE0S001")
    assert excinfo.value.code == ErrorCode.AUTH_REQUIRED
    assert len(replay.requests) == 1


def test_waf_challenge_is_retried_then_succeeds(settings) -> None:
    replay = _Replay(
        [
            httpx.Response(405, content=b"<html>data-spm</html>"),
            _json({}, status=202),
            _json(_ok_group(["g0"])),
        ]
    )
    client = replay.client(settings)
    batch = client.review(["g0"], region_id="NIKE0S001")
    assert batch.trusted is True


def test_transport_timeouts_raise_transport_error(settings) -> None:
    settings.request_retry_limit = 2
    replay = _Replay([httpx.ConnectTimeout("timeout"), httpx.ConnectTimeout("timeout")])
    client = replay.client(settings)
    with pytest.raises(NikeTransportError):
        client.review(["g0"], region_id="NIKE0S001")


def test_quota_listener_blocks_request_before_it_is_sent(settings) -> None:
    replay = _Replay([_json(_ok_group(["g0"]))])
    client = replay.client(settings, transaction_listener=lambda kind, charge: False)
    with pytest.raises(ScanError) as excinfo:
        client.review(["g0"], region_id="NIKE0S001")
    assert excinfo.value.code == ErrorCode.BUDGET_EXHAUSTED
    assert replay.requests == []


def test_quota_listener_counts_every_attempt(settings) -> None:
    kinds: list[str] = []

    def listener(kind: str, charge: bool) -> bool:
        if charge:
            kinds.append(kind)
        return True

    replay = _Replay(
        [
            _json({"error": "unauthorized"}, status=401),
            _json({}, status=202),
            _json(_ok_group(["g0"])),
            _json(_ok_group(["g0"])),
        ]
    )
    client = replay.client(
        settings, token_provider=lambda rejected: "fresh", transaction_listener=listener
    )
    batch = client.review(["g0"], region_id="NIKE0S001", with_price=True)
    assert batch.trusted is True
    assert kinds == ["price", "price", "price"]


def test_unknown_gtins_are_never_invented(settings) -> None:
    """Details for an unrequested sku must not appear in the batch."""
    payload = _ok_group(["g0", "g-extra"])
    replay = _Replay([_json({}, status=202), _json(payload)])
    client = replay.client(settings)
    batch = client.review(["g0"], region_id="NIKE0S001")
    assert batch.trusted is True
    assert set(batch.items) == {"g0"}


class _StaleCredential(RuntimeError):
    """Mirrors the stable ``NIKE_*`` code carried by credential errors."""

    code = "NIKE_CREDENTIAL_CHANGED"


def test_credential_guard_blocks_a_request_before_it_is_sent(settings) -> None:
    calls: list[int] = []

    def guard() -> None:
        calls.append(1)
        raise _StaleCredential("binding changed")

    replay = _Replay([_json(_ok_group(["g0"]))])
    client = replay.client(settings, credential_guard=guard)
    with pytest.raises(ScanError) as excinfo:
        client.review(["g0"], region_id="NIKE0S001")
    assert excinfo.value.code == ErrorCode.AUTH_REQUIRED
    assert calls == [1]
    assert replay.requests == []


def test_credential_guard_runs_before_every_authorized_request(settings) -> None:
    checks: list[str] = []

    def guard() -> None:
        checks.append("checked")

    replay = _Replay([_json({}, status=202), _json(_ok_group(["g0"]))])
    client = replay.client(settings, credential_guard=guard)
    batch = client.review(["g0"], region_id="NIKE0S001")
    assert batch.trusted is True
    # One guard per authorized request: the PUT and the poll GET.
    assert len(checks) == 2
    assert len(replay.requests) == 2


def test_credential_guard_scan_error_passes_through(settings) -> None:
    def guard() -> None:
        raise ScanError(ErrorCode.AUTH_REQUIRED, "guard stopped the scan")

    replay = _Replay([])
    client = replay.client(settings, credential_guard=guard)
    with pytest.raises(ScanError) as excinfo:
        client.review(["g0"], region_id="NIKE0S001")
    assert excinfo.value.code == ErrorCode.AUTH_REQUIRED
    assert excinfo.value.message == "guard stopped the scan"
    assert replay.requests == []


def test_rate_limited_responses_are_retried_then_become_throttled(settings) -> None:
    """403/429：先退避重试，耗尽重试预算后按 THROTTLED 结束（账号级冷却的信号）。"""
    replay = _Replay([_json({}, status=403), _json({}, status=403)])
    client = replay.client(settings)
    with pytest.raises(ScanError) as excinfo:
        client.review(["g0"], region_id="NIKE0S001")
    assert excinfo.value.code == ErrorCode.THROTTLED
    assert len(replay.requests) == 2  # settings.request_retry_limit == 2


def test_a_single_429_is_retried_transparently(settings) -> None:
    replay = _Replay(
        [_json({}, status=429), _json({}, status=200), _json(_ok_group(["g0"]))]
    )
    client = replay.client(settings)
    batch = client.review(["g0"], region_id="NIKE0S001")
    assert batch.trusted is True
    assert [request.method for request in replay.requests] == ["PUT", "PUT", "GET"]


def _timing_settings(**overrides) -> Settings:
    values = {
        "credential_key": "test-key-for-unit-tests",
        "review_poll_tries": 5,
        "review_poll_first_seconds": 0.5,
        "review_poll_interval_seconds": 2.5,
        "request_retry_limit": 5,
        "waf_retry_limit": 12,
        "request_retry_base_seconds": 1.5,
        "request_retry_max_seconds": 8.0,
    }
    values.update(overrides)
    return Settings(**values)


def _recording_client(replay: _Replay, settings: Settings, waits: list[float]) -> NikeClient:
    return replay.client(settings, sleep=waits.append)


def test_review_poll_waits_start_short_then_stretch_to_the_cap() -> None:
    """轮询节奏：首次 0.5s，未就绪才翻倍，封顶在 review_poll_interval_seconds。"""
    settings = _timing_settings()
    replay = _Replay(
        [
            _json({}, status=200),
            _json({"response": {"fulfillmentGroups": []}}),
            _json({"response": {"fulfillmentGroups": []}}),
            _json(_ok_group(["g0"])),
        ]
    )
    waits: list[float] = []
    client = _recording_client(replay, settings, waits)
    batch = client.review(["g0"], region_id="NIKE0S001")
    assert batch.trusted is True
    assert waits == [0.5, 1.0, 2.0]


def test_review_poll_backoff_is_capped_by_the_interval() -> None:
    settings = _timing_settings(review_poll_first_seconds=1.5, review_poll_interval_seconds=2.0)
    replay = _Replay(
        [
            _json({}, status=200),
            _json({"response": {"fulfillmentGroups": []}}),
            _json({"response": {"fulfillmentGroups": []}}),
            _json(_ok_group(["g0"])),
        ]
    )
    waits: list[float] = []
    client = _recording_client(replay, settings, waits)
    client.review(["g0"], region_id="NIKE0S001")
    assert waits == [1.5, 2.0, 2.0]


def test_waf_challenge_has_its_own_retry_budget() -> None:
    """405 挑战的重试预算独立于 request_retry_limit（避免 403/5xx 被一起放大）。"""
    settings = _timing_settings(request_retry_limit=2, waf_retry_limit=12, waf_retry_delay_seconds=0.2)
    responses = [httpx.Response(405, content=b"<html data-spm=challenge>") for _ in range(6)]
    responses += [_json({}, status=200), _json(_ok_group(["g0"]))]
    waits: list[float] = []
    client = _recording_client(_Replay(responses), settings, waits)
    assert client.review(["g0"], region_id="NIKE0S001").trusted is True
    # 6 次 405 全部近即时重试成功（远超 request_retry_limit=2），随后是首次轮询等待。
    assert waits[:6] == [0.2] * 6
    assert waits[6] == 0.5


def test_waf_challenge_exhausting_its_budget_raises_throttling_error() -> None:
    settings = _timing_settings(waf_retry_limit=4, request_retry_limit=2, waf_retry_delay_seconds=0.2)
    replay = _Replay([httpx.Response(405, content=b"<html data-spm=challenge>") for _ in range(4)])
    waits: list[float] = []
    client = _recording_client(replay, settings, waits)
    with pytest.raises(ScanError) as excinfo:
        client.review(["g0"], region_id="NIKE0S001")
    assert excinfo.value.code == ErrorCode.WAF_CHALLENGE
    assert waits == [0.2, 0.2, 0.2]


def test_upstream_retry_backoff_doubles_and_caps() -> None:
    """5xx 走指数退避 1.5 → 3 → 6 → 8（封顶）；405 挑战改为近即时重试（不退避）。"""
    settings = _timing_settings()
    replay = _Replay(
        [
            httpx.Response(405, content=b"<html data-spm=challenge>"),
            httpx.Response(405, content=b"<html data-spm=challenge>"),
            _json({}, status=200),
            _json(_ok_group(["g0"])),
        ]
    )
    waits: list[float] = []
    client = _recording_client(replay, settings, waits)
    assert client.review(["g0"], region_id="NIKE0S001").trusted is True
    # 两次 405 近即时重试（0.2s），然后是一次 0.5s 的首次轮询等待。
    assert waits == [0.2, 0.2, 0.5]

    failing = _Replay([_json({}, status=500) for _ in range(5)])
    retry_waits: list[float] = []
    failing_client = _recording_client(failing, settings, retry_waits)
    with pytest.raises(ScanError) as excinfo:
        failing_client.review(["g0"], region_id="NIKE0S001")
    assert excinfo.value.code == ErrorCode.UNEXPECTED_STATUS
    assert retry_waits == [1.5, 3.0, 6.0, 8.0]


def test_waf_challenge_retries_are_not_billed(settings) -> None:
    """405 是上游自己的抽签：重试不计费，只有真正到达业务层的应答才计费。"""
    charged: list[str] = []

    def listener(kind: str, charge: bool) -> bool:
        if charge:
            charged.append(kind)
        return True

    replay = _Replay(
        [
            httpx.Response(405, content=b"<html data-spm=challenge>"),
            httpx.Response(405, content=b"<html data-spm=challenge>"),
            httpx.Response(405, content=b"<html data-spm=challenge>"),
            _json({}, status=202),
            _json(_ok_group(["g0"])),
        ]
    )
    client = replay.client(settings, token_provider=None, transaction_listener=listener)
    batch = client.review(["g0"], region_id="NIKE0S001")
    assert batch.trusted is True
    # 3 次挑战 + 1 次 202 轮询 + 1 次结果：挑战不计费，应答计 2 次。
    assert charged == ["availability", "availability"]


def test_waf_challenge_exhaustion_sends_no_billable_requests(settings) -> None:
    """挑战打满预算时整轮失败，但一次费用都不能记。"""
    charged: list[str] = []

    def listener(kind: str, charge: bool) -> bool:
        if charge:
            charged.append(kind)
        return True

    settings = _timing_settings(waf_retry_limit=4, waf_retry_delay_seconds=0)
    replay = _Replay(
        [httpx.Response(405, content=b"<html data-spm=challenge>") for _ in range(4)]
    )
    client = replay.client(settings, transaction_listener=listener)
    with pytest.raises(ScanError) as excinfo:
        client.review(["g0"], region_id="NIKE0S001")
    assert excinfo.value.code == ErrorCode.WAF_CHALLENGE
    assert charged == []


def _single_structure_payload(code: str, sizes: int = 2) -> dict:
    return {
        "productGroupings": [
            {
                "products": [
                    {
                        "productCode": code,
                        "copy": {"fullTitle": f"{code} 单款"},
                        "sizes": [
                            {
                                "localizedLabel": f"4{index}",
                                "gtins": [{"gtin": f"S-{code}-{index}"}],
                            }
                            for index in range(sizes)
                        ],
                    }
                ]
            }
        ]
    }


def test_batch_structure_rejection_falls_back_to_per_style_lookups(settings) -> None:
    """批量端点被上游收回（实测 401/403）时逐款回落，绝不静默变成"缺尺码"。"""
    replay = _Replay(
        [
            httpx.Response(401),
            _json(_single_structure_payload("ST-01")),
            _json({"productGroupings": []}),
        ]
    )
    client = replay.client(settings)

    result = client.fetch_structures("3875", ["ST-01", "ST-02"])

    urls = [str(request.url) for request in replay.requests]
    assert "internal/v1" in urls[0]
    assert "style_color/ST-01" in urls[1]
    assert "style_color/ST-02" in urls[2]
    assert result["ST-01"].status == "OK"
    assert [entry.gtin for entry in result["ST-01"].sizes] == ["S-ST-01-0", "S-ST-01-1"]
    # 单款端点明确没有该货号 → MISSING（未知不当作结论）。
    assert result["ST-02"].status == "MISSING"


def test_upstream_counters_aggregate_responses_for_the_per_round_summary(settings) -> None:
    """上游响应计数：按状态聚合（取代 httpx 的每请求一行日志）。

    405 挑战占比靠这一行汇总保留；逐请求行曾让单个 worker 日志涨到 300MB+。
    """
    from app.nike import client as client_module

    with client_module._stats_guard:
        client_module._upstream_stats.clear()
    replay = _Replay(
        [
            httpx.Response(405, content=b"data-spm"),
            _json(_structure_payload(["ST-01"])),
        ]
    )
    client = replay.client(settings)
    client.fetch_structures("3875", ["ST-01"])
    stats = client_module.upstream_stats()
    assert stats == {"405": 1, "200": 1}
