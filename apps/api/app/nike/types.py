from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class StoreRecord:
    store_code: str
    region_id: str
    display_name: str
    address: str = ""
    concept: str = ""


@dataclass(frozen=True)
class SizeEntry:
    size_label: str
    gtin: str


@dataclass(frozen=True)
class SizeStructure:
    store_code: str
    style_code: str
    status: str
    title: str
    sizes: tuple[SizeEntry, ...] = ()


@dataclass
class PriceInfo:
    price: float | None = None
    full_price: float | None = None
    discount_amount: float | None = None
    total_price: float | None = None
    promotions: list[str] = field(default_factory=list)


@dataclass
class ReviewItem:
    gtin: str
    availability: str
    price: PriceInfo | None = None


@dataclass
class ReviewBatch:
    """Outcome of one cart_reviews request.

    ``trusted`` is False whenever the response cannot be interpreted with
    confidence. Untrusted batches must never reach the state machine.
    """

    items: dict[str, ReviewItem] = field(default_factory=dict)
    trusted: bool = True
    error_code: str | None = None
    message: str = ""

    def in_stock_gtins(self) -> list[str]:
        return [gtin for gtin, item in self.items.items() if item.availability == "IN_STOCK"]
