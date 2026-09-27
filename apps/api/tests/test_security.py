from __future__ import annotations

from app.security import (
    csrf_matches,
    csrf_token_for,
    decrypt_secret,
    encrypt_secret,
    hash_password,
    new_csrf_secret,
    token_fingerprint,
    verify_password,
)


def test_password_roundtrip(settings) -> None:
    stored = hash_password("a-strong-password")
    assert verify_password("a-strong-password", stored)
    assert not verify_password("wrong", stored)


def test_credential_encryption_roundtrip(settings) -> None:
    sealed = encrypt_secret("secret-token-value")
    assert sealed != "secret-token-value"
    assert decrypt_secret(sealed) == "secret-token-value"


def test_csrf_binding(settings) -> None:
    secret = new_csrf_secret()
    token = csrf_token_for("session-token", secret)
    assert csrf_matches("session-token", secret, token)
    assert not csrf_matches("other-token", secret, token)


def test_token_fingerprint_is_stable_and_opaque(settings) -> None:
    assert token_fingerprint("abc") == token_fingerprint("abc")
    assert token_fingerprint("abc") != "abc"
