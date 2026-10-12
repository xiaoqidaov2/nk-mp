"""T02: session, CSRF and tenant isolation against real PostgreSQL."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.api.deps import CSRF_COOKIE, SESSION_COOKIE
from app.config import get_settings
from app.db import SessionLocal
from app.models import ChangeEvent, MonitorTask, NotificationChannel, Role, User
from app.services.jobs import enqueue_job

from .helpers import (
    auth_headers,
    login,
    login_session,
    make_store,
    make_style,
    make_tenant,
    make_user,
    store_credential,
)


def _seed_two_tenants() -> dict:
    settings = get_settings()
    with SessionLocal() as db:
        tenant_a = make_tenant(db, name="租户A")
        tenant_b = make_tenant(db, name="租户B")
        user_a = make_user(db, tenant_a, email="a@example.com")
        user_b = make_user(db, tenant_b, email="b@example.com")
        make_user(db, tenant_a, email="viewer@example.com", role=Role.TENANT_VIEWER.value)
        store = make_store(db, owner=user_a, code="S001")
        style = make_style(db, owner=user_a, code="DN3707-202")
        db.commit()
        seed = {
            "tenant_a": tenant_a.id,
            "tenant_b": tenant_b.id,
            "user_a": user_a.id,
            "user_b": user_b.id,
            "store_id": store.id,
            "style_id": style.id,
        }
    # Enqueueing is personal: the acting user needs a frozen Nike binding.
    seed["binding_a"] = store_credential(settings, seed["user_a"])
    return seed


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


def test_duplicate_task_name_returns_a_stable_conflict(client) -> None:
    """同名创建失败必须带固定 error_code，前端才能引导改名重试。"""
    _seed_two_tenants()
    csrf = login(client, "a@example.com")
    payload = {"name": "重复任务", "store_codes": ["S001"], "style_codes": ["DN3707-202"]}
    first = client.post("/api/v1/tasks", json=payload, headers=auth_headers(csrf))
    assert first.status_code == 201, first.text

    second = client.post("/api/v1/tasks", json=payload, headers=auth_headers(csrf))
    assert second.status_code == 409
    body = second.json()
    assert body["error_code"] == "TASK_NAME_CONFLICT"
    assert body["detail"] == "已存在同名任务，请换一个名称"
    assert body["retry_after_seconds"] is None


def test_duplicate_task_rename_returns_a_stable_conflict(client) -> None:
    _seed_two_tenants()
    csrf = login(client, "a@example.com")
    headers = auth_headers(csrf)
    first = client.post(
        "/api/v1/tasks",
        json={"name": "一号", "store_codes": ["S001"], "style_codes": ["DN3707-202"]},
        headers=headers,
    )
    second = client.post(
        "/api/v1/tasks",
        json={"name": "二号", "store_codes": ["S001"], "style_codes": ["DN3707-202"]},
        headers=headers,
    )
    assert first.status_code == 201 and second.status_code == 201, second.text

    renamed = client.patch(
        f"/api/v1/tasks/{second.json()['id']}", json={"name": "一号"}, headers=headers
    )
    assert renamed.status_code == 409
    assert renamed.json()["error_code"] == "TASK_NAME_CONFLICT"


def test_cross_tenant_scan_access_is_denied(client) -> None:
    seed = _seed_two_tenants()
    with SessionLocal() as db:
        job = enqueue_job(
            db,
            tenant_id=seed["tenant_a"],
            store_codes=["S001"],
            style_codes=["DN3707-202"],
            owner_user_id=seed["user_a"],
            credential_binding=seed["binding_a"],
        )
        db.commit()
        job_id = job.id

    login(client, "b@example.com")
    assert client.get(f"/api/v1/scans/{job_id}").status_code == 404
    assert client.get(f"/api/v1/scans/{job_id}/stores").status_code == 404
    assert client.get(f"/api/v1/scans/{job_id}/events").status_code == 404


def test_change_feed_is_owner_scoped(client) -> None:
    """Every event is personal: no shared broadcast, no cross-owner leakage."""
    seed = _seed_two_tenants()
    now = datetime.now(UTC)
    with SessionLocal() as db:
        def _event(tenant_id, owner_user_id, tag: str) -> ChangeEvent:
            return ChangeEvent(
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                store_id=seed["store_id"],
                style_id=seed["style_id"],
                size_label="42",
                event_type="RESTOCK",
                dedupe_key=f"{tag}-{uuid.uuid4()}",
                checked_at=now,
            )

        owned_a = _event(seed["tenant_a"], seed["user_a"], "a")
        owned_b = _event(seed["tenant_b"], seed["user_b"], "b")
        unattributed = _event(None, None, "legacy")
        db.add_all([owned_a, owned_b, unattributed])
        db.commit()
        owned_a_id, owned_b_id, unattributed_id = (
            owned_a.id,
            owned_b.id,
            unattributed.id,
        )

    csrf_a = login(client, "a@example.com")
    ids_a = {row["id"] for row in client.get("/api/v1/changes").json()}
    assert owned_a_id in ids_a
    assert owned_b_id not in ids_a
    assert unattributed_id not in ids_a, "unattributed legacy rows are not served"

    client.post("/api/v1/auth/logout", headers=auth_headers(csrf_a))
    login(client, "b@example.com")
    ids_b = {row["id"] for row in client.get("/api/v1/changes").json()}
    assert owned_b_id in ids_b
    assert owned_a_id not in ids_b
    assert unattributed_id not in ids_b


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


def test_ops_read_endpoints_use_personal_credential_route(client) -> None:
    """Ops reads need no CSRF; the per-user credential view replaces the old one."""
    with SessionLocal() as db:
        platform = make_tenant(db, name="服务方", is_platform=True)
        make_user(db, platform, email="ops@example.com", role=Role.OPS_ADMIN.value)
        tenant = make_tenant(db, name="租户A")
        make_user(db, tenant, email="a@example.com")
        db.commit()

    login(client, "ops@example.com")
    # The logged-in user's own Nike connection is the only credential view now.
    personal = client.get("/api/v1/me/nike-credential")
    assert personal.status_code == 200, personal.text
    assert personal.json()["status"] == "NOT_CONNECTED"
    for path in ("/api/v1/ops/tenants", "/api/v1/ops/health/detail"):
        response = client.get(path)
        assert response.status_code == 200, f"{path}: {response.text}"

    # The legacy platform-wide credential endpoints are gone.
    assert client.get("/api/v1/ops/credentials").status_code == 404
    assert client.post("/api/v1/ops/credentials/refresh").status_code == 404

    # Mutating ops endpoints still require the CSRF header.
    assert client.post("/api/v1/ops/stores/sync").status_code == 403
    client.post(
        "/api/v1/auth/logout", headers={"X-CSRF-Token": client.cookies.get(CSRF_COOKIE, "")}
    )

    # A customer tenant must not reach ops endpoints at all.
    login(client, "a@example.com")
    assert client.get("/api/v1/ops/tenants").status_code == 403


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
            created_by=seed["user_a"],
        )
        db.add(task)
        db.flush()
        linked = enqueue_job(
            db,
            tenant_id=seed["tenant_a"],
            store_codes=["S001"],
            style_codes=["DN3707-202"],
            task_id=task.id,
            requested_by=seed["user_a"],
            kind="MONITOR",
            owner_user_id=seed["user_a"],
            credential_binding=seed["binding_a"],
        )
        manual = enqueue_job(
            db,
            tenant_id=seed["tenant_a"],
            store_codes=["S001"],
            style_codes=["DN3707-202"],
            owner_user_id=seed["user_a"],
            credential_binding=seed["binding_a"],
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


def test_login_locks_after_repeated_failures(client) -> None:
    """连续输错密码会触发短暂锁定；锁定期过后正确密码仍可登录。"""
    settings = get_settings()
    with SessionLocal() as db:
        tenant = make_tenant(db)
        make_user(db, tenant, email="lock@example.com")
        db.commit()

    for _ in range(settings.login_max_attempts - 1):
        response = client.post(
            "/api/v1/auth/login", json={"email": "lock@example.com", "password": "wrong"}
        )
        assert response.status_code == 401

    # 达到阈值的那一次直接进入锁定
    response = client.post(
        "/api/v1/auth/login", json={"email": "lock@example.com", "password": "wrong"}
    )
    assert response.status_code == 429
    assert response.headers.get("Retry-After")
    assert "尝试过多" in response.json()["detail"]

    # 锁定期内即使密码正确也拒绝
    response = client.post(
        "/api/v1/auth/login", json={"email": "lock@example.com", "password": "test-password"}
    )
    assert response.status_code == 429

    # 手动解除锁定后，正确密码登录会清零计数
    with SessionLocal() as db:
        user = db.execute(select(User).where(User.email == "lock@example.com")).scalar_one()
        user.login_locked_until = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    assert login(client, "lock@example.com") is not None

    # 成功一次后再错一次，计数从新窗口开始（不应立即锁定）
    response = client.post(
        "/api/v1/auth/login", json={"email": "lock@example.com", "password": "wrong"}
    )
    assert response.status_code == 401


def test_unknown_email_is_never_locked(client) -> None:
    for _ in range(10):
        response = client.post(
            "/api/v1/auth/login", json={"email": "ghost@example.com", "password": "wrong"}
        )
        assert response.status_code == 401


def test_oversized_channel_field_is_rejected(client) -> None:
    with SessionLocal() as db:
        tenant = make_tenant(db)
        make_user(db, tenant, email="cap@example.com")
        db.commit()
    csrf = login(client, "cap@example.com")
    response = client.post(
        "/api/v1/me/notification-channels",
        json={"platform": "custom", "name": "too-long", "fields": {"url": "json://" + "a" * 600}},
        headers=auth_headers(csrf),
    )
    assert response.status_code == 422


def test_overlong_target_is_rejected_by_the_validator() -> None:
    from app.services import push

    with pytest.raises(push.PushTargetError) as excinfo:
        push.validate_target("json://httpbin.org/post?x=" + "a" * 600, get_settings())
    assert excinfo.value.code == push.PUSH_TARGET_INVALID


def test_csrf_cookie_is_reissued_when_lost(client) -> None:
    """CSRF cookie 丢失后，任意一次带会话的请求都会补发，无需重新登录。"""
    _seed_two_tenants()
    login(client, "a@example.com")
    client.cookies.delete(CSRF_COOKIE)
    assert not client.cookies.get(CSRF_COOKIE)

    response = client.get("/api/v1/auth/me")
    assert response.status_code == 200
    reissued = client.cookies.get(CSRF_COOKIE)
    assert reissued

    created = client.post(
        "/api/v1/tasks",
        json={"name": "补发后创建", "store_codes": ["S001"], "style_codes": ["DN3707-202"]},
        headers={"X-CSRF-Token": reissued},
    )
    assert created.status_code == 201, created.text


def test_concurrent_duplicate_channel_maps_to_duplicate_error() -> None:
    """两个真正并发的相同目标：一个 201、一个 422 重复提示，不允许 500。"""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from fastapi.testclient import TestClient

    from app.main import app

    with SessionLocal() as db:
        tenant = make_tenant(db)
        make_user(db, tenant, email="dup@example.com")
        db.commit()

    client = TestClient(app)
    csrf = login(client, "dup@example.com")
    session_value = client.cookies.get(SESSION_COOKIE)
    payload = {
        "platform": "custom",
        "name": "竞态",
        "fields": {"url": "json://httpbin.org/post?race=1"},
    }
    barrier = Barrier(2)

    def create(_: int) -> int:
        thread_client = TestClient(app)
        barrier.wait(timeout=10)
        return thread_client.post(
            "/api/v1/me/notification-channels",
            json=payload,
            cookies={SESSION_COOKIE: session_value, CSRF_COOKIE: csrf},
            headers=auth_headers(csrf),
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(create, range(2)))
    assert statuses == [201, 422], statuses


def test_task_update_after_concurrent_delete_is_404(client, monkeypatch) -> None:
    """并发删除后的 PATCH 必须给 404，而不是 StaleDataError 500。"""
    _seed_two_tenants()
    csrf = login(client, "a@example.com")
    created = client.post(
        "/api/v1/tasks",
        json={"name": "竞态更新", "store_codes": ["S001"], "style_codes": ["DN3707-202"]},
        headers=auth_headers(csrf),
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]

    from app.api import tasks as api_mod

    real_load = api_mod._load

    def racy_load(db, user, task_uuid):
        task = real_load(db, user, task_uuid)
        with SessionLocal() as other:
            other.execute(delete(MonitorTask).where(MonitorTask.id == task_uuid))
            other.commit()
        return task

    monkeypatch.setattr(api_mod, "_load", racy_load)
    response = client.patch(
        f"/api/v1/tasks/{task_id}", json={"name": "改不到了"}, headers=auth_headers(csrf)
    )
    assert response.status_code == 404, response.text


def test_task_delete_after_concurrent_delete_is_idempotent(client, monkeypatch) -> None:
    """两个 DELETE 撞车：输家幂等返回 204，而不是 500。"""
    _seed_two_tenants()
    csrf = login(client, "a@example.com")
    created = client.post(
        "/api/v1/tasks",
        json={"name": "竞态删除", "store_codes": ["S001"], "style_codes": ["DN3707-202"]},
        headers=auth_headers(csrf),
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]

    from app.api import tasks as api_mod

    real_load = api_mod._load

    def racy_load(db, user, task_uuid):
        task = real_load(db, user, task_uuid)
        with SessionLocal() as other:
            other.execute(delete(MonitorTask).where(MonitorTask.id == task_uuid))
            other.commit()
        return task

    monkeypatch.setattr(api_mod, "_load", racy_load)
    response = client.delete(f"/api/v1/tasks/{task_id}", headers=auth_headers(csrf))
    # 行已被并发删掉时本次 DELETE 只是少删一行（ORM 仅告警）：幂等成功。
    assert response.status_code == 204, response.text


def test_channel_update_after_concurrent_delete_is_404(client, monkeypatch) -> None:
    with SessionLocal() as db:
        tenant = make_tenant(db)
        make_user(db, tenant, email="chan-race@example.com")
        db.commit()
    csrf = login(client, "chan-race@example.com")
    created = client.post(
        "/api/v1/me/notification-channels",
        json={
            "platform": "custom",
            "name": "竞态渠道",
            "fields": {"url": "json://httpbin.org/post?chan-race=1"},
        },
        headers=auth_headers(csrf),
    )
    assert created.status_code == 201, created.text
    channel_id = created.json()["id"]

    from app.api import notifications as api_mod

    real_load = api_mod._load_channel

    def racy_load(db, user, channel_uuid):
        channel = real_load(db, user, channel_uuid)
        with SessionLocal() as other:
            other.execute(delete(NotificationChannel).where(NotificationChannel.id == channel_uuid))
            other.commit()
        return channel

    monkeypatch.setattr(api_mod, "_load_channel", racy_load)
    response = client.patch(
        f"/api/v1/me/notification-channels/{channel_id}",
        json={"name": "改不到了"},
        headers=auth_headers(csrf),
    )
    assert response.status_code == 404, response.text


def test_channel_delete_after_concurrent_delete_is_idempotent(client, monkeypatch) -> None:
    with SessionLocal() as db:
        tenant = make_tenant(db)
        make_user(db, tenant, email="chan-race2@example.com")
        db.commit()
    csrf = login(client, "chan-race2@example.com")
    created = client.post(
        "/api/v1/me/notification-channels",
        json={
            "platform": "custom",
            "name": "竞态渠道二",
            "fields": {"url": "json://httpbin.org/post?chan-race=2"},
        },
        headers=auth_headers(csrf),
    )
    assert created.status_code == 201, created.text
    channel_id = created.json()["id"]

    from app.api import notifications as api_mod

    real_load = api_mod._load_channel

    def racy_load(db, user, channel_uuid):
        channel = real_load(db, user, channel_uuid)
        with SessionLocal() as other:
            other.execute(delete(NotificationChannel).where(NotificationChannel.id == channel_uuid))
            other.commit()
        return channel

    monkeypatch.setattr(api_mod, "_load_channel", racy_load)
    response = client.delete(
        f"/api/v1/me/notification-channels/{channel_id}", headers=auth_headers(csrf)
    )
    # 行已被并发删掉时本次 DELETE 只是少删一行（ORM 仅告警）：幂等成功。
    assert response.status_code == 204, response.text


def test_store_list_defaults_to_clearance_and_can_show_all(client) -> None:
    """门店目录默认只给换季优惠店；?scope=all 才返回全部门店。"""
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant, email=f"clearance-list-{uuid.uuid4().hex[:8]}@example.com")
        make_store(db, owner=user, code="S1", name="NIKE台州兴华换季优惠店")
        make_store(db, owner=user, code="S2", name="NIKE上海001")
        db.commit()
        email = user.email
    login_session(client, email)

    default_codes = [row["store_code"] for row in client.get("/api/v1/stores").json()]
    assert default_codes == ["S1"]
    all_codes = [row["store_code"] for row in client.get("/api/v1/stores?scope=all").json()]
    assert all_codes == ["S1", "S2"]
