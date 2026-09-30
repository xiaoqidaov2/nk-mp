"""Password hashing, session tokens and credential encryption."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import get_settings

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_LEN = 32


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_LEN,
    )
    salt_b64 = base64.b64encode(salt).decode()
    digest_b64 = base64.b64encode(digest).decode()
    return f"scrypt${SCRYPT_N}${salt_b64}${SCRYPT_R}${digest_b64}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, salt_b64, r, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=SCRYPT_P,
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(expected, actual)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_csrf_secret() -> str:
    return secrets.token_urlsafe(24)


def csrf_token_for(session_token: str, csrf_secret: str) -> str:
    return hmac.new(csrf_secret.encode("utf-8"), session_token.encode("utf-8"), hashlib.sha256).hexdigest()


def csrf_matches(session_token: str, csrf_secret: str, provided: str) -> bool:
    expected = csrf_token_for(session_token, csrf_secret)
    return hmac.compare_digest(expected, provided or "")


def _credential_key() -> bytes:
    raw = get_settings().credential_key.strip()
    if not raw:
        raise RuntimeError(
            "NIKE_MP_CREDENTIAL_KEY is not configured; refusing to read or write credentials"
        )
    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except Exception:  # noqa: BLE001 - fall back to hashing the raw value
        decoded = b""
    if len(decoded) == 32:
        return decoded
    return hashlib.sha256(raw.encode("utf-8")).digest()


def encrypt_secret(plaintext: str) -> str:
    if not plaintext:
        return ""
    nonce = os.urandom(12)
    sealed = AESGCM(_credential_key()).encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + sealed).decode()


def decrypt_secret(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    blob = base64.b64decode(ciphertext)
    nonce, sealed = blob[:12], blob[12:]
    return AESGCM(_credential_key()).decrypt(nonce, sealed, None).decode("utf-8")


def secret_fingerprint(value: str, *, purpose: str) -> str:
    """Purpose-scoped HMAC-SHA256 for low-entropy identifiers (phone, IP).

    A bare hash of a phone number is trivially reversible; the server key and
    the purpose label keep fingerprints isolated and non-guessable.
    """
    key = _credential_key()
    return hmac.new(key, f"{purpose}\x00{value}".encode(), hashlib.sha256).hexdigest()


def credential_key_available() -> bool:
    try:
        _credential_key()
    except RuntimeError:
        return False
    return True
