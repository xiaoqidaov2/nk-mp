"""Per-store probe: size structure, availability, then price refinement."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import GtinStructure, Observation, PriceStatus, Store, Style, Trust
from app.nike.client import NikeClient, NikeTransportError
from app.nike.errors import ErrorCode, ScanError
from app.nike.types import PriceInfo, SizeEntry, SizeStructure
from app.services.quota import QuotaGuard


@dataclass
class ProbeResult:
    store_code: str
    observations: list[Observation]
    error_code: str | None = None
    message: str = ""
    trusted: bool = True
    observations_written: int = 0


def _hash_structure(structure: SizeStructure) -> str:
    payload = json.dumps(
        [[entry.size_label, entry.gtin] for entry in structure.sizes], ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_or_fetch_structure(
    session: Session,
    client: NikeClient,
    settings: Settings,
    store: Store,
    style: Style,
    force: bool = False,
) -> tuple[SizeStructure, bool]:
    """Return structure plus ``structure_changed`` flag."""
    now = datetime.now(UTC)
    cached = session.execute(
        select(GtinStructure).where(
            GtinStructure.store_id == store.id, GtinStructure.style_id == style.id
        )
    ).scalar_one_or_none()

    if cached is not None and not force and cached.expires_at is not None and cached.expires_at > now:
        sizes = tuple(
            (entry["size_label"], entry["gtin"])  # type: ignore[index]
            for entry in (cached.sizes or [])
        )
        structure = SizeStructure(
            store_code=store.store_code,
            style_code=style.style_code,
            status=cached.status,
            title=style.title or style.style_code,
            sizes=tuple(SizeEntry(size_label=size, gtin=gtin) for size, gtin in sizes),
        )
        return structure, False

    structure = client.fetch_structure(store.store_code, style.style_code)
    changed = False
    if cached is not None and cached.status == "OK" and structure.status == "OK":
        changed = _hash_structure(structure) != cached.structure_hash

    if cached is None:
        cached = GtinStructure(store_id=store.id, style_id=style.id)
        session.add(cached)
    cached.status = structure.status
    cached.structure_hash = _hash_structure(structure)
    cached.sizes = [
        {"size_label": entry.size_label, "gtin": entry.gtin} for entry in structure.sizes
    ]
    cached.fetched_at = now
    cached.expires_at = now + timedelta(days=settings.structure_ttl_days)

    if structure.title and structure.title != style.style_code:
        style.title = structure.title
    return structure, changed


def probe_store(
    session: Session,
    client: NikeClient,
    settings: Settings,
    quota: QuotaGuard,
    store: Store,
    style_codes: list[str],
    job_id=None,
) -> ProbeResult:
    now = datetime.now(UTC)
    observations: list[Observation] = []

    styles: dict[str, Style] = {}
    structures: dict[str, SizeStructure] = {}
    for style_code in style_codes:
        style = session.execute(
            select(Style).where(Style.style_code == style_code)
        ).scalar_one_or_none()
        if style is None:
            style = Style(style_code=style_code)
            session.add(style)
            session.flush()
        styles[style_code] = style
        structures[style_code] = get_or_fetch_structure(
            session, client, settings, store, style
        )[0]

    missing_styles = [code for code in style_codes if not structures[code].sizes]
    for style_code in missing_styles:
        structure = get_or_fetch_structure(
            session, client, settings, store, styles[style_code], force=True
        )[0]
        structures[style_code] = structure

    plan: list[tuple[str, str, str]] = []
    for style_code in style_codes:
        for entry in structures[style_code].sizes:
            plan.append((entry.gtin, style_code, entry.size_label))

    if not plan:
        return ProbeResult(
            store_code=store.store_code,
            observations=[],
            error_code=ErrorCode.STRUCTURE_MISSING.value,
            message="no size structure available for the requested styles",
            trusted=False,
        )

    if not quota.try_consume(session, store, len(plan), kind="availability"):
        return ProbeResult(
            store_code=store.store_code,
            observations=[],
            error_code=ErrorCode.BATCH_SUSPECT.value,
            message="daily review budget exhausted",
            trusted=False,
        )

    availability: dict[str, str] = {}
    prices: dict[str, PriceInfo] = {}
    batch = None
    for start in range(0, len(plan), settings.availability_chunk_size):
        chunk = plan[start : start + settings.availability_chunk_size]
        gtins = [item[0] for item in chunk]
        try:
            batch = client.review(gtins, region_id=store.region_id)
        except (ScanError, NikeTransportError) as exc:
            code = exc.code.value if isinstance(exc, ScanError) else ErrorCode.TRANSPORT_ERROR.value
            return ProbeResult(
                store_code=store.store_code,
                observations=[],
                error_code=code,
                message=str(exc),
                trusted=False,
            )
        if not batch.trusted:
            return ProbeResult(
                store_code=store.store_code,
                observations=[],
                error_code=batch.error_code or ErrorCode.BATCH_SUSPECT.value,
                message=batch.message,
                trusted=False,
            )
        for gtin, item in batch.items.items():
            availability[gtin] = item.availability
            if item.price is not None:
                prices[gtin] = item.price

    in_stock = [gtin for gtin, status in availability.items() if status == "IN_STOCK"]
    if in_stock and not quota.try_consume(session, store, len(in_stock), kind="price"):
        # Size-level stock is still trustworthy; prices stay missing.
        prices = {}
    else:
        for start in range(0, len(in_stock), settings.price_chunk_size):
            group = in_stock[start : start + settings.price_chunk_size]
            try:
                price_batch = client.review(group, region_id=store.region_id, with_price=True)
            except (ScanError, NikeTransportError):
                continue
            if not price_batch.trusted:
                continue
            for gtin, item in price_batch.items.items():
                if item.price is not None:
                    prices[gtin] = item.price
            for gtin, item in price_batch.items.items():
                if item.availability != "IN_STOCK":
                    availability[gtin] = item.availability

    for gtin, style_code, size_label in plan:
        status = availability.get(gtin, "UNKNOWN")
        price = prices.get(gtin)
        observations.append(
            Observation(
                store_id=store.id,
                style_id=styles[style_code].id,
                size_label=size_label,
                gtin=gtin,
                availability=status,
                trust=Trust.TRUSTED.value,
                price_status=(
                    PriceStatus.OK.value
                    if price is not None and price.total_price is not None
                    else PriceStatus.MISSING.value
                ),
                price=price.price if price else None,
                full_price=price.full_price if price else None,
                discount_amount=price.discount_amount if price else None,
                total_price=price.total_price if price else None,
                promotions=price.promotions if price else [],
                job_id=job_id,
                checked_at=now,
            )
        )

    return ProbeResult(store_code=store.store_code, observations=observations)
