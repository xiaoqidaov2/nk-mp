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


def _create_task(client, csrf, *, size_labels=None, name="尺码任务") -> str:
    payload = {
        "name": name,
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


def test_scan_list_by_task_includes_merged_jobs(client) -> None:
    """合并采集作业（task_id 为空、归属写在 task_ids 里）要出现在任务详情页的扫描列表里。

    否则客户升级后会发现"任务页一条扫描记录都没有"——因为作业都被合并掉了。
    """
    seed = _seed()
    csrf = login(client, "scope@example.com")
    task_a = _create_task(client, csrf, name="任务 A")
    task_b = _create_task(client, csrf, name="任务 B")

    with SessionLocal() as db:
        merged = ScanJob(
            tenant_id=seed["tenant_id"],
            owner_user_id=seed["user_id"],
            kind="MONITOR",
            status="QUEUED",
            store_codes=["S001", "S002"],
            style_codes=["DN3707-202"],
            task_id=None,
            task_ids=[task_a, task_b],
        )
        single = ScanJob(
            tenant_id=seed["tenant_id"],
            owner_user_id=seed["user_id"],
            kind="MONITOR",
            status="QUEUED",
            store_codes=["S001"],
            style_codes=["DN3707-202"],
            task_id=task_b,
            task_ids=[],
        )
        db.add_all([merged, single])
        db.commit()
        merged_id, single_id = str(merged.id), str(single.id)

    listed = client.get(
        f"/api/v1/scans?task_id={task_a}", headers=auth_headers(csrf)
    )
    assert listed.status_code == 200, listed.text
    rows = listed.json()
    assert [row["id"] for row in rows] == [merged_id]
    assert set(rows[0]["task_ids"]) == {task_a, task_b}

    # 单任务作业仍按 task_id 归属，B 能看到两条
    for_task_b = client.get(
        f"/api/v1/scans?task_id={task_b}", headers=auth_headers(csrf)
    ).json()
    assert {row["id"] for row in for_task_b} == {merged_id, single_id}