"""Server-side web sessions for VIBEOS.

This module manages sessions only; it does not authenticate a user. Call
create() only after a trusted authentication adapter has validated the user's
identity (including signature, issuer, audience, expiry and nonce/state).
"""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from .identity import IdentityError, Principal

from bson import ObjectId


def _id_candidates(value):
    out = [value]
    try:
        out.append(ObjectId(value))
    except Exception:
        pass
    return out

SESSION_COOKIE = "__Host-vibeos_session"
DEFAULT_TTL_SECONDS = 60 * 60 * 8
MAX_TTL_SECONDS = 60 * 60 * 24


def _now():
    return datetime.now(timezone.utc)

def _aware(dt):
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def session_cookie(token, max_age=DEFAULT_TTL_SECONDS):
    """Return a production-only secure cookie header value."""
    if not token or any(c in str(token) for c in "\r\n;"):
        raise ValueError("Invalid session token.")
    age = max(0, min(int(max_age), MAX_TTL_SECONDS))
    return (
        f"{SESSION_COOKIE}={token}; Path=/; Secure; HttpOnly; "
        f"SameSite=Lax; Max-Age={age}"
    )


def clear_session_cookie():
    return (
        f"{SESSION_COOKIE}=; Path=/; Secure; HttpOnly; SameSite=Lax; "
        "Max-Age=0"
    )


class WebSessionStore:
    """Mongo-backed, revocable sessions with hashed bearer tokens."""

    def __init__(self, sessions, users, ttl_seconds=DEFAULT_TTL_SECONDS, clock=_now):
        self.sessions = sessions
        self.users = users
        self.ttl_seconds = int(ttl_seconds)
        self.clock = clock
        if not 1 <= self.ttl_seconds <= MAX_TTL_SECONDS:
            raise ValueError("Session lifetime must be between 1 second and 24 hours.")

    def ensure_indexes(self):
        self.sessions.create_index("token_hash", unique=True)
        self.sessions.create_index("expires_at", expireAfterSeconds=0)
        self.sessions.create_index([("user_id", 1), ("expires_at", 1)])

    def create(self, verified_subject):
        """Issue a session for an already verified, stable auth-provider subject.

        The subject must come from a trusted authentication adapter, never from
        a query parameter, form field, or unverified JWT payload. The matching
        user record must already exist and be enabled.
        """
        principal = Principal.from_web(verified_subject)
        user = self.users.find_one({
            "auth_subject": principal.subject,
            "disabled": {"$ne": True},
        })
        if not user:
            raise IdentityError("Verified identity is not linked to an active user.")
        token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        now = self.clock()
        expires = now + timedelta(seconds=self.ttl_seconds)
        self.sessions.insert_one({
            "token_hash": _digest(token),
            "user_id": str(user["_id"]),
            "csrf_hash": _digest(csrf_token),
            "created_at": now,
            "expires_at": expires,
        })
        return {"token": token, "csrf_token": csrf_token,
                "principal": Principal.from_web(str(user["_id"])),
                "expires_at": expires}

    def resolve(self, token):
        """Return the session principal, or None for invalid/expired/revoked sessions."""
        if not isinstance(token, str) or not token or len(token) > 256:
            return None
        session = self.sessions.find_one({"token_hash": _digest(token)})
        if not session:
            return None
        now = self.clock()
        expires = _aware(session.get("expires_at"))
        if not expires or expires <= now:
            self.sessions.delete_one({"token_hash": _digest(token)})
            return None
        user = self.users.find_one({
            "_id": {"$in": _id_candidates(session.get("user_id"))},
            "disabled": {"$ne": True},
        })
        if not user:
            self.sessions.delete_one({"token_hash": _digest(token)})
            return None
        try:
            return Principal.from_web(str(user["_id"]))
        except (KeyError, IdentityError):
            return None

    def validate_csrf(self, token, csrf_token):
        """Validate a CSRF token for a live session using constant-time comparison."""
        if (not isinstance(token, str) or not token or
                not isinstance(csrf_token, str) or not csrf_token):
            return False
        session = self.sessions.find_one({"token_hash": _digest(token)})
        if not session or _aware(session.get("expires_at")) <= self.clock():
            return False
        user = self.users.find_one({
            "_id": {"$in": _id_candidates(session.get("user_id"))},
            "disabled": {"$ne": True},
        })
        if not user:
            return False
        return hmac.compare_digest(session.get("csrf_hash", ""), _digest(csrf_token))

    def revoke(self, token):
        """Revoke one session; safe to call repeatedly."""
        if isinstance(token, str) and token and len(token) <= 256:
            self.sessions.delete_one({"token_hash": _digest(token)})
