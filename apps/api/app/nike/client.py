"""HTTP client for the Nike China mini-program and store APIs.

The client is deliberately free of persistence concerns so that it can be
exercised in tests without a database. Every uncertain response is surfaced
as an :class:`~app.nike.errors.ScanError` instead of being coerced into a
stock conclusion.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings
from app.nike import fingerprints
from app.nike.egress import FILE_RELOAD_SECONDS, EgressPool, load_urls_from_file
from app.nike.errors import ITEM_STATUS_BY_CODE, ErrorCode, ScanError
from app.nike.types import PriceInfo, ReviewBatch, ReviewItem, SizeEntry, SizeStructure, StoreRecord

logger = logging.getLogger("nike_mp.nike_client")


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
        transaction_listener: Callable[[str, bool], bool] | None = None,
        *,
        credential_guard: Callable[[], None] | None = None,
        egress: EgressPool | None = None,
    ) -> None:
        self.settings = settings
        self._access_token = access_token
        self._token_provider = token_provider
        self._credential_guard = credential_guard
        self._sleep = sleep
        self._client = httpx.Client(
            http2=True, timeout=30.0, follow_redirects=False, transport=transport
        )
        self.transaction_listener = transaction_listener
        self._fingerprint_index = 0
        self._egress = egress if egress is not None else self._build_egress_pool()
        self._egress_file_mtime = 0.0
        self._egress_file_checked_at = 0.0

    def _build_egress_pool(self) -> EgressPool | None:
        urls = list(self.settings.proxy_urls)
        if self.settings.proxy_urls_file:
            urls.extend(load_urls_from_file(self.settings.proxy_urls_file))
        if not urls:
            return None
        pool = EgressPool(
            urls,
            cooldown_seconds=self.settings.proxy_cooldown_seconds,
            sticky=self.settings.proxy_sticky,
        )
        logger.info(
            "egress pool enabled with %d exit(s)%s",
            len(pool),
            " (sticky)" if self.settings.proxy_sticky else "",
        )
        return pool

    def _reload_egress_file(self) -> None:
        """Hot-reload the proxy list file (at most once a minute)."""
        path = self.settings.proxy_urls_file
        if not path or self._egress is None:
            return
        now = time.monotonic()
        if now - self._egress_file_checked_at < FILE_RELOAD_SECONDS:
            return
        self._egress_file_checked_at = now
        try:
            mtime = Path(path).stat().st_mtime
        except OSError:
            return
        if mtime == self._egress_file_mtime:
            return
        self._egress_file_mtime = mtime
        urls = list(self.settings.proxy_urls) + load_urls_from_file(path)
        if urls:
            size = self._egress.replace_urls(urls)
            logger.info("egress pool reloaded from %s (%d exit(s))", path, size)

    def close(self) -> None:
        self._client.close()
        if self._egress is not None:
            self._egress.close()

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
        if self.settings.fingerprint_rotation:
            # 每请求换一次特征（实测单请求通过率与特征无关，这里服务于"一请求一特征"的并发隔离）。
            headers.update(fingerprints.pick_profile(self._fingerprint_index))
            self._fingerprint_index += 1
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
        waf_attempts = 0
        response: httpx.Response | None = None
        self._reload_egress_file()
        # 405 挑战有自己的预算（见 waf_retry_limit），循环上限取两者较大值。
        max_attempts = max(self.settings.request_retry_limit, self.settings.waf_retry_limit)
        while attempts < max_attempts:
            if authorized and self._credential_guard is not None:
                # The binding must still be current and the owner enabled
                # before any authoritative request consumes quota. Requests
                # already sent cannot be recalled; only future ones stop.
                self._run_credential_guard()
            # 预检不计数：额度见底时一个请求都不发。
            self._call_quota(quota_kind, charge=False)
            try:
                sender = self._egress.next() if self._egress is not None else self._client
                response = sender.request(
                    method,
                    url,
                    headers=self._headers(region_id, authorized),
                    content=json.dumps(body) if body is not None else None,
                )
            except httpx.TransportError as exc:
                if self._egress is not None:
                    # 出口坏了就冷却它，下次尝试换下一个出口（"一请求一出口"的轮转）。
                    self._egress.report_failure(sender)
                attempts += 1
                if attempts >= self.settings.request_retry_limit:
                    raise NikeTransportError(str(exc)) from exc
                self._sleep(self._retry_delay(attempts))
                continue
            if response.status_code == 405 and b"data-spm" in response.content:
                # WAF 挑战是上游每次独立抽签：这次尝试没有到达业务层，**不计费**，
                # 否则通过率 ~50% 会让额度按 ~2 倍的速率被烧掉、把客户逼进冷却。
                attempts += 1
                waf_attempts += 1
                logger.debug("waf challenge %s/%s on %s", waf_attempts, self.settings.waf_retry_limit, url)
                if waf_attempts >= self.settings.waf_retry_limit:
                    raise ScanError(ErrorCode.WAF_CHALLENGE, "WAF challenge not cleared")
                # 挑战是每次独立抽签：退避不提高通过率，只加时延，因此近即时重试。
                self._sleep(self.settings.waf_retry_delay_seconds)
                continue
            # 到达业务层的应答（含 401/403/429/5xx）才算一次上游事务。
            self._call_quota(quota_kind, charge=True)
            if response.status_code == 401 and authorized:
                if auth_retries >= 2 or self._token_provider is None:
                    raise ScanError(ErrorCode.AUTH_REQUIRED, "access token rejected")
                auth_retries += 1
                rejected = self._access_token
                refreshed = self._run_token_provider(rejected)
                if not refreshed or refreshed == rejected:
                    # The credential could not be rotated; retrying with the
                    # same token would only burn the remaining budget.
                    raise ScanError(ErrorCode.AUTH_REQUIRED, "token refresh did not rotate the credential")
                self._access_token = refreshed
                continue
            if response.status_code in (403, 429):
                # 风控/限速：退避重试有限次，仍被拒绝就交给账号级冷却处理，
                # 不能继续对同一账号逐店空转。
                attempts += 1
                if attempts >= self.settings.request_retry_limit:
                    raise ScanError(
                        ErrorCode.THROTTLED,
                        f"upstream rejected the request ({response.status_code})",
                    )
                self._sleep(self._retry_delay(attempts))
                continue
            if response.status_code >= 500:
                attempts += 1
                if attempts >= self.settings.request_retry_limit:
                    raise ScanError(
                        ErrorCode.UNEXPECTED_STATUS, f"upstream {response.status_code}"
                    )
                self._sleep(self._retry_delay(attempts))
                continue
            return response
        raise NikeTransportError("request retries exhausted")

    def _call_quota(self, quota_kind: str, *, charge: bool) -> None:
        """Ask the quota guard about an upstream transaction.

        ``charge=False`` is the pre-flight check: it never counts, so a request
        the budget cannot afford is not even sent. ``charge=True`` books the
        transaction after the upstream actually answered.
        """
        if not quota_kind or self.transaction_listener is None:
            return
        if not self.transaction_listener(quota_kind, charge):
            raise ScanError(
                ErrorCode.BUDGET_EXHAUSTED,
                "upstream transaction budget exhausted",
            )

    def _retry_delay(self, attempts: int) -> float:
        """Exponential backoff for upstream retries, capped by config.

        405 challenge / 5xx / 403 / 429 / transport errors all share it: the
        first retry is short (the challenge usually clears on the next
        round-trip), later retries stretch out but never run away.
        """
        base = self.settings.request_retry_base_seconds
        ceiling = self.settings.request_retry_max_seconds
        return min(base * (2 ** max(0, attempts - 1)), ceiling)

    def _run_credential_guard(self) -> None:
        assert self._credential_guard is not None
        try:
            self._credential_guard()
        except ScanError:
            raise
        except Exception as exc:  # noqa: BLE001 - credential errors carry stable codes
            code = getattr(exc, "code", None)
            if isinstance(code, str) and code.startswith("NIKE_"):
                raise ScanError(ErrorCode.AUTH_REQUIRED, code) from exc
            raise

    def _run_token_provider(self, rejected: str | None) -> str:
        assert self._token_provider is not None
        try:
            return self._token_provider(rejected)
        except ScanError:
            raise
        except Exception as exc:  # noqa: BLE001 - credential errors carry stable codes
            code = getattr(exc, "code", None)
            if isinstance(code, str) and code.startswith("NIKE_"):
                raise ScanError(ErrorCode.AUTH_REQUIRED, code) from exc
            raise

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
        return self.parse_structure(store_code, style_code, products[0])

    #: 门店尺码表批量上限：实测 ≤30 正常，40/50 被**静默截断**到 30，≥80 直接 500。
    STRUCTURE_BATCH_SIZE = 30

    def fetch_structures(
        self, store_code: str, style_codes: list[str]
    ) -> dict[str, SizeStructure]:
        """一次取多款的门店尺码表（GTIN 是**门店级**的，所以不能按款缓存）。

        这个端点（`store_number/{store}?productCodes=...`）免 token、几乎不触发 WAF，
        把"每 (店, 款) 一个请求"压成"每店 ⌈款数/30⌉ 个请求"——31 款从 31 次变 2 次。

        响应里没有的货号按 ``MISSING`` 处理（未知绝不当作结论）；某一批返回非 200 时该批
        按 MISSING，而重试耗尽的传输/限流错误照旧抛出——上层把门店记失败并重试，
        绝不猜成"无货"。
        """
        results: dict[str, SizeStructure] = {
            code: SizeStructure(store_code, code, "MISSING", code) for code in style_codes
        }
        for start in range(0, len(style_codes), self.STRUCTURE_BATCH_SIZE):
            chunk = style_codes[start : start + self.STRUCTURE_BATCH_SIZE]
            url = (
                f"{self.settings.nike_test_api_base}/store/product_details/internal/v1/"
                f"marketplace/CN/language/zh-Hans/store_number/{store_code}"
                f"?productCodes={','.join(chunk)}&fields=products(productCode,sizes)"
            )
            response = self._request("GET", url, authorized=False)
            if response.status_code != 200:
                continue
            try:
                payload = response.json()
            except ValueError:
                continue
            for product in payload.get("products") or []:
                code = str(product.get("productCode") or "").strip()
                if code in results:
                    results[code] = self.parse_structure(store_code, code, product)
        return results

    @staticmethod
    def parse_structure(store_code: str, style_code: str, product: dict) -> SizeStructure:
        """把一个 product 节点解析成尺码表；缺件即 ``MISSING``（不是"无货"）。"""
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
        # 首次轮询很短：绝大多数 review 在 PUT 返回时就已就绪；未就绪才逐步拉长，
        # 上限仍是 review_poll_interval_seconds，总等待窗口与重试次数保持不变。
        delay = self.settings.review_poll_first_seconds
        for _ in range(self.settings.review_poll_tries):
            self._sleep(delay)
            delay = min(delay * 2, self.settings.review_poll_interval_seconds)
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
