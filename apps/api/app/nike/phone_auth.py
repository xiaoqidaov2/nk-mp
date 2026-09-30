"""Phone + SMS login against the Nike CN identity service.

The wire protocol was frozen on 2026-09-29 against the publicly served SPA
(see ``local://nike-phone-contract.md``); every request shape below is the one
the real page issues, and each step was exercised end to end before this
module was written:

1. ``GET  /authorize/v1`` (mp client, PKCE S256)   -> login page
2. ``POST /credential_lookup/v1``                 -> account challenge shape
3. ``POST /verification_code/send/v1``            -> 204, real SMS
4. ``POST /challenge/code/v1``                    -> 200 (exact body matters)
5. ``POST /check/v1``                             -> authorization code in redirect_uri
6. ``POST /token/v1``                             -> access + refresh tokens (PKCE)

The SMS send / verify / exchange steps are never replayed automatically: a
timeout leaves the result unknown and the caller must not try again.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlencode, urljoin, urlparse

import httpx

from app.config import Settings
from app.nike.auth_waf import extract_arg1, looks_like_challenge, solve_acw_sc_v2

# Contract version of the frozen protocol; a deployment may only offer phone
# login when its configured version equals this value.
SUPPORTED_CONTRACT_VERSION = "2026-09-29/mp-phone-otc-pkce"

MP_REDIRECT_URI = "https://mp-static-assets.gc.nike.com/auth/wechat/index.html"
MP_SCOPE = "openid offline_access nike.digital profile phone flow"
ALLOWED_HOSTS = {"accounts.nike.com.cn", "mp-static-assets.gc.nike.com"}
MAX_CHALLENGE_SOLVES = 3
MAX_REDIRECT_HOPS = 5


class PhoneAuthError(RuntimeError):
    code = "NIKE_AUTH_NOT_AVAILABLE"
    retry_after_seconds: int | None = None


class PhoneAuthAccountNotFound(PhoneAuthError):
    code = "NIKE_AUTH_REQUEST_CONFLICT"


class PhoneAuthSendUncertain(PhoneAuthError):
    code = "NIKE_SMS_SEND_UNCERTAIN"


class PhoneAuthVerifyUncertain(PhoneAuthError):
    code = "NIKE_VERIFY_UNCERTAIN"


class PhoneAuthChallengeRequired(PhoneAuthError):
    code = "NIKE_CHALLENGE_REQUIRED"


class PhoneAuthContractChanged(PhoneAuthError):
    code = "NIKE_AUTH_CONTRACT_CHANGED"


class PhoneAuthCodeInvalid(PhoneAuthError):
    code = "NIKE_CODE_INVALID"


class PhoneAuthCodeExpired(PhoneAuthError):
    code = "NIKE_CODE_EXPIRED"


class PhoneAuthRateLimited(PhoneAuthError):
    code = "NIKE_AUTH_RATE_LIMITED"


class PhoneAuthUnavailable(PhoneAuthError):
    """Transient upstream failure before anything was sent (no SMS issued)."""

    code = "NIKE_UPSTREAM_UNAVAILABLE"


@dataclass
class PhoneAuthChallenge:
    """Verified upstream session state needed to finish the login later."""

    phone_e164: str
    phone_masked: str
    client_id: str
    redirect_uri: str
    scope: str
    state: str
    code_verifier: str = field(repr=False)
    code_challenge: str
    cookies: dict[str, str] = field(repr=False, default_factory=dict)
    challenge_kind: str = ""

    def to_context(self) -> dict:
        """Serialisable form persisted (encrypted) between the two calls."""
        return {
            "phone": self.phone_e164,
            "masked": self.phone_masked,
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": self.scope,
            "state": self.state,
            "code_verifier": self.code_verifier,
            "code_challenge": self.code_challenge,
            "cookies": self.cookies,
            "challenge_kind": self.challenge_kind,
        }

    @classmethod
    def from_context(cls, data: dict) -> PhoneAuthChallenge:
        return cls(
            phone_e164=str(data.get("phone") or ""),
            phone_masked=str(data.get("masked") or ""),
            client_id=str(data.get("client_id") or ""),
            redirect_uri=str(data.get("redirect_uri") or ""),
            scope=str(data.get("scope") or ""),
            state=str(data.get("state") or ""),
            code_verifier=str(data.get("code_verifier") or ""),
            code_challenge=str(data.get("code_challenge") or ""),
            cookies={str(k): str(v) for k, v in (data.get("cookies") or {}).items()},
            challenge_kind=str(data.get("challenge_kind") or ""),
        )


@dataclass
class PhoneAuthTokens:
    access_token: str = field(repr=False)
    refresh_token: str | None = field(repr=False)
    client_id: str
    access_expires_at: datetime | None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _decode_expiry(access_token: str) -> datetime | None:
    parts = access_token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    try:
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        exp = claims.get("exp")
    except Exception:  # noqa: BLE001 - unknown expiry is acceptable
        return None
    if not isinstance(exp, (int, float)):
        return None
    return datetime.fromtimestamp(exp, tz=UTC)


def mask_phone(phone_e164: str) -> str:
    if len(phone_e164) >= 7:
        return phone_e164[:3] + "****" + phone_e164[-4:]
    return "***"


class NikePhoneAuthClient:
    """Single HTTP adapter for the frozen phone-auth contract."""

    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        self._client = httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(25.0),
            follow_redirects=False,
            headers={"User-Agent": settings.nike_user_agent},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> NikePhoneAuthClient:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ------------------------------------------------------------------ steps

    def start(self, phone_e164: str) -> PhoneAuthChallenge:
        """Open a clean session, look the phone up and trigger the SMS."""
        cookies: dict[str, str] = {}
        verifier = _b64url(os.urandom(32))
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        state = os.urandom(8).hex()
        client_id = self.settings.oauth_client_id
        params = {
            "client_id": client_id,
            "redirect_uri": MP_REDIRECT_URI,
            "response_type": "code",
            "scope": MP_SCOPE,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        authorize_url = f"{self.settings.nike_oauth_base}/authorize/v1?{urlencode(params)}"
        try:
            response = self._request("GET", authorize_url, cookies=cookies)
        except httpx.TransportError as exc:
            raise PhoneAuthUnavailable("Nike 服务暂不可用，请稍后重试") from exc
        if response.status_code != 200 or looks_like_challenge(response.content):
            raise PhoneAuthContractChanged("登录页不可用")

        try:
            lookup = self._request(
                "POST",
                self._url("/credential_lookup/v1"),
                cookies=cookies,
                headers={"Content-Type": "application/json", "x-nike-ux-id": "com.nike.unite"},
                json_body={"credential": phone_e164, "client_id": client_id},
            )
        except httpx.TransportError as exc:
            # Nothing was sent yet: this is a clean transient failure.
            raise PhoneAuthUnavailable("Nike 服务暂不可用，请稍后重试") from exc
        if lookup.status_code == 404:
            raise PhoneAuthAccountNotFound("该手机号没有 Nike 账号，请先注册或更换手机号")
        if lookup.status_code != 200:
            raise PhoneAuthContractChanged("账号查询返回了未知结果")
        try:
            lookup_data = lookup.json()
        except Exception as exc:  # noqa: BLE001
            raise PhoneAuthContractChanged("账号查询返回了未知结果") from exc
        if not isinstance(lookup_data, dict):
            raise PhoneAuthContractChanged("账号查询返回了未知结果")

        legacy_login = str(lookup_data.get("legacySwooshChallenge", "none")) != "none"
        swoosh_login = str(lookup_data.get("challenge", "none")) != "none" and bool(
            lookup_data.get("swooshEntitlement")
        )
        swoosh_flag = bool(legacy_login or (swoosh_login and not lookup_data.get("swooshEntitlement")))

        try:
            sent = self._request(
                "POST",
                self._url("/verification_code/send/v1"),
                cookies=cookies,
                headers={"Content-Type": "application/json"},
                json_body={
                    "destination": phone_e164,
                    "country": "CN",
                    "type": "CHALLENGE",
                    "client_id": client_id,
                    "language": "zh-Hans",
                    "swoosh_login": swoosh_flag,
                },
            )
        except httpx.TransportError as exc:
            # The request may or may not have reached Nike; never retry.
            raise PhoneAuthSendUncertain("短信发送结果不确定") from exc
        if sent.status_code == 429:
            raise PhoneAuthRateLimited("请求过于频繁，请稍后再试")
        if sent.status_code not in (200, 204):
            raise PhoneAuthContractChanged("发码接口返回了未知结果")

        masked = mask_phone(phone_e164)
        if sent.status_code == 200:
            try:
                payload = sent.json()
                channels = payload.get("additional_channels") or {}
                upstream_mask = str(channels.get("phone_number") or "")
                if upstream_mask:
                    masked = upstream_mask
            except Exception:  # noqa: BLE001 - the mask is cosmetic only
                pass
        return PhoneAuthChallenge(
            phone_e164=phone_e164,
            phone_masked=masked,
            client_id=client_id,
            redirect_uri=MP_REDIRECT_URI,
            scope=MP_SCOPE,
            state=state,
            code_verifier=verifier,
            code_challenge=challenge,
            cookies=cookies,
            challenge_kind=str(lookup_data.get("challenge") or ""),
        )

    def verify(self, context: PhoneAuthChallenge, code: str) -> PhoneAuthTokens:
        """Verify the SMS code, finish the session and exchange the code."""
        cookies = dict(context.cookies)
        try:
            response = self._request(
                "POST",
                self._url("/challenge/code/v1"),
                cookies=cookies,
                headers={"Content-Type": "application/json"},
                json_body={
                    "credential": context.phone_e164,
                    "verification_code": code,
                    "client_id": context.client_id,
                    "verification_code_type": "CHALLENGE",
                },
            )
        except httpx.TransportError as exc:
            raise PhoneAuthVerifyUncertain("验证码校验结果不确定") from exc
        if response.status_code == 429:
            raise PhoneAuthRateLimited("请求过于频繁，请稍后再试")
        if response.status_code not in (200, 204):
            raise self._map_verify_error(response)

        try:
            check = self._request(
                "POST",
                self._url("/check/v1"),
                cookies=cookies,
                headers={"Content-Type": "application/json"},
                json_body={
                    "client_id": context.client_id,
                    "redirect_uri": context.redirect_uri,
                    "scope": context.scope,
                    "code_challenge": context.code_challenge,
                    "code_challenge_method": "S256",
                    "state": context.state,
                    "flow": "LOGIN",
                },
            )
        except httpx.TransportError as exc:
            raise PhoneAuthVerifyUncertain("会话完成结果不确定") from exc
        if check.status_code != 200:
            raise PhoneAuthContractChanged("会话完成返回了未知结果")
        try:
            check_data = check.json()
        except Exception as exc:  # noqa: BLE001
            raise PhoneAuthContractChanged("会话完成返回了未知结果") from exc
        required = check_data.get("requiredStates")
        if required:
            raise PhoneAuthChallengeRequired("需要额外验证步骤后才能完成登录")

        code_value = _extract_code(str(check_data.get("redirect_uri") or ""))
        if not code_value:
            code_value = self._capture_code_via_redirect(str(check_data.get("redirect_uri") or ""), cookies)
        if not code_value:
            raise PhoneAuthContractChanged("未取得授权码")

        try:
            token = self._request(
                "POST",
                self._url("/token/v1"),
                cookies=cookies,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                form_body={
                    "grant_type": "authorization_code",
                    "code": code_value,
                    "redirect_uri": context.redirect_uri,
                    "client_id": context.client_id,
                    "code_verifier": context.code_verifier,
                },
            )
        except httpx.TransportError as exc:
            raise PhoneAuthVerifyUncertain("授权码兑换结果不确定") from exc
        if token.status_code == 429:
            raise PhoneAuthRateLimited("请求过于频繁，请稍后再试")
        if token.status_code != 200:
            raise PhoneAuthVerifyUncertain("授权码兑换失败，请重新获取验证码")
        try:
            payload = token.json()
        except Exception as exc:  # noqa: BLE001
            raise PhoneAuthVerifyUncertain("授权码兑换结果不确定") from exc
        access_token = str(payload.get("access_token") or "")
        if not access_token:
            raise PhoneAuthVerifyUncertain("授权码兑换结果不确定")
        refresh_token = str(payload.get("refresh_token") or "") or None
        return PhoneAuthTokens(
            access_token=access_token,
            refresh_token=refresh_token,
            client_id=context.client_id,
            access_expires_at=_decode_expiry(access_token),
        )

    # -------------------------------------------------------------- internals

    def _url(self, path: str) -> str:
        return f"{self.settings.nike_oauth_base}{path}"

    def _map_verify_error(self, response: httpx.Response) -> PhoneAuthError:
        if looks_like_challenge(response.content):
            return PhoneAuthContractChanged("上游要求完成额外挑战")
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001
            return PhoneAuthContractChanged("验证码校验返回了未知结果")
        errors = payload.get("errors") or []
        first = errors[0] if errors else {}
        code = str(first.get("code") or "")
        if code in ("invalid_verification_code", "invalid_code"):
            return PhoneAuthCodeInvalid("验证码不正确")
        if code in ("verification_code_expired", "expired_verification_code"):
            return PhoneAuthCodeExpired("验证码已过期，请重新获取")
        return PhoneAuthContractChanged("验证码校验返回了未知结果")

    def _capture_code_via_redirect(self, url: str, cookies: dict[str, str]) -> str | None:
        """Follow the identity redirect chain (same registered redirect) to find the code."""
        if not url:
            return None
        current = url
        for _ in range(MAX_REDIRECT_HOPS):
            if urlparse(current).hostname not in ALLOWED_HOSTS:
                return None
            try:
                response = self._request("GET", current, cookies=cookies)
            except httpx.TransportError:
                return None
            code = _extract_code(str(response.headers.get("location") or ""))
            if code:
                return code
            if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("location"):
                current = urljoin(current, str(response.headers.get("location")))
                continue
            return None
        return None

    def _request(
        self,
        method: str,
        url: str,
        *,
        cookies: dict[str, str],
        headers: dict[str, str] | None = None,
        json_body: dict | None = None,
        form_body: dict | None = None,
    ) -> httpx.Response:
        """One logical request: solve WAF challenges, absorb cookies, follow redirects.

        Redirect targets must stay on the fixed Nike hosts; authentication
        material is never sent anywhere else. Input data is never logged.
        """
        current_url = url
        current_method = method
        content = None
        request_headers = dict(headers or {})
        if json_body is not None:
            content = json.dumps(json_body).encode()
        elif form_body is not None:
            content = urlencode(form_body).encode()

        solves = 0
        hops = 0
        while True:
            response = self._client.request(
                current_method,
                current_url,
                headers=request_headers,
                content=content,
                cookies=cookies,
            )
            for key, value in response.cookies.items():
                cookies[key] = value
            if looks_like_challenge(response.content):
                arg1 = extract_arg1(response.content)
                if arg1 and solves < MAX_CHALLENGE_SOLVES:
                    cookies["acw_sc__v2"] = solve_acw_sc_v2(arg1)
                    solves += 1
                    continue
                raise PhoneAuthContractChanged("上游要求完成额外挑战")
            location = response.headers.get("location")
            if (
                response.status_code in (301, 302, 303, 307, 308)
                and location
                and hops < MAX_REDIRECT_HOPS
            ):
                target = urljoin(current_url, str(location))
                if urlparse(target).hostname not in ALLOWED_HOSTS:
                    raise PhoneAuthContractChanged("上游返回了站外跳转")
                hops += 1
                current_url = target
                if response.status_code == 303:
                    current_method, content = "GET", None
                continue
            return response


def _extract_code(url: str) -> str | None:
    from urllib.parse import parse_qs

    query = parse_qs(urlparse(url).query)
    values = query.get("code") or []
    return values[0] if values else None
