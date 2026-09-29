"""T04: credential lifecycle.

Covers forced refresh after a 401, single-flight serialisation, immediate
persistence of rotated credentials and bounded retry behaviour.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.models import Credential, CredentialStatus
from app.nike.client import NikeClient
from app.nike.errors import ErrorCode, ScanError
from app.security import encrypt_secret
from app.services import credentials as credentials_module
from app.services.credentials import CredentialService

from .conftest import make_jwt


class _Result:
    def __init__(self, value):  # noqa: ANN001
        self._value = value

    def scalar_one_or_none(self):  # noqa: ANN201
        return self._value


class _FakeSession:
    """Duck-typed session; records commits so persistence can be asserted."""

    def __init__(self, record: Credential | None) -> None:
        self.record = record
        self.commits = 0
        self.flushes = 0

    def execute(self, _stmt):  # noqa: ANN001
        return _Result(self.record)

    def commit(self) -> None:
        self.commits += 1

    def flush(self) -> None:
        self.flushes += 1


def _record(*, access: str = "old-token", refresh: str = "refresh-token", expires_in: int = 3600) -> Credential:
    return Credential(
        account_key="default",
        client_id="client",
        access_token_enc=encrypt_secret(access),
        refresh_token_enc=encrypt_secret(refresh),
        status=CredentialStatus.ACTIVE.value,
        access_expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        last_error="",
    )


def _fake_response(payload: dict, status: int = 200) -> httpx.Response:
    response = httpx.Response(status, json=payload)
    response.request = httpx.Request("POST", "https://accounts.nike.com.cn/token/v1")
    return response


def _post_ok(monkeypatch, *, access: str, refresh: str | None = None) -> list[dict]:
    calls: list[dict] = []

    def fake_post(url, data=None, headers=None, timeout=None):  # noqa: ANN001
        calls.append({"url": url, "data": data})
        payload = {"access_token": access}
        if refresh:
            payload["refresh_token"] = refresh
        return _fake_response(payload)

    monkeypatch.setattr(credentials_module.httpx, "post", fake_post)
    return calls


def test_unexpired_token_is_used_without_refresh(settings, monkeypatch) -> None:
    calls = _post_ok(monkeypatch, access="unused")
    session = _FakeSession(_record())
    service = CredentialService(settings, session)
    assert service.access_token() == "old-token"
    assert calls == []


def test_expiring_token_is_refreshed(settings, monkeypatch) -> None:
    new_token = make_jwt(3600)
    calls = _post_ok(monkeypatch, access=new_token, refresh="refresh-2")
    session = _FakeSession(_record(expires_in=60))
    service = CredentialService(settings, session)
    assert service.access_token() == new_token
    assert len(calls) == 1
    assert session.record.refresh_token_enc and session.commits >= 1


def test_rejected_token_is_refreshed_even_when_unexpired(settings, monkeypatch) -> None:
    """Regression: a 401 must rotate the credential even before its expiry."""
    new_token = make_jwt(3600)
    calls = _post_ok(monkeypatch, access=new_token)
    session = _FakeSession(_record(expires_in=3600))
    service = CredentialService(settings, session)
    assert service.access_token(rejected_token="old-token") == new_token
    assert len(calls) == 1


def test_rejected_token_already_rotated_is_not_refreshed_again(settings, monkeypatch) -> None:
    calls = _post_ok(monkeypatch, access="unused")
    session = _FakeSession(_record(access="already-new"))
    service = CredentialService(settings, session)
    assert service.access_token(rejected_token="old-token") == "already-new"
    assert calls == []


def test_refresh_failure_marks_needs_authorization_and_commits(settings, monkeypatch) -> None:
    def failing_post(*_args, **_kwargs):  # noqa: ANN001
        raise httpx.ConnectError("oauth unreachable")

    monkeypatch.setattr(credentials_module.httpx, "post", failing_post)
    session = _FakeSession(_record(expires_in=60))
    service = CredentialService(settings, session)
    assert service.try_refresh() is False
    assert session.record.status == CredentialStatus.NEEDS_AUTHORIZATION.value
    assert "oauth unreachable" in session.record.last_error
    assert session.commits == 1


def test_failed_refresh_is_not_hammered_within_cooldown(settings, monkeypatch) -> None:
    attempts = {"count": 0}

    def failing_post(*_args, **_kwargs):  # noqa: ANN001
        attempts["count"] += 1
        raise httpx.ConnectError("oauth unreachable")

    monkeypatch.setattr(credentials_module.httpx, "post", failing_post)
    session = _FakeSession(_record(expires_in=60))
    service = CredentialService(settings, session)
    assert service.try_refresh() is False
    assert service.try_refresh() is False
    assert attempts["count"] == 1


def test_single_flight_refresh_across_threads(settings, monkeypatch) -> None:
    new_token = make_jwt(3600)
    calls: list[dict] = []
    barrier = threading.Barrier(2)

    def slow_post(url, data=None, headers=None, timeout=None):  # noqa: ANN001
        calls.append({"url": url, "data": data})
        time.sleep(0.05)
        return _fake_response({"access_token": new_token, "refresh_token": "rt-2"})

    monkeypatch.setattr(credentials_module.httpx, "post", slow_post)
    session = _FakeSession(_record(expires_in=60))
    service = CredentialService(settings, session)
    results: list[bool] = []

    def worker() -> None:
        barrier.wait()
        results.append(service.try_refresh(force=True, expected_token="old-token"))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == [True, True]
    assert len(calls) == 1


def test_401_recovery_flow_rotates_once_and_retries(settings, monkeypatch) -> None:
    """End-to-end: 401 → provider rotates → the request is retried once."""
    new_token = make_jwt(3600)
    calls = _post_ok(monkeypatch, access=new_token)

    responses = [
        httpx.Response(401, json={"error": "unauthorized"}),
        httpx.Response(202, json={}),
        httpx.Response(
            200,
            json={"response": {"fulfillmentGroups": [{"items": [{"skuId": "g0", "itemCosts": {}}]}]}},
        ),
    ]
    seen_auth: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers["Authorization"])
        return responses.pop(0)

    session = _FakeSession(_record(expires_in=3600))
    service = CredentialService(settings, session)
    client = NikeClient(
        settings,
        service.access_token(),
        token_provider=service.token_provider(),
        sleep=lambda _seconds: None,
        transport=httpx.MockTransport(handler),
    )
    batch = client.review(["g0"], region_id="NIKE0S001")
    assert batch.trusted is True
    assert len(calls) == 1
    assert seen_auth[0] == "Bearer old-token"
    assert seen_auth[1] == f"Bearer {new_token}"


def test_refresh_without_new_token_bounds_retries(settings, monkeypatch) -> None:
    def failing_post(*_args, **_kwargs):  # noqa: ANN001
        raise httpx.ConnectError("oauth unreachable")

    monkeypatch.setattr(credentials_module.httpx, "post", failing_post)
    responses = [httpx.Response(401, json={"error": "unauthorized"})]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    session = _FakeSession(_record(expires_in=3600))
    service = CredentialService(settings, session)
    client = NikeClient(
        settings,
        service.access_token(),
        token_provider=service.token_provider(),
        sleep=lambda _seconds: None,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ScanError) as excinfo:
        client.review(["g0"], region_id="NIKE0S001")
    assert excinfo.value.code == ErrorCode.AUTH_REQUIRED
    assert len(requests) == 1
