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