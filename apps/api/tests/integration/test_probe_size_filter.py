"""探针按任务级尺码过滤：空选=全部；命中则只探测所选；全未命中返回可信空结果。

不把"所选尺码这家店没有"当成门店失败（否则会污染失败计数与自动补跑）。
"""

from __future__ import annotations

from app.config import get_settings
from app.db import SessionLocal
from app.nike.types import ReviewBatch, ReviewItem, SizeEntry, SizeStructure
from app.services.probe import probe_store
from app.services.quota import QuotaGuard

from .helpers import make_store, make_style, make_tenant, make_user


class _StructureClient:
    """脚本化上游：固定返回多尺码结构，可用性一律在货存。"""

    def __init__(self, sizes: tuple[str, ...]) -> None:
        self.sizes = sizes
        self.transaction_listener = None

    def fetch_structure(self, store_code: str, style_code: str) -> SizeStructure:
        return SizeStructure(
            store_code,
            style_code,
            "OK",
            "测试标题",
            tuple(SizeEntry(label, f"G-{label}") for label in self.sizes),
        )

    def fetch_structures(  # noqa: ANN001 - 生产走批量端点，这里逐款忠实返回
        self, store_code: str, style_codes: list[str]
    ) -> dict[str, SizeStructure]:
        return {code: self.fetch_structure(store_code, code) for code in style_codes}

    def review(self, gtins, region_id, with_price=False):  # noqa: ANN001
        return ReviewBatch(
            items={gtin: ReviewItem(gtin=gtin, availability="IN_STOCK") for gtin in gtins},
            trusted=True,
        )


def _seed():
    """返回 (store_id, style_code)；门店/货号已提交，尺码结构由假客户端提供。"""
    with SessionLocal() as db:
        tenant = make_tenant(db, budget=100000)
        user = make_user(db, tenant, email="probe-size@example.com")
        store = make_store(db, owner=user, code="S1", region_id="R1")
        make_style(db, owner=user, code="DN3707-202")
        db.commit()
        return store.id, "DN3707-202"


def _probe(store_id, style_code, sizes, **kwargs):
    from app.models import Store

    settings = get_settings()
    with SessionLocal() as db:
        store = db.get(Store, store_id)
        quota = QuotaGuard(
            tenant_id=store.owner_user_id,
            global_budget=100000,
            tenant_budget=100000,
            hourly_cap=100000,
        )
        return probe_store(
            db, _StructureClient(sizes), settings, quota, store, [style_code], **kwargs
        )


def test_no_size_selection_probes_every_size() -> None:
    store_id, style_code = _seed()
    result = _probe(store_id, style_code, ("42.5", "XL"))
    assert result.trusted is True
    assert result.error_code is None
    assert sorted(obs.size_label for obs in result.observations) == ["42.5", "XL"]


def test_selected_size_keeps_only_that_size() -> None:
    store_id, style_code = _seed()
    result = _probe(store_id, style_code, ("42.5", "XL"), size_labels=["42.5"])
    assert result.trusted is True
    assert result.error_code is None
    assert [obs.size_label for obs in result.observations] == ["42.5"]


def test_absent_size_returns_trusted_empty_result() -> None:
    store_id, style_code = _seed()
    result = _probe(store_id, style_code, ("42.5", "XL"), size_labels=["39"])
    assert result.trusted is True
    assert result.error_code is None
    assert result.observations == []
    assert result.message == "所选尺码在这家店不存在"

def test_missing_structure_caches_only_a_short_negative_ttl() -> None:
    """缺尺码的负结果只缓存 structure_missing_ttl_seconds（默认 30 分钟），不是 7 天。

    上游契约变化（批量端点被收回）时，7 天负缓存会让所有新货号在全部门店一直读成
    "缺尺码"；短负缓存让平台在一个 TTL 内自愈。
    """
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from app.models import GtinStructure, Store, Style

    class _MissingStructureClient(_StructureClient):
        def fetch_structure(self, store_code: str, style_code: str) -> SizeStructure:
            return SizeStructure(store_code, style_code, "MISSING", style_code)

    store_id, style_code = _seed()
    settings = get_settings()
    now = datetime.now(UTC)
    with SessionLocal() as db:
        store = db.get(Store, store_id)
        style = db.execute(
            select(Style).where(Style.owner_user_id == store.owner_user_id)
        ).scalar_one()
        quota = QuotaGuard(
            tenant_id=store.owner_user_id,
            global_budget=100000,
            tenant_budget=100000,
            hourly_cap=100000,
        )
        result = probe_store(
            db, _MissingStructureClient(()), settings, quota, store, [style_code]
        )
        assert result.error_code == "STRUCTURE_MISSING"
        row = db.execute(
            select(GtinStructure).where(
                GtinStructure.store_id == store.id, GtinStructure.style_id == style.id
            )
        ).scalar_one()
        assert row.status == "MISSING"
        assert row.expires_at is not None
        remaining = (row.expires_at - now).total_seconds()
        assert remaining <= settings.structure_missing_ttl_seconds + 5
        assert remaining > settings.structure_missing_ttl_seconds - 60
        assert remaining < timedelta(days=1).total_seconds()
