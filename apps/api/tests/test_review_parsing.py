"""The review parser must never turn an uncertain response into stock."""

from __future__ import annotations

from app.nike.client import NikeClient
from app.nike.errors import ErrorCode


def _client(settings) -> NikeClient:
    return NikeClient(settings, "token", sleep=lambda _seconds: None)


def test_empty_completion_is_untrusted(settings) -> None:
    client = _client(settings)
    batch = client._parse_review({"response": {"fulfillmentGroups": []}}, ["g1", "g2"])
    assert batch.trusted is False
    assert batch.error_code == ErrorCode.EMPTY_COMPLETED.value
    assert all(item.availability == "UNKNOWN" for item in batch.items.values())


def test_item_errors_map_to_statuses(settings) -> None:
    client = _client(settings)
    payload = {
        "error": {
            "errors": [
                {"field": "/request/items/0/skuId", "code": "INVENTORY_INSUFFICIENT"},
                {"field": "/request/items/1/skuId", "code": "PRODUCT_NOT_BUYABLE"},
            ]
        }
    }
    batch = client._parse_review(payload, ["g0", "g1", "g2"])
    assert batch.trusted is True
    assert batch.items["g0"].availability == "OUT_OF_STOCK"
    assert batch.items["g1"].availability == "NOT_CARRIED"
    assert batch.items["g2"].availability == "IN_STOCK"


def test_quantity_limit_never_means_out_of_stock(settings) -> None:
    client = _client(settings)
    payload = {
        "error": {
            "errors": [
                {"field": "/request/items/0/quantity", "code": "ITEM_QUANTITY_LIMIT"},
            ]
        }
    }
    batch = client._parse_review(payload, ["g0", "g1"])
    assert batch.trusted is False
    assert batch.error_code == ErrorCode.ITEM_QUANTITY_LIMIT.value


def test_unknown_error_code_is_untrusted(settings) -> None:
    client = _client(settings)
    payload = {"error": {"errors": [{"field": "/request/items/0/skuId", "code": "SOMETHING_NEW"}]}}
    batch = client._parse_review(payload, ["g0"])
    assert batch.trusted is False
    assert batch.error_code == ErrorCode.UNKNOWN_ERROR_CODE.value


def test_partial_response_is_untrusted(settings) -> None:
    client = _client(settings)
    payload = {"response": {"fulfillmentGroups": [{"items": [{"skuId": "g0", "itemCosts": {}}]}]}}
    batch = client._parse_review(payload, ["g0", "g1"])
    assert batch.trusted is False
    assert batch.error_code == ErrorCode.BATCH_SUSPECT.value
    assert batch.items["g1"].availability == "UNKNOWN"


def test_price_extraction(settings) -> None:
    client = _client(settings)
    payload = {
        "response": {
            "fulfillmentGroups": [
                {
                    "items": [
                        {
                            "skuId": "g0",
                            "itemCosts": {
                                "priceInfo": {
                                    "price": 699,
                                    "fullPrice": 999,
                                    "discount": 314.55,
                                    "total": 384.45,
                                },
                                "promotionDiscounts": [{"displayName": "指定鞋服 55 折"}],
                            },
                        }
                    ]
                }
            ]
        }
    }
    batch = client._parse_review(payload, ["g0"])
    assert batch.trusted is True
    price = batch.items["g0"].price
    assert price is not None
    assert price.total_price == 384.45
    assert price.promotions == ["指定鞋服 55 折"]
