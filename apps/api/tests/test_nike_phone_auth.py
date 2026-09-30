"""Phone + SMS login adapter against the frozen Nike contract.

All upstream traffic is served by ``httpx.MockTransport``: no test may reach a
real Nike host. The tests pin three properties of the adapter:

* the WAF challenge is solved with a self-computed ``acw_sc__v2`` cookie and
  the solved request is retried (the SMS is then sent exactly once);
* every failure mode maps to its stable ``NIKE_*`` code, and error text never
  echoes a phone number, the SMS code or a token;
* a timeout or an unknown result is never replayed automatically — the mock
  sees no further request after the uncertain step.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.nike.auth_waf import solve_acw_sc_v2
from app.nike.phone_auth import (
    MP_REDIRECT_URI,
    MP_SCOPE,
    NikePhoneAuthClient,
    PhoneAuthAccountNotFound,
    PhoneAuthChallenge,
    PhoneAuthCodeInvalid,
    PhoneAuthContractChanged,
    PhoneAuthRateLimited,
    PhoneAuthSendUncertain,
    PhoneAuthVerifyUncertain,
)

from .conftest import make_jwt

PHONE = "+8613800138000"
SMS_CODE = "654321"
ACCESS_TOKEN = make_jwt(3600)
REFRESH_TOKEN = "REFRESH-TOKEN-SECRET"
ARG1 = "0123456789abcdef0123456789abcdef01234567"
SOLVED_COOKIE = solve_acw_sc_v2(ARG1)

LOGIN_PAGE = httpx.Response(200, content=b"<!doctype html><title>login</title>")


def _json(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _challenge() -> httpx.Response:
    body = (
        f"<html><script>var arg1='{ARG1}';document.cookie='acw_sc__v2'</script></html>"
    ).encode()
    return httpx.Response(405, content=body)


class _Replay:
    """Queue-based MockTransport; any extra request fails the test loudly."""

    def __init__(self, responses: list[httpx.Response | Exception]) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected extra upstream request")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def client(self, settings) -> NikePhoneAuthClient:  # noqa: ANN001
        return NikePhoneAuthClient(settings, transport=httpx.MockTransport(self.handler))


def _context(**overrides) -> PhoneAuthChallenge:  # noqa: ANN003
    fields: dict = {
        "phone_e164": PHONE,
        "phone_masked": "+86****8000",
        "client_id": "client-1",
        "redirect_uri": MP_REDIRECT_URI,
        "scope": MP_SCOPE,
        "state": "state-1",
        "code_verifier": "verifier-1",
        "code_challenge": "challenge-1",
        "cookies": {"acw_sc__v2": SOLVED_COOKIE},
    }
    fields.update(overrides)
    return PhoneAuthChallenge(**fields)


def _cookie_header(request: httpx.Request) -> str:
    return request.headers.get("cookie") or ""


def _assert_no_secrets(message: str, *secrets: str) -> None:
    for secret in secrets:
        assert secret not in message


# --------------------------------------------------------------------- start()


def test_start_solves_waf_then_sends_code_once(settings) -> None:
    replay = _Replay(
        [
            _challenge(),  # GET /authorize/v1 -> WAF page
            LOGIN_PAGE,  # retried with the self-computed cookie
            _json({"challenge": "SWOOSH_OTP"}),  # POST /credential_lookup/v1
            httpx.Response(204),  # POST /verification_code/send/v1
        ]
    )
    with replay.client(settings) as client:
        challenge = client.start(PHONE)

    assert challenge.phone_e164 == PHONE
    assert challenge.phone_masked == "+86****8000"
    assert challenge.challenge_kind == "SWOOSH_OTP"
    assert challenge.client_id == settings.oauth_client_id
    assert challenge.redirect_uri == MP_REDIRECT_URI
    assert challenge.state and challenge.code_verifier and challenge.code_challenge
    assert challenge.cookies["acw_sc__v2"] == SOLVED_COOKIE

    # Exactly four upstream hits: the challenge is solved, not looped forever.
    assert len(replay.requests) == 4
    # The first authorize call was unauthenticated with the WAF cookie...
    assert "acw_sc__v2" not in _cookie_header(replay.requests[0])
    # ...and the retry used the cookie this adapter computed from arg1.
    assert f"acw_sc__v2={SOLVED_COOKIE}" in _cookie_header(replay.requests[1])
    # The solved cookie stays on the session for the following steps.
    assert f"acw_sc__v2={SOLVED_COOKIE}" in _cookie_header(replay.requests[2])
    assert f"acw_sc__v2={SOLVED_COOKIE}" in _cookie_header(replay.requests[3])


def test_start_lookup_404_is_account_not_found(settings) -> None:
    replay = _Replay([LOGIN_PAGE, httpx.Response(404, json={"error": "not_found"})])
    with pytest.raises(PhoneAuthAccountNotFound) as excinfo, replay.client(settings) as client:
        client.start(PHONE)
    assert excinfo.value.code == "NIKE_AUTH_REQUEST_CONFLICT"
    assert len(replay.requests) == 2
    _assert_no_secrets(str(excinfo.value), PHONE, "13800138000")


def test_start_unknown_send_result_is_contract_changed(settings) -> None:
    replay = _Replay([LOGIN_PAGE, _json({}), _json({"unexpected": True}, status=500)])
    with pytest.raises(PhoneAuthContractChanged) as excinfo, replay.client(settings) as client:
        client.start(PHONE)
    assert excinfo.value.code == "NIKE_AUTH_CONTRACT_CHANGED"
    assert len(replay.requests) == 3


def test_start_send_timeout_is_uncertain_and_never_replayed(settings) -> None:
    replay = _Replay([LOGIN_PAGE, _json({}), httpx.ConnectTimeout("send timed out")])
    with pytest.raises(PhoneAuthSendUncertain) as excinfo, replay.client(settings) as client:
        client.start(PHONE)
    assert excinfo.value.code == "NIKE_SMS_SEND_UNCERTAIN"
    # The SMS may already have been sent: no automatic replay is allowed.
    assert len(replay.requests) == 3


# -------------------------------------------------------------------- verify()


def test_verify_success_returns_tokens_with_refresh(settings) -> None:
    replay = _Replay(
        [
            httpx.Response(200),  # POST /challenge/code/v1
            _json({"redirect_uri": f"{MP_REDIRECT_URI}?code=AUTH-CODE-1&state=state-1"}),
            _json({"access_token": ACCESS_TOKEN, "refresh_token": REFRESH_TOKEN}),
        ]
    )
    context = _context(client_id=settings.oauth_client_id)
    with replay.client(settings) as client:
        tokens = client.verify(context, SMS_CODE)

    assert tokens.access_token == ACCESS_TOKEN
    assert tokens.refresh_token == REFRESH_TOKEN
    assert tokens.client_id == settings.oauth_client_id
    assert tokens.access_expires_at is not None
    assert tokens.access_expires_at > datetime.now(UTC) + timedelta(minutes=30)
    assert len(replay.requests) == 3

    token_body = replay.requests[2].content.decode()
    assert "grant_type=authorization_code" in token_body
    assert "code_verifier=verifier-1" in token_body
    assert "code=AUTH-CODE-1" in token_body


def test_verify_success_without_refresh_token(settings) -> None:
    replay = _Replay(
        [
            httpx.Response(200),
            _json({"redirect_uri": f"{MP_REDIRECT_URI}?code=AUTH-CODE-2"}),
            _json({"access_token": ACCESS_TOKEN}),
        ]
    )
    with replay.client(settings) as client:
        tokens = client.verify(_context(), SMS_CODE)
    assert tokens.access_token == ACCESS_TOKEN
    assert tokens.refresh_token is None
    assert len(replay.requests) == 3


def test_verify_invalid_code_maps_to_stable_error(settings) -> None:
    replay = _Replay(
        [
            _json({"errors": [{"code": "invalid_verification_code"}]}, status=400),
        ]
    )
    with pytest.raises(PhoneAuthCodeInvalid) as excinfo, replay.client(settings) as client:
        client.verify(_context(), SMS_CODE)
    assert excinfo.value.code == "NIKE_CODE_INVALID"
    assert len(replay.requests) == 1
    _assert_no_secrets(str(excinfo.value), PHONE, "13800138000", SMS_CODE, ACCESS_TOKEN)


def test_verify_rate_limited(settings) -> None:
    replay = _Replay([_json({"error": "too_many"}, status=429)])
    with pytest.raises(PhoneAuthRateLimited) as excinfo, replay.client(settings) as client:
        client.verify(_context(), SMS_CODE)
    assert excinfo.value.code == "NIKE_AUTH_RATE_LIMITED"
    assert len(replay.requests) == 1


def test_verify_waf_challenge_is_contract_changed(settings) -> None:
    # The first verify request keeps answering with a challenge; after the
    # solve budget is exhausted the adapter must give up, not loop.
    replay = _Replay([_challenge() for _ in range(4)])
    with pytest.raises(PhoneAuthContractChanged) as excinfo, replay.client(settings) as client:
        client.verify(_context(), SMS_CODE)
    assert excinfo.value.code == "NIKE_AUTH_CONTRACT_CHANGED"
    assert len(replay.requests) == 4


def test_verify_token_timeout_is_uncertain_and_never_replayed(settings) -> None:
    replay = _Replay(
        [
            httpx.Response(204),
            _json({"redirect_uri": f"{MP_REDIRECT_URI}?code=AUTH-CODE-3"}),
            httpx.ConnectTimeout("token exchange timed out"),
        ]
    )
    with pytest.raises(PhoneAuthVerifyUncertain) as excinfo, replay.client(settings) as client:
        client.verify(_context(), SMS_CODE)
    assert excinfo.value.code == "NIKE_VERIFY_UNCERTAIN"
    assert len(replay.requests) == 3
    _assert_no_secrets(str(excinfo.value), PHONE, SMS_CODE, ACCESS_TOKEN)


def test_verify_token_without_access_token_is_uncertain(settings) -> None:
    replay = _Replay(
        [
            httpx.Response(204),
            _json({"redirect_uri": f"{MP_REDIRECT_URI}?code=AUTH-CODE-4"}),
            _json({"token_type": "bearer"}),
        ]
    )
    with pytest.raises(PhoneAuthVerifyUncertain) as excinfo, replay.client(settings) as client:
        client.verify(_context(), SMS_CODE)
    assert excinfo.value.code == "NIKE_VERIFY_UNCERTAIN"
    assert len(replay.requests) == 3
