"""publishing/crypto.py — tokens are encrypted at rest; URLs/state are signed."""
import base64
import hashlib
import hmac
import json
import time

from cryptography.fernet import Fernet, InvalidToken


class TokenCipher:
    def __init__(self, key: str):
        self._f = Fernet(key.encode() if isinstance(key, str) else key)

    def encrypt(self, text: str) -> str:
        return self._f.encrypt(text.encode()).decode()

    def decrypt(self, blob: str) -> str:
        try:
            return self._f.decrypt(blob.encode()).decode()
        except InvalidToken:
            raise ValueError("Cannot decrypt stored token (wrong key?)") from None


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign(secret: str, payload: dict, ttl: int, now=None) -> str:
    """Compact signed token: base64(payload+exp).hmac"""
    now = time.time() if now is None else now
    body = dict(payload, exp=int(now + ttl))
    raw = _b64(json.dumps(body, separators=(",", ":"), sort_keys=True).encode())
    mac = _b64(hmac.new(secret.encode(), raw.encode(), hashlib.sha256).digest())
    return f"{raw}.{mac}"


def verify(secret: str, token: str, now=None):
    """Return payload dict, or None if forged/expired/malformed."""
    now = time.time() if now is None else now
    try:
        raw, mac = token.split(".", 1)
        want = _b64(hmac.new(secret.encode(), raw.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(mac, want):
            return None
        body = json.loads(_unb64(raw))
        if body.get("exp", 0) < now:
            return None
        return body
    except Exception:
        return None
