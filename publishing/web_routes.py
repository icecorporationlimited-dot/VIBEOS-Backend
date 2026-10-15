"""Browser login route adapter for the existing stdlib publishing server."""
import html
import hmac
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse

from .oidc import OIDCError
from .web_auth import (
    SESSION_COOKIE, WebSessionStore, clear_session_cookie, session_cookie,
)

CSRF_COOKIE = "__Host-vibeos_csrf"
OIDC_STATE_COOKIE = "vibeos_oidc_state"


def _cookie_value(header, name):
    jar = SimpleCookie()
    try:
        jar.load(header or "")
    except Exception:
        return ""
    morsel = jar.get(name)
    return morsel.value if morsel else ""


def _csrf_cookie(token, max_age=8 * 60 * 60):
    if not token or any(c in token for c in "\r\n;"):
        raise ValueError("Invalid CSRF token.")
    return f"{CSRF_COOKIE}={token}; Path=/; Secure; SameSite=Strict; Max-Age={max_age}"


def _clear_csrf_cookie():
    return f"{CSRF_COOKIE}=; Path=/; Secure; SameSite=Strict; Max-Age=0"


def _clear_state_cookie():
    return f"{OIDC_STATE_COOKIE}=; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=0"


def _page(title, body):
    return (
        "<!doctype html><html><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width'>"
        "<title>" + html.escape(title) + "</title></head>"
        "<body style='font-family:sans-serif;max-width:42rem;margin:4rem auto;padding:1rem'>"
        "<h1>" + html.escape(title) + "</h1>" + body + "</body></html>"
    )


class WebLoginRoutes:
    """Small route service; only this adapter may issue browser sessions."""

    def __init__(self, cfg, oidc, sessions):
        self.cfg, self.oidc, self.sessions = cfg, oidc, sessions
        self.origin = cfg.public_base_url.rstrip("/")
        self.callback_path = "/oidc/callback"
        self.login_path = "/login"
        self.web_path = "/web"

    def login(self):
        try:
            url = self.oidc.begin()
            state = (parse_qs(urlparse(url).query).get("state") or [""])[0]
            if not state:
                raise OIDCError("OIDC URL missing state.")
            cookie = f"{OIDC_STATE_COOKIE}={state}; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=600"
            return 302, "", {"Location": url, "Set-Cookie-2": cookie}
        except Exception:
            return 503, _page("Login unavailable", "<p>Please try again later.</p>"), {}

    def callback(self, query):
        first = lambda key: (query.get(key) or [""])[0]
        if first("error"):
            return 400, _page("Sign-in cancelled", "<p>You can close this tab.</p>"), {}
        try:
            subject = self.oidc.complete(first("state"), first("code"))
            issued = self.sessions.create(subject)
            headers = {
                "Location": self.web_path,
                "Set-Cookie": session_cookie(issued["token"]),
                "Set-Cookie-2": _csrf_cookie(issued["csrf_token"]),
            }
            return 303, "", headers
        except (OIDCError, ValueError):
            return 400, _page("Sign-in failed", "<p>Please start again from the login page.</p>"), {}
        except Exception:
            return 500, _page("Sign-in failed", "<p>Please try again later.</p>"), {}

    def home(self, cookie_header):
        token = _cookie_value(cookie_header, SESSION_COOKIE)
        principal = self.sessions.resolve(token)
        if not principal:
            return 303, "", {"Location": self.login_path}
        csrf = _cookie_value(cookie_header, CSRF_COOKIE)
        if not csrf:
            return 403, _page("Session needs refreshing", "<p>Please sign in again.</p>"), {}
        body = (
            "<p>You are signed in.</p>"
            "<form method=post action='/logout'>"
            "<input type=hidden name=csrf_token value='" + html.escape(csrf, quote=True) + "'>"
            "<button type=submit>Sign out</button></form>"
        )
        return 200, _page("VIBEOS", body), {}

    def logout(self, cookie_header, form, origin):
        token = _cookie_value(cookie_header, SESSION_COOKIE)
        csrf_cookie = _cookie_value(cookie_header, CSRF_COOKIE)
        csrf_form = (form.get("csrf_token") or [""])[0]
        if origin != self.origin or not csrf_cookie or not csrf_form:
            return 403, _page("Request rejected", "<p>Please return to the app and try again.</p>"), {}
        if not hmac.compare_digest(csrf_cookie, csrf_form):
            return 403, _page("Request rejected", "<p>Please return to the app and try again.</p>"), {}
        if not self.sessions.validate_csrf(token, csrf_form):
            return 403, _page("Request rejected", "<p>Please sign in again.</p>"), {}
        self.sessions.revoke(token)
        return 303, "", {
            "Location": self.login_path,
            "Set-Cookie": clear_session_cookie(),
            "Set-Cookie-2": _clear_csrf_cookie(),
        }
