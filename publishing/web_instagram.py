"""Browser Instagram OAuth linking, bound to a verified web session and one-time state."""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse
import html

from .identity import Principal
from .oauth import OAuthError

STATE_COOKIE = "__Host-vibeos_ig_state"
CSRF_COOKIE = "__Host-vibeos_csrf"
STATE_TTL_SECONDS = 600


def _now():
    return datetime.now(timezone.utc)


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _cookie(header, name):
    jar = SimpleCookie()
    try:
        jar.load(header or "")
    except Exception:
        return ""
    item = jar.get(name)
    return item.value if item else ""


def _page(title, message):
    return ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            "<title>" + html.escape(title) + "</title><main style='font-family:sans-serif;"
            "max-width:42rem;margin:4rem auto;padding:1rem'><h1>" + html.escape(title) +
            "</h1><p>" + html.escape(message) + "</p><p><a href='/web'>Return to VIBEOS</a></p></main>")


def _clear_state_cookie():
    return f"{STATE_COOKIE}=; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=0"


class WebInstagramConnect:
    """Only connects an Instagram account; it does not enable web publishing."""

    def __init__(self, cfg, oauth, accounts, sessions, transactions, clock=_now):
        self.cfg = cfg
        self.oauth = oauth
        self.accounts = accounts
        self.sessions = sessions
        self.transactions = transactions
        self.clock = clock
        self.origin = cfg.public_base_url.rstrip("/")
        self.callback_uri = self.origin + "/web/instagram/callback"

    def ensure_indexes(self):
        self.transactions.create_index("state_hash", unique=True)
        self.transactions.create_index("expires_at", expireAfterSeconds=0)

    def start(self, cookie_header, form, origin):
        session_token = _cookie(cookie_header, "__Host-vibeos_session")
        csrf_cookie = _cookie(cookie_header, CSRF_COOKIE)
        csrf_form = (form.get("csrf_token") or [""])[0]
        principal = self.sessions.resolve(session_token)
        why = []
        if origin != self.origin:
            why.append("origin=%r expected=%r" % (origin, self.origin))
        if not session_token:
            why.append("no session cookie")
        elif not principal:
            why.append("session not resolved")
        if not csrf_cookie:
            why.append("no csrf cookie")
        if not csrf_form:
            why.append("no csrf form field")
        if csrf_cookie and csrf_form and not hmac.compare_digest(csrf_cookie, csrf_form):
            why.append("csrf cookie != form")
        try:
            if principal and csrf_form and not self.sessions.validate_csrf(session_token, csrf_form):
                why.append("validate_csrf False")
        except Exception as exc:
            why.append("validate_csrf error %r" % (exc,))
        if why:
            import logging
            logging.getLogger(__name__).warning("IG CONNECT REJECTED: %s", "; ".join(why))
            return 403, _page("Request rejected", "DEBUG: " + "; ".join(why)), {}
        state = secrets.token_urlsafe(32)
        now = self.clock()
        self.transactions.insert_one({
            "state_hash": _digest(state),
            "owner_id": principal.owner_id,
            "created_at": now,
            "expires_at": now + timedelta(seconds=STATE_TTL_SECONDS),
        })
        try:
            url = self.oauth.web_authorize_url(state)
        except Exception:
            # Consume the transaction if provider URL construction fails.
            self.transactions.delete_one({"state_hash": _digest(state)})
            return 503, _page("Instagram unavailable", "Please try again later."), {}
        cookie = f"{STATE_COOKIE}={state}; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age={STATE_TTL_SECONDS}"
        return 303, "", {"Location": url, "Set-Cookie": cookie}

    def callback(self, query, cookie_header):
        first = lambda key: (query.get(key) or [""])[0]
        state = first("state")
        state_cookie = _cookie(cookie_header, STATE_COOKIE)
        if not state or not state_cookie or not hmac.compare_digest(state, state_cookie):
            return 400, _page("Connection failed", "Please start Instagram connect again."), {
                "Set-Cookie": _clear_state_cookie()
            }
        transaction = self.transactions.find_one_and_delete({
            "state_hash": _digest(state),
            "expires_at": {"$gt": self.clock()},
        })
        if not transaction:
            return 400, _page("Connection expired", "Please start Instagram connect again."), {
                "Set-Cookie": _clear_state_cookie()
            }
        principal = self.sessions.resolve(_cookie(cookie_header, "__Host-vibeos_session"))
        if not principal or principal.owner_id != transaction.get("owner_id"):
            return 403, _page("Session expired", "Sign in and start Instagram connect again."), {
                "Set-Cookie": _clear_state_cookie()
            }
        if first("error"):
            return 400, _page("Connection cancelled", "You can return to VIBEOS."), {
                "Set-Cookie": _clear_state_cookie()
            }
        try:
            token_info = self.oauth.exchange_code(first("code"), redirect_uri=self.callback_uri)
            account = self.accounts.connect_for(principal, token_info)
        except (OAuthError, ValueError):
            return 400, _page("Couldn't connect Instagram", "Check the account permissions and try again."), {
                "Set-Cookie": _clear_state_cookie()
            }
        except Exception:
            return 502, _page("Couldn't connect Instagram", "Please try again later."), {
                "Set-Cookie": _clear_state_cookie()
            }
        username = account.get("username") or "Instagram account"
        return 200, _page("Instagram connected", "@" + str(username) + " is now linked to your VIBEOS account."), {
            "Set-Cookie": _clear_state_cookie()
        }
