"""
publishing/oauth.py — Instagram Login (Business Login for Instagram).

Flow: /connect -> user opens authorize_url -> Instagram redirects to
/oauth/callback?code&state -> short-lived token -> long-lived token (~60 days)
-> store encrypted. `state` is a signed, expiring token binding the callback to
the Telegram user who started it (CSRF protection).
"""
from urllib.parse import urlencode

from . import crypto
from .graph import GraphClient, GraphError

AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
TOKEN_URL = "https://api.instagram.com/oauth/access_token"
GRAPH_HOST = "https://graph.instagram.com"
STATE_TTL = 600


class OAuthError(Exception):
    pass


class InstagramOAuth:
    def __init__(self, cfg, http):
        self.cfg = cfg
        self._client = GraphClient(http, cfg.graph_version)

    def web_authorize_url(self, state: str) -> str:
        """Build a browser connect URL using a server-stored one-time state."""
        if not isinstance(state, str) or not state or len(state) > 256:
            raise OAuthError("Invalid browser OAuth state.")
        return AUTHORIZE_URL + "?" + urlencode({
            "client_id": self.cfg.app_id,
            "redirect_uri": self.cfg.public_base_url.rstrip("/") + "/web/instagram/callback",
            "response_type": "code",
            "scope": ",".join(self.cfg.scopes),
            "state": state,
        })

    def authorize_url(self, telegram_id, now=None) -> str:
        state = crypto.sign(self.cfg.signing_secret,
                            {"tg": str(telegram_id), "p": "ig_oauth"},
                            STATE_TTL, now=now)
        return AUTHORIZE_URL + "?" + urlencode({
            "client_id": self.cfg.app_id,
            "redirect_uri": self.cfg.redirect_uri,
            "response_type": "code",
            "scope": ",".join(self.cfg.scopes),
            "state": state,
        })

    def telegram_id_from_state(self, state, now=None):
        body = crypto.verify(self.cfg.signing_secret, state or "", now=now)
        if not body or body.get("p") != "ig_oauth":
            raise OAuthError("Invalid or expired link. Run /connect again.")
        return body["tg"]

    def exchange_code(self, code: str, redirect_uri=None) -> dict:
        """code -> long-lived token. Returns {access_token, expires_in}."""
        code = (code or "").split("#")[0]
        if not code:
            raise OAuthError("Missing authorization code.")
        try:
            short = self._client._call("POST", TOKEN_URL, {
                "client_id": self.cfg.app_id,
                "client_secret": self.cfg.app_secret,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri or self.cfg.redirect_uri,
                "code": code,
            })
            if "data" in short:          # older response shape
                short = short["data"][0]
            long_ = self._client._call("GET", f"{GRAPH_HOST}/access_token", {
                "grant_type": "ig_exchange_token",
                "client_secret": self.cfg.app_secret,
                "access_token": short["access_token"],
            })
        except GraphError as exc:
            raise OAuthError(f"Instagram rejected the login: {exc}") from None

        granted = set(short.get("permissions") or [])
        if isinstance(short.get("permissions"), str):
            granted = set(short["permissions"].split(","))
        missing = set(self.cfg.scopes) - granted if granted else set()
        if missing:
            raise OAuthError(
                "Publishing permission was not granted "
                f"({', '.join(sorted(missing))}). Run /connect and keep all "
                "permissions ticked."
            )
        return {"access_token": long_["access_token"],
                "expires_in": int(long_.get("expires_in", 60 * 24 * 3600)),
                "scopes": sorted(granted) or list(self.cfg.scopes)}

    def refresh(self, token: str) -> dict:
        body = self._client._call("GET", f"{GRAPH_HOST}/refresh_access_token", {
            "grant_type": "ig_refresh_token", "access_token": token})
        return {"access_token": body["access_token"],
                "expires_in": int(body.get("expires_in", 60 * 24 * 3600))}
