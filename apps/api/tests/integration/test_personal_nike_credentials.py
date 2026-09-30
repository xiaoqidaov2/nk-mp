"""Self-service Nike phone binding: ``/me`` credential lifecycle (PostgreSQL).

These tests exercise the personal phone-auth flow end to end against a real
database with a scripted upstream client (no HTTP to Nike ever happens):

* attempt replay / session binding and the one-shot verify budget;
* operation timeouts, uncertainty and the "never replay" rule;
* failed re-binds keep the old credential, successful ones bump the generation;
* a late upstream result is never installed after cancel / logout / disconnect;
* the feature flag turns the whole surface into a clean 503 without any call.
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.config import get_settings
from app.db import SessionLocal
from app.models import Credential, NikeAuthAttempt, NikeAuthLimit
from app.nike.phone_auth import (
    MP_REDIRECT_URI,
    MP_SCOPE,
    SUPPORTED_CONTRACT_VERSION,
    PhoneAuthChallenge,
    PhoneAuthCodeInvalid,
    PhoneAuthTokens,
    mask_phone,
)
from app.services import nike_auth
from app.services.credentials import CredentialChanged, CredentialService

from .helpers import (
    csrf_headers,
    login_session,
    make_tenant,
    make_user,
    session_cookies,
    store_credential,
)

# The phone-auth contract is versioned and off by default; the tests pin the
# supported version and drop the resend cooldown so re-binding is deterministic.
os.environ.setdefault("NIKE_MP_NIKE_PHONE_AUTH_ENABLED", "true")
os.environ["NIKE_MP_NIKE_PHONE_AUTH_CONTRACT_VERSION"] = SUPPORTED_CONTRACT_VERSION
os.environ["NIKE_MP_NIKE_SMS_COOLDOWN_SECONDS"] = "0"
os.environ["NIKE_MP_NIKE_SMS_VERIFY_LIMIT"] = "5"

PHONE = "+8613800138000"
SEND_UNCERTAIN = "NIKE_SMS_SEND_UNCERTAIN"
VERIFY_UNCERTAIN = "NIKE_VERIFY_UNCERTAIN"


class FakePhoneAuth:
    """Scripted ``NikePhoneAuthClient``: counts calls and can block/raise."""

    start_calls = 0
    verify_calls = 0
    start_error: Exception | None = None
    verify_error: Exception | None = None
    tokens: PhoneAuthTokens | None = None
    verify_entered: threading.Event | None = None
    verify_release: threading.Event | None = None

    def __init__(self, settings, *, transport=None):  # noqa: ANN001
        self.settings = settings

    # ------------------------------------------------------------- lifecycle
    def __enter__(self) -> FakePhoneAuth:
        return self

    def __exit__(self, *exc_info) -> bool:
        return False

    def close(self) -> None:
        return None

    @classmethod
    def reset(cls) -> None:
        cls.start_calls = 0
        cls.verify_calls = 0
        cls.start_error = None
        cls.verify_error = None
        cls.tokens = None
        cls.verify_entered = None
        cls.verify_release = None

    # ------------------------------------------------------------- upstream
    def start(self, phone_e164: str) -> PhoneAuthChallenge:
        type(self).start_calls += 1
        if type(self).start_error is not None:
            raise type(self).start_error
        return PhoneAuthChallenge(
            phone_e164=phone_e164,
            phone_masked=mask_phone(phone_e164),
            client_id=self.settings.oauth_client_id,
            redirect_uri=MP_REDIRECT_URI,
            scope=MP_SCOPE,
            state="s" * 16,
            code_verifier="v" * 43,
            code_challenge="c" * 43,
            cookies={"sid": "fake"},
            challenge_kind="challenge",
        )

    def verify(self, context: PhoneAuthChallenge, code: str) -> PhoneAuthTokens:
        type(self).verify_calls += 1
        if type(self).verify_entered is not None:
            type(self).verify_entered.set()
        if type(self).verify_release is not None:
            type(self).verify_release.wait(timeout=10)
        if type(self).verify_error is not None:
            raise type(self).verify_error
        return type(self).tokens or PhoneAuthTokens(
            access_token="access-token",
            refresh_token="refresh-token",
            client_id=context.client_id,
            access_expires_at=None,
        )


@pytest.fixture(autouse=True)
def _reset_fake_auth():
    FakePhoneAuth.reset()
    yield
    FakePhoneAuth.reset()


@pytest.fixture
def fake_auth(monkeypatch):
    monkeypatch.setattr(nike_auth, "NikePhoneAuthClient", FakePhoneAuth)
    return FakePhoneAuth


@pytest.fixture
def account(client):
    with SessionLocal() as db:
        tenant = make_tenant(db)
        user = make_user(db, tenant, email=f"nike-{uuid.uuid4().hex[:8]}@example.com")
        db.commit()
        info = {"user_id": user.id, "email": user.email, "tenant_id": tenant.id}
    info.update(login_session(client, info["email"]))
    return info


# ------------------------------------------------------------------ helpers


def _start(client, account, request_id, *, session=None, csrf=None, phone=PHONE):  # noqa: ANN001
    return client.post(
        "/api/v1/me/nike-auth/sessions",
        json={"phone": phone, "consent": True, "request_id": str(request_id)},
        headers=csrf_headers(csrf or account["csrf"]),
        cookies=session_cookies(session or account["session"]),
    )


def _verify(client, account, attempt_id, code="123456", *, session=None, csrf=None):  # noqa: ANN001
    return client.post(
        f"/api/v1/me/nike-auth/sessions/{attempt_id}/verify",
        json={"code": code},
        headers=csrf_headers(csrf or account["csrf"]),
        cookies=session_cookies(session or account["session"]),
    )


def _cancel(client, account, attempt_id, *, session=None, csrf=None):  # noqa: ANN001
    return client.delete(
        f"/api/v1/me/nike-auth/sessions/{attempt_id}",
        headers=csrf_headers(csrf or account["csrf"]),
        cookies=session_cookies(session or account["session"]),
    )


def _disconnect(client, account):  # noqa: ANN001
    return client.delete(
        "/api/v1/me/nike-credential", headers=csrf_headers(account["csrf"])
    )


def _attempt_row(attempt_id):
    with SessionLocal() as db:
        return db.get(NikeAuthAttempt, attempt_id)


# --------------------------------------------------------------------- tests


def test_replayed_request_returns_same_attempt_without_resending(fake_auth, client, account) -> None:
    request_id = uuid.uuid4()
    first = _start(client, account, request_id)
    assert first.status_code == 201, first.text
    attempt_id = first.json()["id"]
    assert first.json()["state"] == "CODE_SENT"

    replay = _start(client, account, request_id)
    assert replay.status_code == 200, "a replay must not create a second attempt"
    assert replay.json()["id"] == attempt_id

    assert fake_auth.start_calls == 1
    with SessionLocal() as db:
        assert len(db.execute(select(NikeAuthAttempt)).scalars().all()) == 1


def test_replay_from_another_auth_session_is_rejected(fake_auth, client, account) -> None:
    second = login_session(client, account["email"])
    request_id = uuid.uuid4()

    first = _start(client, account, request_id)
    assert first.status_code == 201, first.text

    replay = _start(client, account, request_id, session=second["session"], csrf=second["csrf"])
    assert replay.status_code == 409
    assert replay.json()["error_code"] == "NIKE_AUTH_SESSION_MISMATCH"
    assert fake_auth.start_calls == 1


def test_concurrent_verify_exchanges_only_once(fake_auth, client, account) -> None:
    started, release = threading.Event(), threading.Event()
    fake_auth.verify_entered = started
    fake_auth.verify_release = release
    fake_auth.tokens = PhoneAuthTokens("verified-access", "verified-refresh", "cid", None)

    attempt_id = _start(client, account, uuid.uuid4()).json()["id"]

    outcome: dict[str, object] = {}

    def worker() -> None:
        with TestClient(_app()) as thread_client:
            outcome["response"] = thread_client.post(
                f"/api/v1/me/nike-auth/sessions/{attempt_id}/verify",
                json={"code": "123456"},
                headers=csrf_headers(account["csrf"]),
                cookies=session_cookies(account["session"]),
            )

    thread = threading.Thread(target=worker)
    thread.start()
    assert started.wait(5), "the first verify never reached the upstream"
    # The first request holds the attempt in VERIFYING; a concurrent second
    # verify must not race a second code exchange.
    second = _verify(client, account, attempt_id)
    assert second.status_code == 409
    assert second.json()["error_code"] == "NIKE_AUTH_IN_PROGRESS"

    release.set()
    thread.join(10)
    assert outcome["response"].status_code == 200, outcome
    assert fake_auth.verify_calls == 1


def test_verify_lock_after_repeated_failures_survives_new_login(fake_auth, client, account) -> None:
    fake_auth.verify_error = PhoneAuthCodeInvalid("bad code")

    request_id = uuid.uuid4()
    attempt_id = _start(client, account, request_id).json()["id"]
    for _ in range(4):
        response = _verify(client, account, attempt_id, "000000")
        assert response.status_code == 400
        assert response.json()["error_code"] == "NIKE_CODE_INVALID"

    assert _cancel(client, account, attempt_id).status_code == 204
    resend_id = _start(client, account, uuid.uuid4()).json()["id"]
    fifth = _verify(client, account, resend_id, "000000")
    assert fifth.status_code == 429
    assert fifth.json()["error_code"] == "NIKE_AUTH_RATE_LIMITED"

    with SessionLocal() as db:
        limit = db.execute(
            select(NikeAuthLimit).where(NikeAuthLimit.owner_user_id == account["user_id"])
        ).scalar_one()
        assert limit.verify_locked_until is not None

    # Logging in again opens a new platform session but must not unlock.
    second = login_session(client, account["email"])
    fresh_id = _start(
        client, account, uuid.uuid4(), session=second["session"], csrf=second["csrf"]
    ).json()["id"]
    locked = _verify(client, account, fresh_id, "000000", session=second["session"], csrf=second["csrf"])
    assert locked.status_code == 429
    assert locked.json()["error_code"] == "NIKE_AUTH_RATE_LIMITED"
    # Only the first five attempts ever reached the upstream verifier.
    assert fake_auth.verify_calls == 5


def test_expired_operations_are_finalised_uncertain(fake_auth, client, account) -> None:
    past = datetime.now(UTC) - timedelta(seconds=1)

    send_id = _start(client, account, uuid.uuid4()).json()["id"]
    with SessionLocal() as db:
        db.execute(
            update(NikeAuthAttempt)
            .where(NikeAuthAttempt.id == send_id)
            .values(state="SENDING", operation_deadline_at=past)
        )
        db.commit()
    assert nike_auth.expire_attempts() >= 1
    row = _attempt_row(send_id)
    assert row.state == "FAILED"
    assert row.last_error_code == SEND_UNCERTAIN

    verify_id = _start(client, account, uuid.uuid4()).json()["id"]
    with SessionLocal() as db:
        db.execute(
            update(NikeAuthAttempt)
            .where(NikeAuthAttempt.id == verify_id)
            .values(state="VERIFYING", operation_deadline_at=past)
        )
        db.commit()
    assert nike_auth.expire_attempts() >= 1
    row = _attempt_row(verify_id)
    assert row.state == "FAILED"
    assert row.last_error_code == VERIFY_UNCERTAIN

    # Once finalised, the owner may open a fresh attempt.
    assert _start(client, account, uuid.uuid4()).status_code == 201


def test_upstream_send_timeout_is_not_replayed(fake_auth, client, account) -> None:
    fake_auth.start_error = httpx.TransportError("boom")
    request_id = uuid.uuid4()

    response = _start(client, account, request_id)
    assert response.status_code == 503
    assert response.json()["error_code"] == SEND_UNCERTAIN
    assert fake_auth.start_calls == 1

    # Replaying the same request id returns the stored terminal attempt and
    # must never issue a second SMS.
    replay = _start(client, account, request_id)
    assert replay.status_code == 200
    assert replay.json()["state"] == "FAILED"
    assert fake_auth.start_calls == 1


def test_upstream_verify_timeout_is_not_replayed(fake_auth, client, account) -> None:
    attempt_id = _start(client, account, uuid.uuid4()).json()["id"]
    fake_auth.verify_error = RuntimeError("boom")

    response = _verify(client, account, attempt_id)
    assert response.status_code == 503
    assert response.json()["error_code"] == VERIFY_UNCERTAIN
    assert fake_auth.verify_calls == 1

    # A second attempt cannot consume the single-use code again.
    again = _verify(client, account, attempt_id)
    assert again.status_code == 410
    assert fake_auth.verify_calls == 1


def test_failed_rebind_keeps_old_binding_and_success_bumps_generation(fake_auth, client, account) -> None:
    settings = get_settings()
    user_id = account["user_id"]
    binding1 = store_credential(settings, user_id, access_token="old-access", refresh_token="old-refresh")
    assert binding1.generation == 1

    fake_auth.verify_error = PhoneAuthCodeInvalid("bad")
    failed_id = _start(client, account, uuid.uuid4()).json()["id"]
    assert _verify(client, account, failed_id, "000000").status_code == 400

    # A failed replacement never touches the existing credential.
    assert CredentialService(settings, user_id).binding() == binding1
    assert CredentialService(settings, user_id).access_token(binding=binding1) == "old-access"

    assert _cancel(client, account, failed_id).status_code == 204
    fake_auth.verify_error = None
    fake_auth.tokens = PhoneAuthTokens("new-access", "new-refresh", "cid", None)
    success_id = _start(client, account, uuid.uuid4()).json()["id"]
    assert _verify(client, account, success_id).status_code == 200

    binding2 = CredentialService(settings, user_id).binding()
    assert binding2.credential_id == binding1.credential_id
    assert binding2.generation == binding1.generation + 1
    assert CredentialService(settings, user_id).access_token(binding=binding2) == "new-access"
    # The old frozen binding can no longer be used.
    with pytest.raises(CredentialChanged):
        CredentialService(settings, user_id).assert_binding(binding1)


def test_disconnect_without_credential_cancels_pending_attempt(fake_auth, client, account) -> None:
    settings = get_settings()
    attempt_id = _start(client, account, uuid.uuid4()).json()["id"]
    assert CredentialService(settings, account["user_id"]).status() is None

    assert _disconnect(client, account).status_code == 204

    assert _attempt_row(attempt_id).state == "CANCELLED"
    with SessionLocal() as db:
        assert db.execute(select(Credential)).scalars().all() == []


@pytest.mark.parametrize("action", ["cancel", "disconnect", "logout"])
def test_late_verify_result_is_not_installed(fake_auth, client, account, action) -> None:
    settings = get_settings()
    started, release = threading.Event(), threading.Event()
    fake_auth.verify_entered = started
    fake_auth.verify_release = release
    fake_auth.tokens = PhoneAuthTokens("late-access", "late-refresh", "cid", None)

    attempt_id = _start(client, account, uuid.uuid4()).json()["id"]
    outcome: dict[str, object] = {}

    def worker() -> None:
        with TestClient(_app()) as thread_client:
            outcome["response"] = thread_client.post(
                f"/api/v1/me/nike-auth/sessions/{attempt_id}/verify",
                json={"code": "123456"},
                headers=csrf_headers(account["csrf"]),
                cookies=session_cookies(account["session"]),
            )

    thread = threading.Thread(target=worker)
    thread.start()
    assert started.wait(5), "the verify never reached the upstream"

    if action == "cancel":
        assert _cancel(client, account, attempt_id).status_code == 204
    elif action == "disconnect":
        assert _disconnect(client, account).status_code == 204
    else:
        assert client.post("/api/v1/auth/logout").status_code == 200

    release.set()
    thread.join(10)
    assert outcome["response"].status_code == 409, outcome
    assert outcome["response"].json()["error_code"] == "NIKE_CREDENTIAL_CHANGED"
    # The late upstream token was never installed.
    assert CredentialService(settings, account["user_id"]).status() is None


def test_feature_flag_off_blocks_send_and_verify(fake_auth, client, account, monkeypatch) -> None:
    settings = get_settings()
    store_credential(settings, account["user_id"], access_token="acc", refresh_token="ref")

    monkeypatch.setenv("NIKE_MP_NIKE_PHONE_AUTH_ENABLED", "false")
    get_settings.cache_clear()

    # Reading the credential still returns the real state, just not available.
    status = client.get("/api/v1/me/nike-credential")
    assert status.status_code == 200, status.text
    assert status.json()["login_available"] is False
    assert status.json()["status"] == "ACTIVE"

    start = _start(client, account, uuid.uuid4())
    assert start.status_code == 503
    assert start.json()["error_code"] == "NIKE_AUTH_NOT_AVAILABLE"
    assert fake_auth.start_calls == 0

    verify = _verify(client, account, uuid.uuid4())
    assert verify.status_code == 503
    assert verify.json()["error_code"] == "NIKE_AUTH_NOT_AVAILABLE"
    assert fake_auth.verify_calls == 0

    # Disconnect is a local action and still works while login is off.
    assert _disconnect(client, account).status_code == 204


def _app():
    """The FastAPI app singleton (imported lazily to keep import order simple)."""
    from app.main import app

    return app
