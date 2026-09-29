"""HTTP client for the Nike China mini-program and store APIs.

The client is deliberately free of persistence concerns so that it can be
exercised in tests without a database. Every uncertain response is surfaced
as an :class:`~app.nike.errors.ScanError` instead of being coerced into a
stock conclusion.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx

from app.config import Settings
from app.nike.errors import ITEM_STATUS_BY_CODE, ErrorCode, ScanError
from app.nike.types import PriceInfo, ReviewBatch, ReviewItem, SizeEntry, SizeStructure, StoreRecord


class NikeTransportError(RuntimeError):
    pass


class NikeClient:
    def __init__(
        self,
        settings: Settings,
        access_token: str,
        token_provider: Callable[[str | None], str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        transport: httpx.BaseTransport | None = None,
        transaction_listener: Callable[[str], bool] | None = None,
    ) -> None:
        self.settings = settings
        self._access_token = access_token
        self._token_provider = token_provider
        self._sleep = sleep
        self._client = httpx.Client(
            http2=True, timeout=30.0, follow_redirects=False, transport=transport
        )
        self.transaction_listener = transaction_listener

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> NikeClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ http

    def _headers(self, region_id: str, authorized: bool) -> dict[str, str]:
        headers = {
            "appid": self.settings.nike_app_id,
            "nike-api-caller-id": "nike:wechat:web:1.0",
            "x-nike-partner": self.settings.nike_partner,
            "x-nike-region": region_id,
            "accept": "application/json",
            "accept-language": "zh-CN,zh;q=0.9",
            "referer": self.settings.nike_referer,
            "user-agent": self.settings.nike_user_agent,
            "content-type": "application/json; charset=UTF-8",
        }
        if authorized:
            headers["Authorization"] = f"Bearer {self._access_token}"
        return headers

    def _request(
        self,
        method: str,
        url: str,
        region_id: str = "",
        body: Any | None = None,
        authorized: bool = True,
        quota_kind: str = "",
    ) -> httpx.Response:
        attempts = 0
        auth_retries = 0
        response: httpx.Response | None = None
        while attempts < self.settings.request_retry_limit:
            self._consume_transaction(quota_kind)
            try:
                response = self._client.request(
                    method,
                    url,
                    headers=self._headers(region_id, authorized),
                    content=json.dumps(body) if body is not None else None,
                )
            except httpx.TransportError as exc:
                attempts += 1
                if attempts >= self.settings.request_retry_limit:
                    raise NikeTransportError(str(exc)) from exc
                self._sleep(1.5 + attempts)
                continue
            if response.status_code == 401 and authorized:
                if auth_retries >= 2 or self._token_provider is None:
                    raise ScanError(ErrorCode.AUTH_REQUIRED, "access token rejected")
                auth_retries += 1
                rejected = self._access_token
                refreshed = self._token_provider(rejected)
                if not refreshed or refreshed == rejected:
                    # The credential could not be rotated; retrying with the
                    # same token would only burn the remaining budget.
                    raise ScanError(ErrorCode.AUTH_REQUIRED, "token refresh did not rotate the credential")
                self._access_token = refreshed
                continue
            if response.status_code == 405 and b"data-spm" in response.content:
                attempts += 1
                if attempts >= self.settings.request_retry_limit:
                    raise ScanError(ErrorCode.WAF_CHALLENGE, "WAF challenge not cleared")
                self._sleep(2.0 + attempts * 1.5)
                continue
            if response.status_code >= 500:
                attempts += 1
                if attempts >= self.settings.request_retry_limit:
                    raise ScanError(
                        ErrorCode.UNEXPECTED_STATUS, f"upstream {response.status_code}"
                    )
                self._sleep(1.5 + attempts)
                continue
            return response
        raise NikeTransportError("request retries exhausted")

    def _consume_transaction(self, quota_kind: str) -> None:
        if not quota_kind or self.transaction_listener is None:
            return
        if not self.transaction_listener(quota_kind):
            raise ScanError(
                ErrorCode.BUDGET_EXHAUSTED,
                "upstream transaction budget exhausted",
            )

    # --------------------------------------------------------------- catalog

    def list_stores(self) -> list[StoreRecord]:
        url = f"{self.settings.nike_api_base}/o2o/buy/regions/v1?anchor=0&count=200"
        response = self._request("GET", url, region_id="NIKE0S088")
        if response.status_code != 200:
            raise ScanError(ErrorCode.UNEXPECTED_STATUS, f"regions {response.status_code}")
        records: list[StoreRecord] = []
        for obj in response.json().get("objects", []):
            store_code = str(obj.get("storeCode") or "").strip()
            region_id = str(obj.get("id") or "").strip()
            if not store_code or not region_id:
                continue
            records.append(
                StoreRecord(
                    store_code=store_code,
                    region_id=region_id,
                    display_name=str(obj.get("displayName") or "").strip(),
                    address=str(obj.get("storeAddress") or "").strip(),
                    concept=str(obj.get("concept") or "").strip(),
                )
            )
        return records

    def fetch_structure(self, store_code: str, style_code: str) -> SizeStructure:
        url = (
            f"{self.settings.nike_test_api_base}/store/product_details/v1/marketplace/CN/"
            f"language/zh-Hans/store_number/{store_code}/style_color/{style_code}"
        )
        response = self._request("GET", url, authorized=False)
        if response.status_code != 200:
            return SizeStructure(store_code, style_code, "MISSING", style_code)
        try:
            payload = response.json()
        except ValueError:
            return SizeStructure(store_code, style_code, "MISSING", style_code)
        groupings = payload.get("productGroupings") or []
        products = (groupings[0].get("products") if groupings else None) or []
        if not products:
            return SizeStructure(store_code, style_code, "MISSING", style_code)
        product = products[0]
        copy = product.get("copy") or {}
        item_data = product.get("itemData") or {}
        title = str(copy.get("fullTitle") or item_data.get("title") or style_code)
        sizes: list[SizeEntry] = []
        for size in product.get("sizes") or []:
            gtins = size.get("gtins") or []
            gtin = str((gtins[0] if gtins else {}).get("gtin") or "").strip()
            label = str(size.get("localizedLabel") or "").strip()
            if gtin and label:
                sizes.append(SizeEntry(size_label=label, gtin=gtin))
        if not sizes:
            return SizeStructure(store_code, style_code, "MISSING", title)
        return SizeStructure(store_code, style_code, "OK", title, tuple(sizes))

    def fetch_new_arrivals(self, pages: int = 6) -> list[dict[str, Any]]:
        channel = "d9a5bc42-4b9c-4976-858a-f159cf99c647"
        results: list[dict[str, Any]] = []
        anchor = 0
        for _ in range(pages):
            url = (
                f"{self.settings.nike_api_base}/discover/product_wall/v1/marketplace/CN/"
                f"language/zh-Hans/consumerChannelId/{channel}"
                f"?path=%2Fw%2Fnew-3n82y&queryType=PRODUCTS&anchor={anchor}&count=50"
            )
            response = self._request("GET", url, region_id="NIKE0S088", authorized=False)
            if response.status_code != 200:
                break
            payload = response.json()
            found = 0
            for grouping in payload.get("productGroupings") or []:
                for product in grouping.get("products") or []:
                    code = product.get("productCode")
                    if not code:
                        continue
                    found += 1
                    copy = product.get("copy") or {}
                    prices = product.get("prices") or {}
                    results.append(
                        {
                            "style_code": str(code).upper(),
                            "title": str(copy.get("title") or ""),
                            "price": prices.get("currentPrice"),
                            "full_price": prices.get("initialPrice"),
                            "discount": prices.get("discountPercentage"),
                        }
                    )
            if found == 0:
                break
            anchor += 50
            self._sleep(2.0)
        return results

    # ---------------------------------------------------------------- probes

    def review(
        self,
        gtins: list[str],
        region_id: str,
        with_price: bool = False,
    ) -> ReviewBatch:
        if not gtins:
            return ReviewBatch(trusted=True)
        request_id = str(uuid.uuid4())
        base = f"{self.settings.nike_api_base}/o2o/buy/cart_reviews/v2/{request_id}"
        items = [
            {
                "id": str(uuid.uuid4()),
                "skuId": gtin,
                "quantity": 1,
                "valueAddedServices": [],
                "fulfillmentDetails": {
                    "type": "SHIP",
                    "location": {
                        "type": "address/shipping",
                        "postalAddress": {"country": "CN"},
                    },
                },
            }
            for gtin in gtins
        ]
        body = {
            "request": {
                "country": "CN",
                "currency": "CNY",
                "locale": "zh_CN",
                "items": items,
                "promotionCodes": [],
            }
        }
        quota_kind = "price" if with_price else "availability"
        response = self._request("PUT", base, region_id=region_id, body=body, quota_kind=quota_kind)
        if response.status_code not in (200, 201, 202):
            raise ScanError(ErrorCode.UNEXPECTED_STATUS, f"review PUT {response.status_code}")

        saw_empty = False
        for _ in range(self.settings.review_poll_tries):
            self._sleep(self.settings.review_poll_interval_seconds)
            polled = self._request("GET", base, region_id=region_id, quota_kind=quota_kind)
            try:
                payload = polled.json()
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            if "response" not in payload and "error" not in payload:
                continue
            batch = self._parse_review(payload, gtins)
            if batch.error_code == ErrorCode.EMPTY_COMPLETED.value:
                saw_empty = True
                continue
            if with_price and batch.trusted:
                return batch
            return batch
        code = ErrorCode.EMPTY_COMPLETED if saw_empty else ErrorCode.POLL_TIMEOUT
        return ReviewBatch(
            items={gtin: ReviewItem(gtin=gtin, availability="UNKNOWN") for gtin in gtins},
            trusted=False,
            error_code=code.value,
            message="review did not reach a trustworthy terminal state",
        )

    def _parse_review(self, payload: Any, gtins: list[str]) -> ReviewBatch:
        """Interpret one polled review payload.

        The exclusion rule (unlisted items are in stock) is only applied when
        the payload has a shape every requested SKU can be accounted for.
        Anything else returns UNKNOWN instead of a stock conclusion.
        """
        if not isinstance(payload, dict):
            return self._untrusted(
                gtins, ErrorCode.BATCH_SUSPECT, "review payload is not an object"
            )
        error = payload.get("error")
        response = payload.get("response") or {}
        if not isinstance(response, dict):
            return self._untrusted(
                gtins, ErrorCode.BATCH_SUSPECT, "review response is not an object"
            )
        groups = response.get("fulfillmentGroups") or []
        if not isinstance(groups, list):
            return self._untrusted(
                gtins, ErrorCode.BATCH_SUSPECT, "fulfillmentGroups is not a list"
            )

        if not error and not groups:
            return self._untrusted(
                gtins, ErrorCode.EMPTY_COMPLETED, "empty completed review response"
            )

        if error:
            return self._parse_error_review(error, gtins)

        items: dict[str, ReviewItem] = {}
        for group in groups:
            if not isinstance(group, dict):
                continue
            for entry in group.get("items") or []:
                if not isinstance(entry, dict):
                    continue
                gtin = str(entry.get("skuId") or "")
                if gtin not in gtins or gtin in items:
                    continue
                items[gtin] = ReviewItem(
                    gtin=gtin,
                    availability="IN_STOCK",
                    price=self._extract_price(entry),
                )
        missing = [gtin for gtin in gtins if gtin not in items]
        if missing:
            # No error was reported yet some requested SKUs are absent: the
            # response is partial, so the omitted rows stay unknown.
            for gtin in missing:
                items[gtin] = ReviewItem(gtin=gtin, availability="UNKNOWN")
            return ReviewBatch(
                items=items,
                trusted=False,
                error_code=ErrorCode.BATCH_SUSPECT.value,
                message=f"{len(missing)} requested sku(s) missing from review response",
            )
        return ReviewBatch(items=items, trusted=True)

    def _parse_error_review(self, error: Any, gtins: list[str]) -> ReviewBatch:
        """Map per-item errors to statuses; anything else invalidates the batch.

        The exclusion rule is only valid for a well-formed, non-empty
        ``error.errors`` list whose entries all carry a known per-item code.
        A missing or empty list, an unknown error code, an entry that is not an
        object, or a reference outside the requested items means the response
        cannot be accounted for, so every row stays UNKNOWN.
        """
        if not isinstance(error, dict):
            return self._untrusted(
                gtins, ErrorCode.BATCH_SUSPECT, "error payload is not an object"
            )
        entries = error.get("errors")
        if not isinstance(entries, list) or not entries:
            return self._untrusted(
                gtins, ErrorCode.BATCH_SUSPECT, "error object without a usable error list"
            )

        statuses: dict[int, str] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                return self._untrusted(gtins, ErrorCode.BATCH_SUSPECT, "malformed error entry")
            field = str(entry.get("field") or "")
            code = str(entry.get("code") or "")
            index = self._item_index(field)
            if code == "ITEM_QUANTITY_LIMIT":
                return self._untrusted(
                    gtins,
                    ErrorCode.ITEM_QUANTITY_LIMIT,
                    f"{code} on {field or 'request'}",
                )
            if index is None:
                return self._untrusted(
                    gtins, ErrorCode.BATCH_SUSPECT, f"error not scoped to an item: {field or 'request'}"
                )
            mapped = ITEM_STATUS_BY_CODE.get(code)
            if mapped is None:
                return self._untrusted(
                    gtins, ErrorCode.UNKNOWN_ERROR_CODE, f"unknown error code {code or 'missing'}"
                )
            if not 0 <= index < len(gtins):
                return self._untrusted(
                    gtins, ErrorCode.BATCH_SUSPECT, f"error references item {index} outside the batch"
                )
            existing = statuses.get(index)
            if existing is not None and existing != mapped:
                return self._untrusted(
                    gtins, ErrorCode.BATCH_SUSPECT, f"conflicting errors for item {index}"
                )
            statuses[index] = mapped

        items: dict[str, ReviewItem] = {}
        for index, gtin in enumerate(gtins):
            status = statuses.get(index, "IN_STOCK")
            items[gtin] = ReviewItem(gtin=gtin, availability=status)
        return ReviewBatch(items=items, trusted=True)

    @staticmethod
    def _untrusted(gtins: list[str], code: ErrorCode, message: str) -> ReviewBatch:
        return ReviewBatch(
            items={gtin: ReviewItem(gtin=gtin, availability="UNKNOWN") for gtin in gtins},
            trusted=False,
            error_code=code.value,
            message=message,
        )

    @staticmethod
    def _item_index(field: str) -> int | None:
        marker = "/items/"
        if marker not in field:
            return None
        tail = field.split(marker, 1)[1]
        digits = tail.split("/", 1)[0]
        return int(digits) if digits.isdigit() else None


    @staticmethod
    def _extract_price(entry: dict[str, Any]) -> PriceInfo | None:
        costs = entry.get("itemCosts") or {}
        price_info = costs.get("priceInfo")
        if not price_info:
            return None
        promotions = [
            str(item.get("displayName") or "")
            for item in costs.get("promotionDiscounts") or []
            if item.get("displayName")
        ]
        return PriceInfo(
            price=_to_float(price_info.get("price")),
            full_price=_to_float(price_info.get("fullPrice")),
            discount_amount=_to_float(price_info.get("discount")),
            total_price=_to_float(price_info.get("total")),
            promotions=promotions,
        )


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None
