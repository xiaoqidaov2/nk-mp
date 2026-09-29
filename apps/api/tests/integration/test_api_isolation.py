"""T02: session, CSRF and tenant isolation against real PostgreSQL."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.api.deps import CSRF_COOKIE, SESSION_COOKIE
from app.db import SessionLocal
from app.models import ChangeEvent, MonitorTask, Role
from app.services.jobs import enqueue_job

from .helpers import auth_headers, login, make_store, make_style, make_tenant, make_user


def _seed_two_tenants() -> dict:
    with SessionLocal() as db:
        tenant_a = make_tenant(db, name="租户A")
        tenant_b = make_tenant(db, name="租户B")
        make_user(db, tenant_a, email="a@example.com")
        make_user(db, tenant_b, email="b@example.com")
        make_user(db, tenant_a, email="viewer@example.com", role=Role.TENANT_VIEWER.value)
        store = make_store(db, code="S001")
        style = make_style(db, code="DN3707-202")
        db.commit()
        return {
            "tenant_a": tenant_a.id,
            "tenant_b": tenant_b.id,
            "store_id": store.id,
            "style_id": style.id,
        }


def test_cross_tenant_task_access_is_denied(client) -> None:
    _seed_two_tenants()
    csrf_a = login(client, "a@example.com")
    created = client.post(
        "/api/v1/tasks",
        json={"name": "任务A", "store_codes": ["S001"], "style_codes": ["DN3707-202"]},
        headers=auth_headers(csrf_a),
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]

    client.post("/api/v1/auth/logout", headers=auth_headers(csrf_a))
    csrf_b = login(client, "b@example.com")
    assert client.get(f"/api/v1/tasks/{task_id}").status_code == 404
    assert (
        client.patch(
            f"/api/v1/tasks/{task_id}", json={"name": "篡改"}, headers=auth_headers(csrf_b)
        ).status_code
        == 404
    )
    assert client.delete(f"/api/v1/tasks/{task_id}", headers=auth_headers(csrf_b)).status_code == 404
    assert all(task["id"] != task_id for task in client.get("/api/v1/tasks").json())


def test_cross_tenant_scan_access_is_denied(client) -> None:
    seed = _seed_two_tenants()
    with SessionLocal() as db:
        job = enqueue_job(
            db,
            tenant_id=seed["tenant_a"],
            store_codes=["S001"],
            style_codes=["DN3707-202"],
        )
        db.commit()

    login(client, "b@example.com")
    assert client.get(f"/api/v1/scans/{job.id}").status_code == 404
    assert client.get(f"/api/v1/scans/{job.id}/stores").status_code == 404
    assert client.get(f"/api/v1/scans/{job.id}/events").status_code == 404


def test_changes_feed_shows_shared_and_own_events_only(client) -> None:
    seed = _seed_two_tenants()
    with SessionLocal() as db:
        now = datetime.now(UTC)
        shared = ChangeEvent(
            tenant_id=None,
            store_id=seed["store_id"],
            style_id=seed["style_id"],
            size_label="42",
            event_type="RESTOCK",
            dedupe_key=f"shared-{uuid.uuid4()}",
            checked_at=now,
        )
        private_a = ChangeEvent(
            tenant_id=seed["tenant_a"],
            store_id=seed["store_id"],
            style_id=seed["style_id"],
            size_label="42",
            event_type="SCAN_FAILED",
            dedupe_key=f"a-{uuid.uuid4()}",
            checked_at=now,
        )
        private_b = ChangeEvent(
            tenant_id=seed["tenant_b"],
            store_id=seed["store_id"],
            style_id=seed["style_id"],
            size_label="42",
            event_type="SCAN_FAILED",
            dedupe_key=f"b-{uuid.uuid4()}",
            checked_at=now,
        )
        db.add_all([shared, private_a, private_b])
        db.commit()
        shared_id, private_a_id, private_b_id = shared.id, private_a.id, private_b.id

    csrf_a = login(client, "a@example.com")
    ids_a = {row["id"] for row in client.get("/api/v1/changes").json()}
    assert shared_id in ids_a
    assert private_a_id in ids_a
    assert private_b_id not in ids_a

    client.post("/api/v1/auth/logout", headers=auth_headers(csrf_a))
    login(client, "b@example.com")
    ids_b = {row["id"] for row in client.get("/api/v1/changes").json()}
    assert shared_id in ids_b
    assert private_b_id in ids_b
    assert private_a_id not in ids_b


def test_write_requires_csrf_and_write_role(client) -> None:
    _seed_two_tenants()
    csrf_a = login(client, "a@example.com")
    payload = {"name": "无CSRF", "store_codes": ["S001"], "style_codes": ["DN3707-202"]}
    assert client.post("/api/v1/tasks", json=payload).status_code == 403
    assert (
        client.post("/api/v1/tasks", json=payload, headers=auth_headers(csrf_a)).status_code == 201
    )

    client.post("/api/v1/auth/logout", headers=auth_headers(csrf_a))
    csrf_viewer = login(client, "viewer@example.com")
    assert (
        client.post(
            "/api/v1/tasks",
            json={"name": "越权", "store_codes": ["S001"], "style_codes": ["DN3707-202"]},
            headers=auth_headers(csrf_viewer),
        ).status_code
        == 403
    )


def test_session_cookie_can_restore_csrf_after_refresh(client) -> None:
    """The readable CSRF cookie lets a refreshed page write without re-login."""
    _seed_two_tenants()
    login(client, "a@example.com")
    # Simulate a page refresh: the module-level token is gone, but the cookie
    # remains and carries the same value the SPA reads on boot.
    cookie = client.cookies.get(CSRF_COOKIE)
    assert cookie

    response = client.post(
        "/api/v1/tasks",
        json={"name": "刷新后创建", "store_codes": ["S001"], "style_codes": ["DN3707-202"]},
        headers={"X-CSRF-Token": cookie},
    )
    assert response.status_code == 201, response.text


def test_logout_clears_csrf_cookie_and_session(client) -> None:
    _seed_two_tenants()
    csrf = login(client, "a@example.com")
    assert SESSION_COOKIE in client.cookies
    assert CSRF_COOKIE in client.cookies
    client.post("/api/v1/auth/logout", headers=auth_headers(csrf))
    assert SESSION_COOKIE not in client.cookies
    assert CSRF_COOKIE not in client.cookies


def test_ops_read_endpoints_do_not_require_csrf(client) -> None:
    with SessionLocal() as db:
        platform = make_tenant(db, name="服务方", is_platform=True)
        make_user(db, platform, email="ops@example.com", role=Role.OPS_ADMIN.value)
        tenant = make_tenant(db, name="租户A")
        make_user(db, tenant, email="a@example.com")
        db.commit()

    login(client, "ops@example.com")
    for path in ("/api/v1/ops/credentials", "/api/v1/ops/tenants", "/api/v1/ops/health/detail"):
        response = client.get(path)
        assert response.status_code == 200, f"{path}: {response.text}"
    # Mutating ops endpoints still require the CSRF header.
    assert client.post("/api/v1/ops/stores/sync").status_code == 403
    assert client.post("/api/v1/ops/credentials/refresh").status_code == 403
    client.post(
        "/api/v1/auth/logout", headers={"X-CSRF-Token": client.cookies.get(CSRF_COOKIE, "")}
    )

    # A customer tenant must not reach ops endpoints at all.
    login(client, "a@example.com")
    assert client.get("/api/v1/ops/credentials").status_code == 403


def test_unauthenticated_access_is_rejected(client) -> None:
    _seed_two_tenants()
    assert client.get("/api/v1/tasks").status_code == 401
    assert client.get("/api/v1/changes").status_code == 401


def test_scan_list_filters_by_task_id(client) -> None:
    seed = _seed_two_tenants()
    with SessionLocal() as db:
        task = MonitorTask(
            tenant_id=seed["tenant_a"],
            name="任务A",
            store_codes=["S001"],
            style_codes=["DN3707-202"],
            all_stores=False,
        )
        db.add(task)
        db.flush()
        linked = enqueue_job(
            db,
            tenant_id=seed["tenant_a"],
            store_codes=["S001"],
            style_codes=["DN3707-202"],
            task_id=task.id,
            kind="MONITOR",
        )
        manual = enqueue_job(
            db, tenant_id=seed["tenant_a"], store_codes=["S001"], style_codes=["DN3707-202"]
        )
        db.commit()
        task_id, linked_id, manual_id = task.id, linked.id, manual.id

    login(client, "a@example.com")
    rows = client.get(f"/api/v1/scans?task_id={task_id}").json()
    assert [row["id"] for row in rows] == [str(linked_id)]
    assert rows[0]["task_id"] == str(task_id)

    all_ids = {row["id"] for row in client.get("/api/v1/scans").json()}
    assert {str(linked_id), str(manual_id)} <= all_ids
    assert client.get(f"/api/v1/scans?task_id={uuid.uuid4()}").json() == []
