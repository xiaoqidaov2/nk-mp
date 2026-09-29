"""Offline upstream stub for the QA environment.

Emulates the read-only Nike endpoints the platform calls (regions, product
structure, cart reviews and the OAuth refresh) using a tiny in-memory model.
It never talks to the real Nike service; a scenario switch lets the smoke
test drive honest state transitions.

Scenarios:
- ``baseline``: size 40 is out of stock, the rest is in stock. Prices are
  returned when every requested item is buyable.
- ``restock``: everything is in stock (triggers RESTOCK for size 40).
- ``untrusted``: every review answers with an empty COMPLETED payload, which
  must map to UNKNOWN rather than a stock conclusion.
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Nike MP offline stub", version="0.1.0")

SCENARIOS = {"baseline", "restock", "untrusted"}
state = {"scenario": "baseline"}
reviews: dict[str, list[dict]] = {}

STORES = [
    {
        "id": "NIKE0S001",
        "storeCode": "QA001",
        "displayName": "离线测试一店",
        "storeAddress": "测试路 1 号",
        "concept": "NK",
    },
    {
        "id": "NIKE0S002",
        "storeCode": "QA002",
        "displayName": "离线测试二店",
        "storeAddress": "测试路 2 号",
        "concept": "NK",
    },
]

# Store-scoped size structures, exactly like the real upstream: the same style
# can have different GTINs per store.
SIZES_BY_STORE: dict[str, list[tuple[str, str]]] = {
    "QA001": [("40", "QA001-40"), ("42", "QA001-42"), ("44", "QA001-44")],
    "QA002": [("40", "QA002-40"), ("42", "QA002-42"), ("44", "QA002-44"), ("46", "QA002-46")],
}


class ScenarioIn(BaseModel):
    scenario: str


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "scenario": state["scenario"]}


@app.post("/__scenario")
def set_scenario(payload: ScenarioIn) -> dict:
    if payload.scenario not in SCENARIOS:
        return {"ok": False, "message": f"unknown scenario {payload.scenario}"}
    state["scenario"] = payload.scenario
    return {"ok": True, "scenario": state["scenario"]}


@app.post("/token/v1")
def refresh_token() -> dict:
    return {"access_token": f"qa-token-{uuid.uuid4().hex[:12]}", "refresh_token": "qa-refresh-token"}


@app.get("/o2o/buy/regions/v1")
def list_regions(anchor: int = 0, count: int = 200) -> dict:  # noqa: ARG001
    return {"objects": STORES}


@app.get(
    "/store/product_details/v1/marketplace/CN/language/zh-Hans/store_number/{store}/style_color/{style}"
)
def product_structure(store: str, style: str) -> dict:
    sizes = SIZES_BY_STORE.get(store.upper(), [])
    if not sizes:
        return {"productGroupings": []}
    return {
        "productGroupings": [
            {
                "products": [
                    {
                        "copy": {"fullTitle": f"离线货号 {style.upper()}"},
                        "sizes": [
                            {"localizedLabel": label, "gtins": [{"gtin": gtin}]}
                            for label, gtin in sizes
                        ],
                    }
                ]
            }
        ]
    }


@app.put("/o2o/buy/cart_reviews/v2/{request_id}")
def submit_review(request_id: str, payload: dict) -> dict:
    items = ((payload.get("request") or {}).get("items")) or []
    reviews[request_id] = [str(item.get("skuId") or "") for item in items]
    return {}


def _is_out_of_stock(sku: str) -> bool:
    if state["scenario"] == "restock":
        return False
    return sku.endswith("-40")


@app.get("/o2o/buy/cart_reviews/v2/{request_id}")
def poll_review(request_id: str) -> dict:
    gtins = reviews.get(request_id, [])
    if state["scenario"] == "untrusted":
        return {"response": {"fulfillmentGroups": []}}

    errors = []
    for index, gtin in enumerate(gtins):
        if _is_out_of_stock(gtin):
            errors.append(
                {"field": f"/request/items/{index}/skuId", "code": "INVENTORY_INSUFFICIENT"}
            )
    if errors:
        return {"error": {"errors": errors}}

    items = []
    for gtin in gtins:
        items.append(
            {
                "skuId": gtin,
                "itemCosts": {
                    "priceInfo": {"price": 699, "fullPrice": 999, "discount": 300, "total": 699},
                    "promotionDiscounts": [{"displayName": "离线测试促销"}],
                },
            }
        )
    return {"response": {"fulfillmentGroups": [{"items": items}]}}
