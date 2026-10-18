"""Per-store probe: size structure, availability, then price refinement."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
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
    # style_id -> new structure hash for structures that changed this round
    structure_changes: dict = field(default_factory=dict)


def _hash_structure(structure: SizeStructure) -> str:
    payload = json.dumps(
        [[entry.size_label, entry.gtin] for entry in structure.sizes], ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _ensure_structure_row(session: Session, store: Store, style: Style) -> GtinStructure:
    """Get the (owner, store, style) structure row, creating it if missing.

    Two workers can probe the same store/style at the same time; the unique
    constraint makes the losing insert a no-op instead of an IntegrityError.
    """
    session.execute(
        pg_insert(GtinStructure)
        .values(
            owner_user_id=store.owner_user_id,
            store_id=store.id,
            style_id=style.id,
        )
        .on_conflict_do_nothing(
            index_elements=["owner_user_id", "store_id", "style_id"]
        )
    )
    return session.execute(
        select(GtinStructure).where(
            GtinStructure.owner_user_id == store.owner_user_id,
            GtinStructure.store_id == store.id,
            GtinStructure.style_id == style.id,
        )
    ).scalar_one()


def _persist_structure(
    session: Session,
    store: Store,
    style: Style,
    cached: GtinStructure | None,
    structure: SizeStructure,
    now: datetime,
    settings: Settings,
) -> tuple[GtinStructure, bool]:
    """写回结构行并报告"结构是否变了"（旧值只在两边都 OK 时才可比）。"""
    changed = False
    if cached is not None and cached.status == "OK" and structure.status == "OK":
        changed = _hash_structure(structure) != cached.structure_hash
    if cached is None:
        cached = _ensure_structure_row(session, store, style)
    cached.status = structure.status
    cached.structure_hash = _hash_structure(structure)
    cached.sizes = [
        {"size_label": entry.size_label, "gtin": entry.gtin} for entry in structure.sizes
    ]
    cached.fetched_at = now
    if structure.status == "OK":
        cached.expires_at = now + timedelta(days=settings.structure_ttl_days)
    else:
        # 负结果只在短窗口内有效：上游契约变化（批量端点被收回、货号暂未铺货）
        # 绝不能被缓存 7 天，否则所有新货号会在全部门店一直读成"缺尺码"。
        cached.expires_at = now + timedelta(seconds=max(60, settings.structure_missing_ttl_seconds))
    if structure.title and structure.title != style.style_code:
        style.title = structure.title
    return cached, changed


def _cached_structure(store: Store, style: Style, cached: GtinStructure) -> SizeStructure:
    sizes = tuple(
        (entry["size_label"], entry["gtin"])  # type: ignore[index]
        for entry in (cached.sizes or [])
    )
    return SizeStructure(
        store_code=store.store_code,
        style_code=style.style_code,
        status=cached.status,
        title=style.title or style.style_code,
        sizes=tuple(SizeEntry(size_label=size, gtin=gtin) for size, gtin in sizes),
    )


def get_or_fetch_structures(
    session: Session,
    client: NikeClient,
    settings: Settings,
    store: Store,
    styles: list[Style],
) -> dict[str, tuple[SizeStructure, bool]]:
    """这家店一次取回多个款的结构（只有缺失/过期的才发请求）。

    为什么批量：GTIN 是**门店级**的（同款在不同门店的 GTIN 不同），不能按款缓存；但这个
    端点允许一次带 ≤30 个货号，于是"每 (店, 款) 一个请求"变成"每店 ⌈款数/30⌉ 个请求"
    ——31 款从 31 次变 2 次，缺 794 对时从 794 次变约 27 次。
    """
    now = datetime.now(UTC)
    cached_rows = {
        row.style_id: row
        for row in session.execute(
            select(GtinStructure).where(
                GtinStructure.owner_user_id == store.owner_user_id,
                GtinStructure.store_id == store.id,
                GtinStructure.style_id.in_([style.id for style in styles]),
            )
        ).scalars()
    }
    results: dict[str, tuple[SizeStructure, bool]] = {}
    pending: list[Style] = []
    for style in styles:
        cached = cached_rows.get(style.id)
        if cached is not None and cached.expires_at is not None and cached.expires_at > now:
            results[style.style_code] = (_cached_structure(store, style, cached), False)
        else:
            pending.append(style)

    if pending:
        fetched = client.fetch_structures(store.store_code, [style.style_code for style in pending])
        for style in pending:
            structure = fetched[style.style_code]
            _row, changed = _persist_structure(
                session, store, style, cached_rows.get(style.id), structure, now, settings
            )
            results[style.style_code] = (structure, changed)
    return results


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
            GtinStructure.owner_user_id == store.owner_user_id,
            GtinStructure.store_id == store.id,
            GtinStructure.style_id == style.id,
        )
    ).scalar_one_or_none()

    if cached is not None and not force and cached.expires_at is not None and cached.expires_at > now:
        return _cached_structure(store, style, cached), False

    structure = client.fetch_structure(store.store_code, style.style_code)
    _row, changed = _persist_structure(session, store, style, cached, structure, now, settings)
    return structure, changed


def probe_store(
    session: Session,
    client: NikeClient,
    settings: Settings,
    quota: QuotaGuard,
    store: Store,
    style_codes: list[str],
    job_id=None,
    *,
    size_labels: list[str] | None = None,
) -> ProbeResult:
    now = datetime.now(UTC)
    observations: list[Observation] = []

    styles: dict[str, Style] = {}
    structures: dict[str, SizeStructure] = {}
    changed_structures: dict = {}
    for style_code in style_codes:
        style = session.execute(
            select(Style).where(
                Style.owner_user_id == store.owner_user_id, Style.style_code == style_code
            )
        ).scalar_one_or_none()
        if style is None:
            # 同一轮里多个门店会并发探测同一个货号：让输掉插入的线程改读已存在行，
            # 而不是抛 IntegrityError。
            session.execute(
                pg_insert(Style)
                .values(owner_user_id=store.owner_user_id, style_code=style_code)
                .on_conflict_do_nothing(index_elements=["owner_user_id", "style_code"])
            )
            style = session.execute(
                select(Style).where(
                    Style.owner_user_id == store.owner_user_id,
                    Style.style_code == style_code,
                )
            ).scalar_one()
        styles[style_code] = style

    # 一次取回这家店所有款的结构（缺失/过期的才发请求）：31 款 = 2 个请求，而不是 31 个。
    fetched_structures = get_or_fetch_structures(
        session, client, settings, store, list(styles.values())
    )
    for style_code, (structure, changed) in fetched_structures.items():
        structures[style_code] = structure
        if changed and structure.status == "OK":
            changed_structures[styles[style_code].id] = _hash_structure(structure)

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

    # 任务级尺码选择：空列表 = 全部尺码，行为与不传完全一致。
    if size_labels:
        wanted = set(size_labels)
        plan = [item for item in plan if item[2] in wanted]
        if not plan:
            # 所选尺码在这家店不存在：这不是门店失败，返回可信的空结果，
            # 不写 UNKNOWN 门店状态（否则会被失败门店逻辑误判）。
            return ProbeResult(
                store_code=store.store_code,
                observations=[],
                message="所选尺码在这家店不存在",
                structure_changes=changed_structures,
            )

    # Every real upstream transaction (PUT, poll GET, retry) consumes quota at
    # the moment the request is issued; exhausting the budget aborts the probe
    # with an UNKNOWN result instead of a guessed one.
    client.transaction_listener = lambda kind, charge: quota.consume_transaction(
        session, kind, charge=charge
    )

    availability: dict[str, str] = {}
    prices: dict[str, PriceInfo] = {}
    for start in range(0, len(plan), settings.availability_chunk_size):
        chunk = plan[start : start + settings.availability_chunk_size]
        gtins = [item[0] for item in chunk]
        try:
            batch = client.review(gtins, region_id=store.region_id)
        except (ScanError, NikeTransportError) as exc:
            if isinstance(exc, ScanError) and exc.code == ErrorCode.AUTH_REQUIRED:
                # The credential is gone; swallowing it into UNKNOWN would
                # keep scanning other stores with a dead binding.
                raise
            code = exc.code.value if isinstance(exc, ScanError) else ErrorCode.TRANSPORT_ERROR.value
            return ProbeResult(
                store_code=store.store_code,
                observations=[],
                error_code=code,
                message=str(exc),
                trusted=False,
                structure_changes=changed_structures,
            )
        if not batch.trusted:
            return ProbeResult(
                store_code=store.store_code,
                observations=[],
                error_code=batch.error_code or ErrorCode.BATCH_SUSPECT.value,
                message=batch.message,
                trusted=False,
                structure_changes=changed_structures,
            )
        for gtin, item in batch.items.items():
            availability[gtin] = item.availability
            if item.price is not None:
                prices[gtin] = item.price

    in_stock = [gtin for gtin, status in availability.items() if status == "IN_STOCK"]
    # 小批量的可用性响应本身就可能带价格：只为「还缺价」的在货尺码再发价格请求，
    # 避免同一价格查询两次（每次都是一次 PUT + 轮询，直接决定单店耗时）。
    missing_price = [gtin for gtin in in_stock if gtin not in prices]
    for start in range(0, len(missing_price), settings.price_chunk_size):
        group = missing_price[start : start + settings.price_chunk_size]
        try:
            price_batch = client.review(group, region_id=store.region_id, with_price=True)
        except ScanError as exc:
            if exc.code == ErrorCode.AUTH_REQUIRED:
                raise
            if exc.code == ErrorCode.BUDGET_EXHAUSTED:
                break
            continue
        except NikeTransportError:
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

    return ProbeResult(
        store_code=store.store_code,
        observations=observations,
        structure_changes=changed_structures,
    )
