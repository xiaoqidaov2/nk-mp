"""T04: credential lifecycle — the parts that need no real PostgreSQL.

The old file exercised rotation, single-flight and commit isolation through a
shared duck-typed session. Those invariants depend on row locks and separate
short transactions, so they are verified against real PostgreSQL in
``tests/integration`` and must not be "proved" with a fake session here.

What remains genuinely unit-testable is pure logic:

* :func:`_decode_expiry` — token claim decoding and its fallback to ``None``;
* the stable ``NIKE_*`` ``.code`` on the credential exception hierarchy;
* the ``status()`` derivation (NEEDS_AUTHORIZATION only when an expired row
  cannot renew; everything else stays a plain connected state)
  against a single lightweight credential row.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models import Credential, CredentialStatus
from app.services.credentials import (
    CredentialChanged,
    CredentialError,
    CredentialReauthRequired,
    CredentialRequired,
    CredentialService,
    CredentialUnavailable,
    _decode_expiry,
)

from .conftest import make_jwt

PHONE_MASKED = "+86****8000"


# --------------------------------------------------------------------- helpers


class _ScalarResult:
    def __init__(self, value):  # noqa: ANN001 - duck-typed for the test
        self._value = value

    def scalar_one_or_none(self):  # noqa: ANN201
        return self._value


class _StubSession:
    """Context-managing session that serves exactly one credential row."""

    def __init__(self, record: Credential | None) -> None:
        self.record = record
        self.closed = False

    def execute(self, _stmt):  # noqa: ANN001 - duck-typed for the test
        return _ScalarResult(self.record)

    def __enter__(self) -> _StubSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.closed = True


def _service(settings, record: Credential | None = None) -> CredentialService:
    return CredentialService(
        settings, uuid4(), session_factory=lambda: _StubSession(record)
    )


def _credential(**overrides) -> Credential:  # noqa: ANN003
    fields: dict = {
        "owner_user_id": uuid4(),
        "binding_generation": 3,
        "status": CredentialStatus.ACTIVE.value,
        "access_expires_at": datetime.now(UTC) + timedelta(seconds=7200),
        "last_refreshed_at": datetime.now(UTC) - timedelta(seconds=30),
        "last_error_code": None,
        "phone_masked": PHONE_MASKED,
    }
    fields.update(overrides)
    return Credential(**fields)


def _token_with_claims(claims: object) -> str:  # noqa: ANN001
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


# ---------------------------------------------------------------- _decode_expiry


def test_decode_expiry_reads_the_exp_claim() -> None:
    decoded = _decode_expiry(make_jwt(3600))
    assert decoded is not None
    remaining = (decoded - datetime.now(UTC)).total_seconds()
    assert 3500 < remaining < 3700


def test_decode_expiry_uses_the_exact_claim_value() -> None:
    assert _decode_expiry(_token_with_claims({"exp": 1234567890})) == datetime.fromtimestamp(
        1234567890, tz=UTC
    )


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("no-dots-here", id="not-a-jwt"),
        pytest.param("header..signature", id="empty-payload"),
        pytest.param("header.not-base64!!.signature", id="undecodable-payload"),
        pytest.param(_token_with_claims({"sub": "no-expiry"}), id="missing-exp"),
        pytest.param(_token_with_claims({"exp": "soon"}), id="non-numeric-exp"),
        pytest.param(_token_with_claims([1, 2, 3]), id="non-object-claims"),
    ],
)
def test_decode_expiry_returns_none_for_unknown_expiry(token: str) -> None:
    assert _decode_expiry(token) is None


# ------------------------------------------------------------- exception codes


@pytest.mark.parametrize(
    ("error_cls", "expected_code"),
    [
        (CredentialError, "NIKE_UPSTREAM_UNAVAILABLE"),
        (CredentialRequired, "NIKE_CREDENTIAL_REQUIRED"),
        (CredentialChanged, "NIKE_CREDENTIAL_CHANGED"),
        (CredentialReauthRequired, "NIKE_REAUTH_REQUIRED"),
        (CredentialUnavailable, "NIKE_UPSTREAM_UNAVAILABLE"),
    ],
)
def test_credential_errors_expose_stable_codes(error_cls, expected_code: str) -> None:
    assert issubclass(error_cls, CredentialError)
    assert error_cls.code == expected_code
    assert error_cls("message").code == expected_code


@pytest.mark.parametrize(
    ("status", "expected_cls"),
    [
        (CredentialStatus.NEEDS_AUTHORIZATION.value, CredentialReauthRequired),
        (CredentialStatus.REVOKED.value, CredentialChanged),
        (CredentialStatus.UNASSIGNED.value, CredentialRequired),
        (CredentialStatus.ACTIVE.value, CredentialUnavailable),
        (CredentialStatus.EXPIRING.value, CredentialUnavailable),
    ],
)
def test_auth_failure_maps_state_to_error(settings, status: str, expected_cls) -> None:
    """A dead credential must surface as the matching stable NIKE_* error."""
    service = _service(settings)
    error = service._auth_failure(_credential(status=status), "boom")
    assert type(error) is expected_cls
    assert error.code == expected_cls.code


# --------------------------------------------------------------- status() view


def test_status_is_none_without_a_credential(settings) -> None:
    assert _service(settings, None).status() is None


def test_status_reports_active_when_far_from_expiry(settings) -> None:
    info = _service(settings, _credential()).status()
    assert info is not None
    assert info.status == CredentialStatus.ACTIVE.value


def test_status_stays_active_even_close_to_expiry(settings) -> None:
    """Renewal covers the last hour; the view must not cry "expiring"."""
    record = _credential(access_expires_at=datetime.now(UTC) + timedelta(seconds=1800))
    info = _service(settings, record).status()
    assert info is not None
    assert info.status == CredentialStatus.ACTIVE.value


def test_status_stays_active_when_expired_but_renewable(settings) -> None:
    """An expired access token with a refresh token still renews on its own."""
    record = _credential(
        access_expires_at=datetime.now(UTC) - timedelta(seconds=60),
        refresh_token_enc="enc:refresh",
    )
    info = _service(settings, record).status()
    assert info is not None
    assert info.status == CredentialStatus.ACTIVE.value


def test_status_derives_needs_authorization_when_expired_without_refresh_token(settings) -> None:
    record = _credential(access_expires_at=datetime.now(UTC) - timedelta(seconds=60))
    info = _service(settings, record).status()
    assert info is not None
    assert info.status == CredentialStatus.NEEDS_AUTHORIZATION.value


def test_status_keeps_non_active_state_even_when_expired(settings) -> None:
    """Only ACTIVE is re-derived; a revoked row must not look re-authorizable."""
    record = _credential(
        status=CredentialStatus.REVOKED.value,
        access_expires_at=datetime.now(UTC) - timedelta(seconds=60),
    )
    info = _service(settings, record).status()
    assert info is not None
    assert info.status == CredentialStatus.REVOKED.value


def test_status_without_expiry_stays_active(settings) -> None:
    info = _service(settings, _credential(access_expires_at=None)).status()
    assert info is not None
    assert info.status == CredentialStatus.ACTIVE.value


def test_status_exposes_row_metadata(settings) -> None:
    refreshed = datetime.now(UTC) - timedelta(seconds=30)
    record = _credential(
        binding_generation=7,
        last_refreshed_at=refreshed,
        last_error_code="NIKE_REAUTH_REQUIRED",
    )
    info = _service(settings, record).status()
    assert info is not None
    assert info.phone_masked == PHONE_MASKED
    assert info.access_expires_at == record.access_expires_at
    assert info.last_refreshed_at == refreshed
    assert info.last_error_code == "NIKE_REAUTH_REQUIRED"
    assert info.binding_generation == 7
