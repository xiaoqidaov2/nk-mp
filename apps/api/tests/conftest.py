from __future__ import annotations

import base64
import json
import os
import time

# The settings object is cached at import time, so the key must exist before
# any application module is imported.
os.environ.setdefault("NIKE_MP_CREDENTIAL_KEY", "test-key-for-unit-tests")

# The test client only ever speaks plain http: a Secure session cookie would
# silently never be sent back, so the suite must run with insecure cookies.
os.environ["NIKE_MP_COOKIE_SECURE"] = "false"

# Integration tests only run against an explicitly provided scratch database.
# The runtime database URL is never silently reused for tests.
if os.environ.get("NIKE_MP_TEST_DATABASE_URL"):
    os.environ["NIKE_MP_DATABASE_URL"] = os.environ["NIKE_MP_TEST_DATABASE_URL"]

import pytest  # noqa: E402

from app.config import Settings, get_settings  # noqa: E402


def make_jwt(exp_offset: int = 3600) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).decode().rstrip("=")
    payload = (
        base64.urlsafe_b64encode(
            json.dumps({"exp": int(time.time()) + exp_offset, "prn": "test"}).encode()
        )
        .decode()
        .rstrip("=")
    )
    return f"{header}.{payload}.sig"


@pytest.fixture(autouse=True)
def _fresh_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        credential_key="test-key-for-unit-tests",
        availability_chunk_size=4,
        price_chunk_size=3,
        review_poll_tries=2,
        review_poll_interval_seconds=0.0,
        request_retry_limit=2,
        request_pace_seconds=0.0,
    )