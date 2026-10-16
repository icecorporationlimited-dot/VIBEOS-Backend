"""OpenID Connect Authorization Code flow with PKCE.

This module verifies identity-provider responses; it deliberately does not
create browser routes or issue app sessions. A web adapter should call begin()
from a login endpoint and complete() only from the registered callback route.
Never link accounts by email alone.
"""
import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse

import requests


MAX_TRANSACTION_AGE_SECONDS = 600
ALLOWED_ID_TOKEN_ALGORITHMS = ("RS256", "ES256")


class OIDCError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc)


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _https_url(value):
    parsed = urlparse(value or "")
    return parsed.scheme == "https" and bool(parsed.netloc) and not parsed.username and not parsed.password


class OIDCLogin:
    """OIDC login coordinator backed by Mongo one-time login transactions."""

    def __init__(self, issuer, client_id, client_secret, redirect_uri,
                 transactions, users, http=None, clock=_now):
        if not _https_url(issuer) or not _https_url(redirect_uri):
            raise OIDCError("OIDC issuer and redirect URI must use HTTPS.")
        if not client_id or not client_secret:
            raise OIDCError("OIDC client credentials are required.")
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.transactions = transactions
        self.users = users
        self.http = http or requests.Session()
        self.clock = clock

    def ensure_indexes(self):
        self.transactions.create_index("state_hash", unique=True)
        self.transactions.create_index("expires_at", expireAfterSeconds=0)
        self.users.create_index("auth_subject", unique=True, sparse=True)

    def _discovery(self):
        response = self.http.get(
            self.issuer + "/.well-known/openid-configuration", timeout=10
        )
        response.raise_for_status()
        metadata = response.json()
        if metadata.get("issuer", "").rstrip("/") != self.issuer:
            raise OIDCError("OIDC discovery issuer does not match configured issuer.")
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not _https_url(metadata.get(key, "")):
                raise OIDCError("OIDC provider returned an invalid HTTPS endpoint.")
        return metadata

    def begin(self):
        """Create a one-time login transaction and return the provider URL."""
        metadata = self._discovery()
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        now = self.clock()
        self.transactions.insert_one({
            "state_hash": _digest(state),
            "nonce": nonce,
            "code_verifier": verifier,
            "created_at": now,
            "expires_at": now + timedelta(seconds=MAX_TRANSACTION_AGE_SECONDS),
        })
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": "openid profile email",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return metadata["authorization_endpoint"] + "?" + urlencode(params)

    def complete(self, state, code):
        """Consume state once, exchange code, verify ID token, and provision a user.

        Returns a stable issuer-qualified subject suitable for WebSessionStore.create().
        User identity is keyed by (issuer, sub); email is profile data, never a link key.
        """
        if not isinstance(state, str) or not state or len(state) > 256:
            raise OIDCError("Invalid or expired login state.")
        if not isinstance(code, str) or not code or len(code) > 4096:
            raise OIDCError("Missing or invalid authorization code.")
        transaction = self.transactions.find_one_and_delete({
            "state_hash": _digest(state),
            "expires_at": {"$gt": self.clock()},
        })
        if not transaction:
            raise OIDCError("Invalid, expired, or already used login state.")

        metadata = self._discovery()
        response = self.http.post(metadata["token_endpoint"], data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "code_verifier": transaction["code_verifier"],
        }, auth=(self.client_id, self.client_secret), timeout=15)
        response.raise_for_status()
        token_response = response.json()
        encoded = token_response.get("id_token")
        if not encoded:
            raise OIDCError("OIDC provider did not return an ID token.")

        try:
            import jwt
            jwks = jwt.PyJWKClient(metadata["jwks_uri"], cache_keys=True)
            signing_key = jwks.get_signing_key_from_jwt(encoded).key
            claims = jwt.decode(
                encoded,
                signing_key,
                algorithms=list(ALLOWED_ID_TOKEN_ALGORITHMS),
                audience=self.client_id,
                issuer=metadata["issuer"],
                options={"require": ["sub", "exp", "iat", "iss", "aud", "nonce"]},
                leeway=30,
            )
        except Exception as exc:
            raise OIDCError("OIDC ID token validation failed.") from None

        nonce = claims.get("nonce", "")
        if not isinstance(nonce, str) or not hmac.compare_digest(nonce, transaction["nonce"]):
            raise OIDCError("OIDC nonce validation failed.")
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub.strip() or len(sub) > 256:
            raise OIDCError("OIDC subject is invalid.")
        auth_subject = self.issuer + "|" + sub
        now = self.clock()
        profile = {
            "auth_subject": auth_subject,
            "auth_issuer": self.issuer,
            "auth_provider": "oidc",
            "updated_at": now,
        }
        if isinstance(claims.get("email"), str):
            profile["email"] = claims["email"][:320]
            profile["email_verified"] = claims.get("email_verified") is True
        if isinstance(claims.get("name"), str):
            profile["display_name"] = claims["name"][:200]

        self.users.update_one(
            {"auth_subject": auth_subject},
            {"$set": profile, "$setOnInsert": {"created_at": now, "disabled": False}},
            upsert=True,
        )
        return auth_subject
