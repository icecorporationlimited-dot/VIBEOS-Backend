"""Browser login route adapter for the existing stdlib publishing server."""
import html
import hmac
import json
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse

from .oidc import OIDCError
from .web_auth import (
    SESSION_COOKIE, WebSessionStore, clear_session_cookie, find_user,
    session_cookie,
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
    return f"{CSRF_COOKIE}={token}; Path=/; Secure; SameSite=Lax; Max-Age={max_age}"


def _clear_csrf_cookie():
    return f"{CSRF_COOKIE}=; Path=/; Secure; SameSite=Lax; Max-Age=0"


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

    def __init__(self, cfg, oidc, sessions, instagram_connect=None, accounts=None):
        self.cfg, self.oidc, self.sessions = cfg, oidc, sessions
        self.instagram_connect, self.accounts = instagram_connect, accounts
        self.origin = cfg.public_base_url.rstrip("/")
        self.callback_path = "/oidc/callback"
        self.login_path = "/login"
        self.web_path = "/web"
        self.app_path = "/app/"

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

    def callback(self, query, cookie_header=""):
        first = lambda key: (query.get(key) or [""])[0]
        state = first("state")
        state_cookie = _cookie_value(cookie_header, OIDC_STATE_COOKIE)
        if first("error"):
            return 400, _page("Sign-in cancelled", "<p>You can close this tab.</p>"), {"Set-Cookie-3": _clear_state_cookie()}
        if not state or not state_cookie or not hmac.compare_digest(state, state_cookie):
            return 400, _page("Sign-in failed", "<p>Please start again from the login page.</p>"), {"Set-Cookie-3": _clear_state_cookie()}
        try:
            subject = self.oidc.complete(state, first("code"))
            issued = self.sessions.create(subject)
            headers = {
                "Location": self.app_path,
                "Set-Cookie": session_cookie(issued["token"]),
                "Set-Cookie-2": _csrf_cookie(issued["csrf_token"]),
                "Set-Cookie-3": _clear_state_cookie(),
            }
            return 303, "", headers
        except (OIDCError, ValueError):
            return 400, _page("Sign-in failed", "<p>Please start again from the login page.</p>"), {"Set-Cookie-3": _clear_state_cookie()}
        except Exception:
            return 500, _page("Sign-in failed", "<p>Please try again later.</p>"), {"Set-Cookie-3": _clear_state_cookie()}

    def me(self, cookie_header):
        """JSON status for the hosted web app (same-origin only, never cached)."""
        token = _cookie_value(cookie_header, SESSION_COOKIE)
        principal = self.sessions.resolve(token)
        if not principal:
            return 200, json.dumps({"signed_in": False}), {}
        name = "Creator"
        try:
            user = find_user(self.sessions.users, principal.subject) or {}
            email = str(user.get("email") or "")
            name = (str(user.get("display_name") or "").strip()
                    or email.split("@")[0] or "Creator")[:60]
        except Exception:
            pass
        username, connected, status = "", False, ""
        if self.accounts is not None:
            try:
                account = self.accounts.get_for(principal)
            except Exception:
                account = None
            if account:
                status = str(account.get("status") or "")
                connected = status == "active"
                username = str(account.get("username") or "")
        return 200, json.dumps({
            "signed_in": True,
            "name": name,
            "connected": connected,
            "username": username,
            "account_status": status,
            "csrf": _cookie_value(cookie_header, CSRF_COOKIE),
        }), {}

    def home(self, cookie_header):
        token = _cookie_value(cookie_header, SESSION_COOKIE)
        principal = self.sessions.resolve(token)
        if not principal:
            return 303, "", {"Location": self.login_path}
        csrf = _cookie_value(cookie_header, CSRF_COOKIE)
        if not csrf:
            return 403, _page("Session needs refreshing", "<p>Please sign in again.</p>"), {}
        body = "<p>You are signed in.</p>"
        if self.instagram_connect is not None and self.accounts is not None:
            account = self.accounts.get_for(principal)
            if account and account.get("status") == "active":
                body += "<p>Instagram connected: <strong>@" + html.escape(str(account.get("username") or "account")) + "</strong></p>"
            else:
                body += (
                    "<p>Connect your Instagram professional account to VIBEOS.</p>"
                    "<form method=post action='/web/instagram/connect'>"
                    "<input type=hidden name=csrf_token value='" + html.escape(csrf, quote=True) + "'>"
                    "<button type=submit>Connect Instagram</button></form>"
                )
        body += (
            "<form method=post action='/logout'>"
            "<input type=hidden name=csrf_token value='" + html.escape(csrf, quote=True) + "'>"
            "<button type=submit>Sign out</button></form>"
        )
        return 200, _page("VIBEOS", body), {}


    def instagram_start(self, cookie_header, form, origin):
        if self.instagram_connect is None:
            return 404, _page("Instagram connect unavailable", "<p>Please try again later.</p>"), {}
        return self.instagram_connect.start(cookie_header, form, origin)

    def instagram_callback(self, query, cookie_header):
        if self.instagram_connect is None:
            return 404, _page("Instagram connect unavailable", "<p>Please try again later.</p>"), {}
        return self.instagram_connect.callback(query, cookie_header)

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
