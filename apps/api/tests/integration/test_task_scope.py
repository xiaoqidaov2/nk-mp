"""任务级尺码选择：PATCH 往返与"从任务建作业时冻结尺码"。

真实 PostgreSQL：走 HTTP 接口建/改任务，再读 scan_job 断言冻结结果。
"""

from __future__ import annotations

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import MonitorTask, ScanJob

from .helpers import (
    auth_headers,
    login,
    make_store,
    make_style,
    make_tenant,
    make_user,
    store_credential,
)


def _seed() -> dict:
    settings = get_settings()
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant, email="scope@example.com")
        make_store(db, owner=user, code="S001")
        make_store(db, owner=user, code="S002")
        make_style(db, owner=user, code="DN3707-202")
        db.commit()
        seed = {"tenant_id": tenant.id, "user_id": user.id}
    seed["binding"] = store_credential(settings, seed["user_id"])
    return seed


def _create_task(client, csrf, *, size_labels=None) -> str:
    payload = {
        "name": "尺码任务",
        "store_codes": ["S001"],
        "style_codes": ["DN3707-202"],
    }
    if size_labels is not None:
        payload["size_labels"] = size_labels
    created = client.post("/api/v1/tasks", json=payload, headers=auth_headers(csrf))
    assert created.status_code == 201, created.text
    return created.json()["id"]


def test_create_task_persists_normalized_size_labels(client) -> None:
    _seed()
    csrf = login(client, "scope@example.com")
    task_id = _create_task(client, csrf, size_labels=[" 42.5 ", "XL", "42.5"])
    body = client.get(f"/api/v1/tasks/{task_id}").json()
    assert body["size_labels"] == ["42.5", "XL"]


def test_patch_size_labels_and_all_stores_roundtrip(client) -> None:
    _seed()
    csrf = login(client, "scope@example.com")
    task_id = _create_task(client, csrf)

    # size_labels 生效且保留大小写。
    patched = client.patch(
        f"/api/v1/tasks/{task_id}",
        json={"size_labels": [" XL ", "42.5", "xl"]},
        headers=auth_headers(csrf),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["size_labels"] == ["XL", "42.5", "xl"]

    # store_codes=[] ⇒ 全部门店，all_stores 置真。
    cleared = client.patch(
        f"/api/v1/tasks/{task_id}",
        json={"store_codes": []},
        headers=auth_headers(csrf),
    )
    assert cleared.status_code == 200, cleared.text
    body = cleared.json()
    assert body["store_codes"] == []
    assert body["all_stores"] is True

    # 指定门店又把它关回去。
    narrowed = client.patch(
        f"/api/v1/tasks/{task_id}",
        json={"store_codes": ["s002"]},
        headers=auth_headers(csrf),
    )
    assert narrowed.status_code == 200, narrowed.text
    assert narrowed.json()["store_codes"] == ["S002"]
    assert narrowed.json()["all_stores"] is False

    with SessionLocal() as db:
        stored = db.get(MonitorTask, task_id)
        assert stored.size_labels == ["XL", "42.5", "xl"]
        assert stored.store_codes == ["S002"]
        assert stored.all_stores is False


def test_scan_from_task_freezes_task_size_labels(client) -> None:
    _seed()
    csrf = login(client, "scope@example.com")
    task_id = _create_task(client, csrf, size_labels=["42.5", "XL"])

    created = client.post(
        "/api/v1/scans",
        json={"task_id": task_id, "store_codes": [], "style_codes": []},
        headers=auth_headers(csrf),
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]

    with SessionLocal() as db:
        job = db.execute(select(ScanJob).where(ScanJob.id == job_id)).scalar_one()
        assert job.size_labels == ["42.5", "XL"]


def test_scan_without_task_defaults_to_all_sizes(client) -> None:
    _seed()
    csrf = login(client, "scope@example.com")

    created = client.post(
        "/api/v1/scans",
        json={"store_codes": ["S001"], "style_codes": ["DN3707-202"]},
        headers=auth_headers(csrf),
    )
    assert created.status_code == 201, created.text

    with SessionLocal() as db:
        job = db.execute(select(ScanJob)).scalar_one()
        assert job.size_labels == []