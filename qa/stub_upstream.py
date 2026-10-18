"""Offline upstream stub for the QA environments.

Emulates both the read-only Nike store APIs (regions, product structure, cart
reviews, OAuth refresh) and the frozen phone-auth contract that
``app/nike/phone_auth.py`` speaks. It never talks to the real Nike service.

Phone-auth contract
-------------------
Paths, methods, payloads and status codes are aligned with
``app/nike/phone_auth.py``:

1. ``GET  /authorize/v1``                  -> 200 HTML login page + session cookie
2. ``POST /credential_lookup/v1``          -> 200 for a registered phone, else 404
3. ``POST /verification_code/send/v1``     -> 204, counted in ``/__stats``
4. ``POST /challenge/code/v1``             -> 123456 -> 200, otherwise 400
                                              ``{"errors":[{"code":"invalid_verification_code"}]}``
5. ``POST /check/v1``                      -> 200 ``{"requiredStates":null,"redirect_uri":...}``
6. ``POST /token/v1``                      -> authorization_code *and* refresh_token grants

Every code carries the identity of the phone that requested it, so each signed
in account gets a *distinct* access/refresh token pair. Refreshing rotates both
tokens but keeps the same identity. ``/token/v1`` is shared with the platform's
credential-refresh path (``CredentialService._refresh_locked``).

Identity-aware data
-------------------
The o2o endpoints resolve the identity from ``Authorization: Bearer <token>``:

* a *scoped* account (registered with ``/__accounts``, used for accounts A/B)
  always answers with its own fixed price - the scenario switch never touches
  it, so one sku can be 699 for A and 799 for B side by side;
* ``default``/unknown tokens (and requests without a token, e.g. A/B never
  bound) fall back to the classic scenario mechanism below.

Scenarios (``POST /__scenario``) - applied to the default identity only
-----------------------------------------------------------------------
* ``baseline``: size 40 is out of stock, the rest is in stock. Prices are
  returned when every requested item is buyable.
* ``restock``: everything is in stock (triggers RESTOCK for size 40).
* ``untrusted``: every review answers with an empty COMPLETED payload, which
  must map to UNKNOWN rather than a stock conclusion.
* ``throttled``: the review *poll* answers 403 (rate limited) for the default
  identity, which must end the round with THROTTLED and enter the account
  cooldown instead of probing the remaining stores.
* ``accounts2``: the review *poll* answers 403 for the scoped identity ``A2``
  only - one platform user with two Nike accounts (``A``/``A2``): the account
  that got limited cools down and the very next round must be served by the
  other account. Every other identity (scoped ``A``/``B``, ``default``) keeps
  its normal behaviour.

``GET /__state`` exposes ``reviews_by_identity`` (review PUT+poll counts per
identity) so a client can prove *which* bound Nike account served a round.

All state is in memory; a restart clears it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlencode, urlparse

from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

app = FastAPI(title="Nike MP offline stub", version="0.2.0")

SCENARIOS = {
    "baseline",
    "restock",
    "untrusted",
    "throttled",
    "accounts2",
    "price_up",
    "waf",
}

#: The one phone the stub always knows, mapped to the scenario-driven default
#: identity. Additional accounts are registered through ``POST /__accounts``.
DEFAULT_PHONE = "+8613800000000"
DEFAULT_PRICE = 699

#: The single SMS code the challenge endpoint accepts.
FIXED_CODE = "123456"

#: Access-token lifetime; comfortably above the credential service's 1h
#: "EXPIRING" window so a fresh binding reads back as ACTIVE.
ACCESS_TTL_SECONDS = 7200

SESSION_COOKIE = "nike_stub_sid"


@dataclass
class Account:
    phone: str
    identity: str
    price: int
    #: Scoped accounts ignore the scenario switch (identity-specific data).
    scoped: bool = True


state = {"scenario": "baseline"}

accounts_by_phone: dict[str, Account] = {
    DEFAULT_PHONE: Account(DEFAULT_PHONE, "default", DEFAULT_PRICE, scoped=False)
}
accounts_by_identity: dict[str, Account] = {"default": accounts_by_phone[DEFAULT_PHONE]}

# sid -> {"phone": str | None, "code_challenge": str, "client_id": str}
sessions: dict[str, dict] = {}
# code -> {"identity": str, "code_challenge": str, "redirect_uri": str, "client_id": str}
codes: dict[str, dict] = {}
# token -> identity
access_tokens: dict[str, str] = {}
refresh_tokens: dict[str, str] = {}

reviews: dict[str, list[dict]] = {}

#: 推送渠道的离线落点：Apprise 的 json:// 目标会把 {title, message} POST 进来。
pushes: list[dict] = []

#: 每个身份（A / A2 / B / default）的 review 调用数：一个平台的多个 Nike 账号
#: 各用各的 token，靠它证明「这一轮究竟是哪个账号在跑」（``GET /__state``）。
reviews_by_identity: dict[str, int] = {}

stats = {"send_count": 0, "codes_issued": 0, "token_grants": 0, "refresh_grants": 0,
         "review_calls": 0, "waf_challenges": 0}


#: 压测用的额外门店数量（默认 0：冒烟仍然是 QA001/QA002 两家，行为不变）。
EXTRA_STORES = max(0, int(os.environ.get("STUB_EXTRA_STORES", "0")))

STORES = [
    {
        "id": "NIKE0S001",
        "storeCode": "QA001",
        "displayName": "离线换季优惠一店",
        "storeAddress": "测试路 1 号",
        "concept": "NK",
    },
    {
        "id": "NIKE0S002",
        "storeCode": "QA002",
        "displayName": "离线换季优惠二店",
        "storeAddress": "测试路 2 号",
        "concept": "NK",
    },
    *[
        {
            "id": f"NIKE0S{index:03d}",
            "storeCode": f"QA{index:03d}",
            "displayName": f"离线换季优惠压测店 {index}",
            "storeAddress": f"压测路 {index} 号",
            "concept": "NK",
        }
        for index in range(3, 3 + EXTRA_STORES)
    ],
]

# Store-scoped size structures, exactly like the real upstream: the same style
# can have different GTINs per store.
SIZES_BY_STORE: dict[str, list[tuple[str, str]]] = {
    "QA001": [("40", "QA001-40"), ("42", "QA001-42"), ("44", "QA001-44")],
    "QA002": [("40", "QA002-40"), ("42", "QA002-42"), ("44", "QA002-44"), ("46", "QA002-46")],
    **{
        f"QA{index:03d}": [
            ("40", f"QA{index:03d}-40"),
            ("42", f"QA{index:03d}-42"),
            ("44", f"QA{index:03d}-44"),
            ("46", f"QA{index:03d}-46"),
        ]
        for index in range(3, 3 + EXTRA_STORES)
    },
}

LOGIN_PAGE = (
    "<!doctype html><html><head><title>Nike 登录</title></head>"
    "<body><h1>离线登录页</h1><p>stub identity endpoint</p></body></html>"
)


class ScenarioIn(BaseModel):
    scenario: str


class StoreIn(BaseModel):
    store_code: str
    display_name: str = ""


class AccountIn(BaseModel):
    phone: str
    identity: str
    price: int
    scoped: bool = True


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _mint_access_token(account: Account) -> str:
    header = _b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = _b64url(
        json.dumps(
            {
                "sub": account.identity,
                "phone": account.phone,
                "iat": int(time.time()),
                "exp": int(time.time()) + ACCESS_TTL_SECONDS,
            }
        ).encode()
    )
    return f"{header}.{payload}.{uuid.uuid4().hex}"


def _mint_refresh_token(account: Account) -> str:
    return f"qa-refresh-{account.identity}-{uuid.uuid4().hex[:16]}"


def _issue_tokens(account: Account) -> dict:
    access = _mint_access_token(account)
    refresh = _mint_refresh_token(account)
    access_tokens[access] = account.identity
    refresh_tokens[refresh] = account.identity
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_in": ACCESS_TTL_SECONDS,
    }


def _identity_from_header(authorization: str) -> str | None:
    value = (authorization or "").strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    return access_tokens.get(value) if value else None


def _scoped_account(authorization: str) -> Account | None:
    """Return the scoped account behind the token, or None for the scenario path."""
    identity = _identity_from_header(authorization)
    if identity is None:
        return None
    account = accounts_by_identity.get(identity)
    if account is None or not account.scoped:
        return None
    return account


def _count_review(authorization: str) -> None:
    """Per-identity review traffic counter (``GET /__state``).

    Counts both the review PUT and every poll, so ``reviews_by_identity`` delta
    across a round tells which bound Nike account actually served it.
    """
    identity = _identity_from_header(authorization) or "default"
    reviews_by_identity[identity] = reviews_by_identity.get(identity, 0) + 1


# --------------------------------------------------------------------- admin


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "scenario": state["scenario"], "accounts": len(accounts_by_phone)}


@app.post("/__scenario")
def set_scenario(payload: ScenarioIn) -> dict:
    if payload.scenario not in SCENARIOS:
        return {"ok": False, "message": f"unknown scenario {payload.scenario}"}
    state["scenario"] = payload.scenario
    return {"ok": True, "scenario": state["scenario"]}


@app.post("/__stores")
def add_store(payload: StoreIn) -> dict:
    """运行期新增一家门店：验收「目录同步后新店自动纳入监控」。"""
    code = payload.store_code.strip()
    if not code:
        return {"ok": False, "message": "store_code is required"}
    if any(item["storeCode"] == code for item in STORES):
        return {"ok": True, "added": False, "stores": [item["storeCode"] for item in STORES]}
    index = len(STORES) + 1
    STORES.append(
        {
            "id": f"NIKE0S9{index:02d}",
            "storeCode": code,
            "displayName": payload.display_name or f"离线换季优惠新店 {index}",
            "storeAddress": f"新店路 {index} 号",
            "concept": "NK",
        }
    )
    SIZES_BY_STORE[code] = [
        ("40", f"{code}-40"),
        ("42", f"{code}-42"),
        ("44", f"{code}-44"),
        ("46", f"{code}-46"),
    ]
    return {"ok": True, "added": True, "stores": [item["storeCode"] for item in STORES]}


@app.post("/__accounts")
def register_account(payload: AccountIn) -> dict:
    """Register a phone with its own identity and price (used for accounts A/B)."""
    account = Account(payload.phone.strip(), payload.identity, int(payload.price), payload.scoped)
    accounts_by_phone[account.phone] = account
    accounts_by_identity[account.identity] = account
    return {
        "ok": True,
        "phone": account.phone,
        "identity": account.identity,
        "price": account.price,
        "scoped": account.scoped,
    }


@app.get("/__accounts")
def list_accounts() -> dict:
    return {
        "accounts": [
            {"phone": a.phone, "identity": a.identity, "price": a.price, "scoped": a.scoped}
            for a in accounts_by_phone.values()
        ]
    }


@app.get("/__stats")
def read_stats() -> dict:
    return {
        **stats,
        "scenario": state["scenario"],
        "active_access_tokens": len(access_tokens),
        "active_refresh_tokens": len(refresh_tokens),
        "push_count": len(pushes),
    }


@app.get("/__state")
def read_state() -> dict:
    """身份级的运行态：谁（A / A2 / B / default）真的发过 review 请求。

    冒烟用它证明「一用户多账号」的轮换：失败轮 A2 的计数增长、随后成功轮
    A2 的计数不变（``reviews_by_identity`` 是 PUT + 每次轮询的总和）。
    """
    return {
        "scenario": state["scenario"],
        "reviews_by_identity": dict(reviews_by_identity),
        "accounts": [
            {"phone": a.phone, "identity": a.identity, "price": a.price, "scoped": a.scoped}
            for a in accounts_by_phone.values()
        ],
    }


@app.post("/__push")
def receive_push(payload: dict) -> dict:
    """Record one Apprise ``json://`` payload (fields: title / message)."""
    pushes.append(
        {
            "title": payload.get("title", ""),
            "body": payload.get("message", "") or payload.get("body", ""),
            "received_at": datetime.now(UTC).isoformat(),
        }
    )
    return {"ok": True, "count": len(pushes)}


@app.get("/__push")
def list_pushes() -> list[dict]:
    return pushes


@app.post("/__reset")
def reset() -> dict:
    """Clear every ephemeral table between smoke runs (accounts included)."""
    state["scenario"] = "baseline"
    accounts_by_phone.clear()
    accounts_by_identity.clear()
    default = Account(DEFAULT_PHONE, "default", DEFAULT_PRICE, scoped=False)
    accounts_by_phone[DEFAULT_PHONE] = default
    accounts_by_identity["default"] = default
    sessions.clear()
    codes.clear()
    access_tokens.clear()
    refresh_tokens.clear()
    reviews.clear()
    pushes.clear()
    reviews_by_identity.clear()
    for key in stats:
        stats[key] = 0
    return {"ok": True}


# ---------------------------------------------------------------- identity


@app.get("/authorize/v1")
def authorize(
    request: Request,
    client_id: str = "",
    redirect_uri: str = "",  # noqa: ARG001 - echoed by /check/v1 only
    state: str = "",
    scope: str = "",  # noqa: ARG001
    code_challenge: str = "",
    code_challenge_method: str = "S256",  # noqa: ARG001
) -> HTMLResponse:
    sid = uuid.uuid4().hex
    sessions[sid] = {"phone": None, "code_challenge": code_challenge, "client_id": client_id}
    for stale in list(sessions)[:-200]:
        sessions.pop(stale, None)
    page = HTMLResponse(LOGIN_PAGE)
    page.set_cookie(SESSION_COOKIE, sid, path="/", httponly=True, samesite="lax")
    return page


def _session_for(request: Request) -> dict | None:
    sid = request.cookies.get(SESSION_COOKIE, "")
    return sessions.get(sid) if sid else None


@app.post("/credential_lookup/v1")
def credential_lookup(payload: dict, request: Request) -> JSONResponse:
    phone = str(payload.get("credential") or "").strip()
    account = accounts_by_phone.get(phone)
    if account is None:
        return JSONResponse(status_code=404, content={"errors": [{"code": "credential_not_found"}]})
    session = _session_for(request)
    if session is not None:
        session["phone"] = phone
    return JSONResponse(
        status_code=200,
        content={
            "id": f"acct-{account.identity}",
            "legacySwooshChallenge": "none",
            "challenge": "none",
            "swooshEntitlement": False,
            "phone_number": phone,
        },
    )


@app.post("/verification_code/send/v1")
def send_verification_code(payload: dict, request: Request) -> Response:
    phone = str(payload.get("destination") or "").strip()
    session = _session_for(request)
    if session is not None and phone:
        session["phone"] = phone
    stats["send_count"] += 1
    return Response(status_code=204)


@app.post("/challenge/code/v1")
def verify_challenge_code(payload: dict, request: Request) -> JSONResponse:
    phone = str(payload.get("credential") or "").strip()
    session = _session_for(request)
    if session is not None and phone:
        session["phone"] = phone
    if str(payload.get("verification_code") or "") != FIXED_CODE:
        return JSONResponse(
            status_code=400,
            content={"errors": [{"code": "invalid_verification_code"}]},
        )
    if phone and phone not in accounts_by_phone:
        return JSONResponse(status_code=404, content={"errors": [{"code": "credential_not_found"}]})
    return JSONResponse(status_code=200, content={})


@app.post("/check/v1")
def complete_session(payload: dict, request: Request) -> JSONResponse:
    redirect_uri = str(payload.get("redirect_uri") or "")
    state = str(payload.get("state") or "")
    session = _session_for(request) or {}
    phone = str(session.get("phone") or "") or DEFAULT_PHONE
    account = accounts_by_phone.get(phone)
    if account is None:
        account = accounts_by_phone[DEFAULT_PHONE]

    code = uuid.uuid4().hex
    codes[code] = {
        "identity": account.identity,
        "code_challenge": str(session.get("code_challenge") or payload.get("code_challenge") or ""),
        "redirect_uri": redirect_uri,
        "client_id": str(payload.get("client_id") or ""),
    }
    stats["codes_issued"] += 1
    separator = "&" if urlparse(redirect_uri).query else "?"
    location = f"{redirect_uri}{separator}{urlencode({'code': code, 'state': state})}"
    return JSONResponse(
        status_code=200,
        content={"requiredStates": None, "redirect_uri": location},
    )


@app.post("/token/v1")
async def token_grant(request: Request) -> JSONResponse:
    # Parsed by hand so the stub needs no extra dependency (Starlette's
    # request.form() would require python-multipart).
    raw = (await request.body()).decode("utf-8", "replace")
    form = parse_qs(raw, keep_blank_values=True)

    def field(name: str) -> str:
        values = form.get(name) or []
        return values[0] if values else ""

    grant_type = field("grant_type")

    if grant_type == "authorization_code":
        code = field("code")
        entry = codes.pop(code, None)
        if entry is None:
            return JSONResponse(status_code=400, content={"error": "invalid_grant"})
        verifier = field("code_verifier")
        expected = entry.get("code_challenge") or ""
        if expected and verifier:
            actual = _b64url(hashlib.sha256(verifier.encode()).digest())
            if actual != expected:
                return JSONResponse(status_code=400, content={"error": "invalid_grant"})
        account = accounts_by_identity.get(str(entry.get("identity") or "default"))
        if account is None:
            return JSONResponse(status_code=400, content={"error": "invalid_grant"})
        stats["token_grants"] += 1
        return JSONResponse(status_code=200, content=_issue_tokens(account))

    if grant_type == "refresh_token":
        presented = field("refresh_token")
        identity = refresh_tokens.pop(presented, None)
        if identity is None:
            return JSONResponse(status_code=400, content={"error": "invalid_grant"})
        account = accounts_by_identity.get(identity)
        if account is None:
            return JSONResponse(status_code=400, content={"error": "invalid_grant"})
        stats["refresh_grants"] += 1
        return JSONResponse(status_code=200, content=_issue_tokens(account))

    return JSONResponse(status_code=400, content={"error": "unsupported_grant_type"})


# ------------------------------------------------------------------- o2o read


@app.get("/o2o/buy/regions/v1")
def list_regions(anchor: int = 0, count: int = 200) -> dict:  # noqa: ARG001
    return {"objects": STORES}


@app.get(
    "/store/product_details/v1/marketplace/CN/language/zh-Hans/store_number/{store}/style_color/{style}"
)
def product_structure(store: str, style: str) -> dict:
    sizes = SIZES_BY_STORE.get(store.upper(), [])
    if not sizes:
        return {"productGroupings": []}
    return {
        "productGroupings": [
            {
                "products": [
                    {
                        "copy": {"fullTitle": f"离线货号 {style.upper()}"},
                        "sizes": [
                            {"localizedLabel": label, "gtins": [{"gtin": gtin}]}
                            for label, gtin in sizes
                        ],
                    }
                ]
            }
        ]
    }


@app.get(
    "/store/product_details/internal/v1/marketplace/CN/language/zh-Hans/store_number/{store}"
)
def product_structures(store: str, productCodes: str = "", fields: str = "") -> dict:  # noqa: ARG001
    """批量门店尺码表：客户端按 ≤30 货号分块调用（免 token，几乎不触发 WAF）。

    与 ``fetch_structures`` 对齐：顶层 ``products`` 数组，每项带 ``productCode`` +
    ``sizes[].localizedLabel``/``gtins[0].gtin``；响应里没有的货号由客户端按 MISSING
    处理（未知绝不当作结论），未知门店返回空数组。
    """
    sizes = SIZES_BY_STORE.get(store.upper(), [])
    codes = list(
        dict.fromkeys(code.strip().upper() for code in productCodes.split(",") if code.strip())
    )
    return {
        "products": [
            {
                "productCode": code,
                "copy": {"fullTitle": f"离线货号 {code}"},
                "sizes": [
                    {"localizedLabel": label, "gtins": [{"gtin": gtin}]}
                    for label, gtin in sizes
                ],
            }
            for code in codes
            if sizes
        ]
    }


def _waf_challenge() -> Response | None:
    """``waf`` 场景下每隔一次调用返回 405 挑战（上游抽签的确定性替身）。"""
    stats["review_calls"] += 1
    if state["scenario"] != "waf":
        return None
    if stats["review_calls"] % 2 == 0:
        stats["waf_challenges"] += 1
        return JSONResponse(status_code=405, content={"data-spm": "challenge"})
    return None


@app.put("/o2o/buy/cart_reviews/v2/{request_id}")
def submit_review(
    request_id: str, payload: dict, authorization: str = Header(default="")
) -> Response:
    _count_review(authorization)
    challenge = _waf_challenge()
    if challenge is not None:
        return challenge
    items = ((payload.get("request") or {}).get("items")) or []
    reviews[request_id] = [str(item.get("skuId") or "") for item in items]
    return {}


def _is_out_of_stock(gtin: str, account: Account | None) -> bool:
    if account is not None:
        # Scoped identity: fixed availability, independent of the scenario switch.
        return gtin.endswith("-40")
    if state["scenario"] == "restock":
        return False
    return gtin.endswith("-40")


@app.get("/o2o/buy/cart_reviews/v2/{request_id}")
def poll_review(request_id: str, authorization: str = Header(default="")) -> Response:
    _count_review(authorization)
    challenge = _waf_challenge()
    if challenge is not None:
        return challenge
    gtins = reviews.get(request_id, [])
    account = _scoped_account(authorization)
    if account is None and state["scenario"] == "untrusted":
        return {"response": {"fulfillmentGroups": []}}
    if account is None and state["scenario"] == "throttled":
        # 风控/限速：客户端退避重试耗尽后必须整轮结束并进入账号级冷却。
        return JSONResponse(status_code=403, content={"errors": [{"code": "rate_limited"}]})
    if account is not None and account.identity == "A2" and state["scenario"] == "accounts2":
        # 只打第二个 Nike 账号：账号级冷却后，同一任务必须换第一个账号继续。
        return JSONResponse(status_code=403, content={"errors": [{"code": "rate_limited"}]})

    errors = []
    for index, gtin in enumerate(gtins):
        if _is_out_of_stock(gtin, account):
            errors.append(
                {"field": f"/request/items/{index}/skuId", "code": "INVENTORY_INSUFFICIENT"}
            )
    if errors:
        return {"error": {"errors": errors}}

    price = account.price if account is not None else DEFAULT_PRICE
    if account is None and state["scenario"] == "price_up":
        # 只在货状态不变、只改价格：用于验证「变价必推送」。
        price = DEFAULT_PRICE + 100
    items = []
    for gtin in gtins:
        items.append(
            {
                "skuId": gtin,
                "itemCosts": {
                    "priceInfo": {
                        "price": price,
                        "fullPrice": 999,
                        "discount": 999 - price,
                        "total": price,
                    },
                    "promotionDiscounts": [{"displayName": "离线测试促销"}],
                },
            }
        )
    return {"response": {"fulfillmentGroups": [{"items": items}]}}
