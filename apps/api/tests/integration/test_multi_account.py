"""一用户多账号（PostgreSQL）：选号、轮换、派发、绑定与断开。

不变式：owner 隔离不变；一个作业只冻结一个账号；全部账号都不可用（冷却/断开）
才暂停派发，且不等账号自动恢复；同一 Nike 手机号仍只能绑一个平台用户。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Credential, CredentialStatus, JobStatus, MonitorTask, ScanJob
from app.services import nike_auth
from app.services.credentials import (
    CredentialChanged,
    CredentialService,
    pick_ready,
)
from app.services.jobs import enqueue_job
from app.services.scheduler import dispatch_due_tasks

from .helpers import (
    add_credential,
    csrf_headers,
    login_session,
    make_store,
    make_style,
    make_tenant,
    make_user,
    session_cookies,
    store_credential,
)
from .test_personal_nike_credentials import FakePhoneAuth

# 第二个手机号：同一用户绑两个 Nike 账号。
PHONE2 = "+8613800138001"


def _seed_user(*, stores: tuple[str, ...] = ("S1", "S2")) -> dict:
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant, email=f"multi-{uuid.uuid4().hex[:8]}@example.com")
        for code in stores:
            make_store(db, owner=user, code=code)
        make_style(db, owner=user, code="DN3707-202")
        db.commit()
        return {"tenant_id": tenant.id, "user_id": user.id}


def _make_task(db, tenant_id, *, created_by, name="多账号任务") -> MonitorTask:
    task = MonitorTask(
        tenant_id=tenant_id,
        name=name,
        store_codes=["S1", "S2"],
        style_codes=["DN3707-202"],
        all_stores=False,
        cadence_seconds=1800,
        is_active=True,
        next_run_at=datetime.now(UTC),
        created_by=created_by,
    )
    db.add(task)
    db.flush()
    return task


def _jobs_for(task_id) -> list[ScanJob]:
    with SessionLocal() as db:
        return list(
            db.execute(select(ScanJob).where(ScanJob.task_id == task_id)).scalars()
        )


def test_pick_ready_skips_cooling_accounts_and_rotates_lru() -> None:
    settings = get_settings()
    seed = _seed_user()
    first = store_credential(settings, seed["user_id"])
    second_id = add_credential(settings, seed["user_id"])

    now = datetime.now(UTC)
    with SessionLocal() as db:
        first_row = db.get(Credential, first.credential_id)
        first_row.last_used_at = now
        db.commit()
        # 刚用过的账号排到最后：选另一个。
        picked = pick_ready(db, seed["user_id"])
        assert picked is not None and picked.id == second_id

        # 冷却中的账号被跳过（仍选可用的那个）。
        first_row.cooldown_until = now + timedelta(minutes=30)
        first_row.cooldown_reason = "throttled"
        db.commit()
        picked = pick_ready(db, seed["user_id"])
        assert picked is not None and picked.id == second_id

        # 全部冷却 → 没有可用账号。
        db.get(Credential, second_id).cooldown_until = now + timedelta(minutes=10)
        db.commit()
        assert pick_ready(db, seed["user_id"]) is None


def test_dispatch_rotates_accounts_and_marks_the_used_one() -> None:
    settings = get_settings()
    seed = _seed_user()
    first = store_credential(settings, seed["user_id"])
    second_id = add_credential(settings, seed["user_id"])
    with SessionLocal() as db:
        task = _make_task(db, seed["tenant_id"], created_by=seed["user_id"])
        db.commit()
        task_id = task.id

    now = datetime.now(UTC)
    with SessionLocal() as db:
        db.get(Credential, first.credential_id).last_used_at = now
        db.commit()
        assert dispatch_due_tasks(db, settings) == 1
        db.commit()

    jobs = _jobs_for(task_id)
    assert len(jobs) == 1
    assert jobs[0].credential_id == second_id
    # mark_used 推进 LRU：下一次轮换到另一个账号。
    with SessionLocal() as db:
        assert db.get(Credential, second_id).last_used_at is not None
        # 上一轮作业必须真的结束（_jobs_for 返回的是游离对象，改它不会落库）。
        db.get(ScanJob, jobs[0].id).status = JobStatus.SUCCEEDED.value
        db.get(MonitorTask, task_id).next_run_at = datetime.now(UTC)
        db.commit()
    with SessionLocal() as db:
        assert dispatch_due_tasks(db, settings) == 1
        db.commit()
    jobs = sorted(_jobs_for(task_id), key=lambda job: job.created_at)
    assert len(jobs) == 2
    assert jobs[0].credential_id == second_id
    assert jobs[1].credential_id == first.credential_id


def test_queued_job_frozen_to_another_account_is_superseded_not_merged() -> None:
    """已排队作业冻结在账号 A 时，合并作业整体绑定本轮选中的账号 B。

    一个作业只冻结一个绑定：旧作业被取消（并入）而不是与新作业混用账号。
    """
    settings = get_settings()
    seed = _seed_user()
    first = store_credential(settings, seed["user_id"])
    second_id = add_credential(settings, seed["user_id"])
    with SessionLocal() as db:
        task1 = _make_task(db, seed["tenant_id"], created_by=seed["user_id"], name="任务一")
        task2 = _make_task(db, seed["tenant_id"], created_by=seed["user_id"], name="任务二")
        # task1 已有一个排队中的单任务作业，冻结在账号 A 上（A 随后冷却）。
        stale = enqueue_job(
            db,
            tenant_id=seed["tenant_id"],
            store_codes=["S1", "S2"],
            style_codes=["DN3707-202"],
            task_id=task1.id,
            kind="MONITOR",
            owner_user_id=seed["user_id"],
            credential_binding=first,
        )
        db.flush()
        stale_id = stale.id
        db.get(Credential, first.credential_id).cooldown_until = datetime.now(UTC) + timedelta(
            minutes=30
        )
        db.commit()
        task1_id, task2_id = task1.id, task2.id

    with SessionLocal() as db:
        assert dispatch_due_tasks(db, settings) == 2
        db.commit()

    with SessionLocal() as db:
        merged = db.execute(
            select(ScanJob).where(ScanJob.task_ids.contains([str(task1_id)]))
        ).scalars().all()
        assert len(merged) == 1
        job = merged[0]
        assert job.credential_id == second_id
        assert set(job.task_ids) == {str(task1_id), str(task2_id)}
        # 冻结在 A 的旧作业被取消，绝不与新作业混用账号。
        assert db.get(ScanJob, stale_id).status == JobStatus.CANCELLED.value
        assert db.get(ScanJob, stale_id).credential_id == first.credential_id
        assert db.execute(select(func.count()).select_from(ScanJob)).scalar_one() == 2


def test_dispatch_pauses_until_the_earliest_cooldown_when_all_accounts_are_used_up() -> None:
    settings = get_settings()
    seed = _seed_user()
    first = store_credential(settings, seed["user_id"])
    second_id = add_credential(settings, seed["user_id"])
    with SessionLocal() as db:
        task = _make_task(db, seed["tenant_id"], created_by=seed["user_id"])
        db.commit()
        task_id = task.id

    now = datetime.now(UTC)
    earliest = now + timedelta(minutes=10)
    latest = now + timedelta(minutes=30)
    with SessionLocal() as db:
        db.get(Credential, first.credential_id).cooldown_until = latest
        db.get(Credential, second_id).cooldown_until = earliest
        db.commit()
        assert dispatch_due_tasks(db, settings) == 0
        db.commit()

    with SessionLocal() as db:
        assert db.execute(select(ScanJob)).scalars().all() == []
        task = db.get(MonitorTask, task_id)
        assert task.is_active is True
        assert task.next_run_at is not None
        assert abs((task.next_run_at - earliest).total_seconds()) <= 2


def test_http_binding_adds_a_second_account_and_leaves_the_first_alone(
    fake_auth, client, account
) -> None:
    """走完整 HTTP 绑定流程再加一个手机号 → 两行账号，第一行不受影响。"""
    settings = get_settings()
    first = store_credential(settings, account["user_id"], access_token="first-access")

    second = _bind(client, account, PHONE2)
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["id"] is not None and body["id"] != str(first.credential_id)
    assert body["phone_masked"].endswith("8001")

    listing = client.get(
        "/api/v1/me/nike-credentials",
        cookies=session_cookies(account["session"]),
    )
    assert listing.status_code == 200, listing.text
    rows = listing.json()
    assert len(rows) == 2
    assert {row["id"] for row in rows} == {str(first.credential_id), body["id"]}

    # 第一行 generation 不变，token 未被覆盖。
    assert (
        CredentialService(settings, account["user_id"], credential_id=first.credential_id)
        .access_token(binding=first)
        == "first-access"
    )
    assert CredentialService(
        settings, account["user_id"], credential_id=first.credential_id
    ).status().binding_generation == first.generation

    # 重新验证指定账号：仍只有两行，且只有那一行的 generation 前进。
    again = _bind(client, account, PHONE2, credential_id=body["id"])
    assert again.status_code == 200, again.text
    assert again.json()["id"] == body["id"]
    assert again.json()["binding_generation"] == 2
    with SessionLocal() as db:
        assert db.execute(select(func.count()).select_from(Credential)).scalar_one() == 2
    assert (
        CredentialService(settings, account["user_id"], credential_id=first.credential_id)
        .status()
        .binding_generation
        == first.generation
    )

    # 逐个断开：只吊销指定行。
    gone = client.delete(
        f"/api/v1/me/nike-credentials/{body['id']}",
        headers=csrf_headers(account["csrf"]),
        cookies=session_cookies(account["session"]),
    )
    assert gone.status_code == 204
    with SessionLocal() as db:
        assert db.get(Credential, uuid.UUID(body["id"])).status == CredentialStatus.REVOKED.value
        assert db.get(Credential, first.credential_id).status == CredentialStatus.ACTIVE.value


def test_assert_binding_rejects_a_revoked_account() -> None:
    settings = get_settings()
    seed = _seed_user()
    first = store_credential(settings, seed["user_id"])
    second_id = add_credential(settings, seed["user_id"])

    service = CredentialService(settings, seed["user_id"])
    service.assert_binding(first)  # 可用

    with SessionLocal() as db:
        db.get(Credential, first.credential_id).status = CredentialStatus.REVOKED.value
        db.commit()

    with pytest.raises(CredentialChanged):
        service.assert_binding(first)
    # 另一个账号不受影响，自动选号也仍然可用。
    assert service.binding().credential_id == second_id


def test_disconnect_unknown_account_is_a_404(client, account) -> None:
    response = client.delete(
        f"/api/v1/me/nike-credentials/{uuid.uuid4()}",
        headers=csrf_headers(account["csrf"]),
        cookies=session_cookies(account["session"]),
    )
    assert response.status_code == 404
    assert response.json()["error_code"] == "NIKE_CREDENTIAL_NOT_FOUND"


# ------------------------------------------------------------------ fixtures


@pytest.fixture
def fake_auth(monkeypatch):
    FakePhoneAuth.reset()
    monkeypatch.setattr(nike_auth, "NikePhoneAuthClient", FakePhoneAuth)
    yield FakePhoneAuth
    FakePhoneAuth.reset()


@pytest.fixture
def account(client):
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant, email=f"multi-http-{uuid.uuid4().hex[:8]}@example.com")
        db.commit()
        info = {"user_id": user.id, "email": user.email, "tenant_id": tenant.id}
    info.update(login_session(client, info["email"]))
    return info


def _bind(client, account, phone, *, credential_id=None):  # noqa: ANN001
    """走两次 HTTP：发起短信 + 验证码校验（上游用脚本化 Fake）。"""
    payload = {"phone": phone, "consent": True, "request_id": str(uuid.uuid4())}
    if credential_id is not None:
        payload["credential_id"] = credential_id
    started = client.post(
        "/api/v1/me/nike-auth/sessions",
        json=payload,
        headers=csrf_headers(account["csrf"]),
        cookies=session_cookies(account["session"]),
    )
    assert started.status_code == 201, started.text
    return client.post(
        f"/api/v1/me/nike-auth/sessions/{started.json()['id']}/verify",
        json={"code": "123456"},
        headers=csrf_headers(account["csrf"]),
        cookies=session_cookies(account["session"]),
    )



def test_matrix_reads_cover_every_live_account(client, account) -> None:
    """一用户多账号：任一仍在账号写入的最新态都可见；断开的账号不再展示。"""
    from app.models import ObservationLatest, User
    from app.services.credentials import apply_tokens_locked

    now = datetime.now(UTC)
    with SessionLocal() as db:
        user = db.get(User, account["user_id"])
        store = make_store(db, owner=user, code="S1")
        style = make_style(db, owner=user, code="DN3707-202")
        first = Credential(owner_user_id=user.id, binding_generation=1, status="ACTIVE")
        db.add(first)
        db.flush()
        apply_tokens_locked(
            first, access_token="a-access", refresh_token="a-refresh", client_id="cid"
        )
        second = Credential(owner_user_id=user.id, binding_generation=1, status="ACTIVE")
        db.add(second)
        db.flush()
        apply_tokens_locked(
            second, access_token="b-access", refresh_token="b-refresh", client_id="cid"
        )
        for credential, label in ((first, "42"), (second, "43")):
            db.add(
                ObservationLatest(
                    owner_user_id=user.id,
                    credential_id=credential.id,
                    credential_generation=credential.binding_generation,
                    store_id=store.id,
                    style_id=style.id,
                    size_label=label,
                    gtin=f"G-{label}",
                    availability="IN_STOCK",
                    trust="TRUSTED",
                    checked_at=now,
                )
            )
        db.commit()
        first_id = first.id

    cookies = session_cookies(account["session"])
    response = client.get("/api/v1/matrix", params={"style_code": "DN3707-202"}, cookies=cookies)
    assert response.status_code == 200, response.text
    assert {cell["size_label"] for cell in response.json()["cells"]} == {"42", "43"}

    # 断开账号 A 后，它写入的那一行不再展示；B 的仍在。
    assert (
        client.delete(
            f"/api/v1/me/nike-credentials/{first_id}",
            headers=csrf_headers(account["csrf"]),
            cookies=cookies,
        ).status_code
        == 204
    )
    response = client.get("/api/v1/matrix", params={"style_code": "DN3707-202"}, cookies=cookies)
    assert response.status_code == 200, response.text
    assert {cell["size_label"] for cell in response.json()["cells"]} == {"43"}


def test_mark_used_does_not_touch_the_refresh_backoff_stamp() -> None:
    """派发只写 LRU 列：续期退避的时间戳不能被高频派发顺手刷新。"""
    settings = get_settings()
    seed = _seed_user()
    first = store_credential(settings, seed["user_id"])
    stamp = datetime.now(UTC) - timedelta(seconds=30)
    with SessionLocal() as db:
        record = db.get(Credential, first.credential_id)
        record.last_attempt_at = stamp
        record.last_error_code = "NIKE_UPSTREAM_UNAVAILABLE"
        db.commit()

    CredentialService(settings, seed["user_id"]).mark_used(first.credential_id)

    with SessionLocal() as db:
        record = db.get(Credential, first.credential_id)
        assert record.last_attempt_at == stamp
        assert record.last_used_at is not None
