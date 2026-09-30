"""T01: upstream contract replay.

Replays HTTP-level scenarios through ``NikeClient`` with a mock transport.
No test may ever reach the real Nike endpoints.
"""

from __future__ import annotations

import httpx
import pytest

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

    def client(self, settings, **kwargs) -> NikeClient:
        return NikeClient(
            settings,
            "token",
            sleep=lambda _seconds: None,
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
    client = replay.client(settings, transaction_listener=lambda kind: False)
    with pytest.raises(ScanError) as excinfo:
        client.review(["g0"], region_id="NIKE0S001")
    assert excinfo.value.code == ErrorCode.BUDGET_EXHAUSTED
    assert replay.requests == []


def test_quota_listener_counts_every_attempt(settings) -> None:
    kinds: list[str] = []

    def listener(kind: str) -> bool:
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
