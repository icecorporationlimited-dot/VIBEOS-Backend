"""
publishing/server.py — tiny stdlib HTTPS-behind-proxy web server (own thread).

  GET /oauth/callback?code&state   Instagram redirects here after login
  GET /media/<signed-token>.jpg    Instagram fetches publish-ready JPEGs here
  GET /healthz                     liveness probe

Media URLs are signed + expire (default 1h) and only expose blobs we sign,
so nobody can enumerate your photo library.
"""
import html
import logging
from pathlib import Path
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from .web_routes import WebLoginRoutes

from . import crypto
from .oauth import OAuthError

logger = logging.getLogger(__name__)

PAGE = ("<!doctype html><meta name=viewport content='width=device-width'>"
        "<body style='font-family:sans-serif;text-align:center;padding:3rem'>"
        "<h2>{title}</h2><p>{body}</p></body>")


def _render_page(title, body):
    """Escape every dynamic value before inserting it into the HTML response."""
    return PAGE.format(title=html.escape(str(title)), body=html.escape(str(body)))


def _web_media_response(web_login, content_buffer, cookie_header, asset_id, variant):
    """Return owner-scoped browser media only for a valid web session."""
    if web_login is None or content_buffer is None:
        return 404, b"not found", "text/plain"
    jar = SimpleCookie()
    try:
        jar.load(cookie_header or "")
    except Exception:
        return 404, b"not found", "text/plain"
    session_cookie = jar.get("__Host-vibeos_session")
    principal = web_login.sessions.resolve(session_cookie.value if session_cookie else "")
    if principal is None or variant not in {"original", "post", "story", "reel"}:
        return 404, b"not found", "text/plain"
    try:
        data = content_buffer.get_media_for(
            principal, asset_id, None if variant == "original" else variant
        )
    except Exception:
        # Keep missing, cross-tenant, and unavailable assets indistinguishable.
        return 404, b"not found", "text/plain"
    return 200, data, "video/mp4" if variant == "reel" else "image/jpeg"


def make_handler(cfg, oauth, accounts, blobs, notify, web_login=None, content_buffer=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):   # keep tokens/codes out of logs
            logger.info("http %s %s", self.command, urlparse(self.path).path)

        def _send(self, status, body, ctype="text/html; charset=utf-8", extra=None):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; "
                "base-uri 'none'; frame-ancestors 'none'; "
                "form-action 'self' https://www.instagram.com https://api.instagram.com "
                "https://www.facebook.com https://m.facebook.com",
            )
            for k, v in (extra or {}).items():
                self.send_header("Set-Cookie" if k.startswith("Set-Cookie") else k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def do_HEAD(self):
            # Never route HEAD through GET: OAuth callbacks have side effects.
            self._send(405, "method not allowed", "text/plain", {"Allow": "GET"})

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/healthz":
                return self._send(200, "ok", "text/plain")
            if url.path in ("/privacy-policy", "/privacy-policy/"):
                # Serve the repository-root policy page on the same Render domain.
                policy_path = Path(__file__).resolve().parent.parent / "privacy-policy.html"
                try:
                    policy_html = policy_path.read_text(encoding="utf-8")
                except OSError:
                    return self._send(404, "Privacy policy not found", "text/plain")
                return self._send(200, policy_html, "text/html; charset=utf-8")
            if web_login is not None and url.path == "/login":
                status, body, headers = web_login.login()
                return self._send(status, body, extra=headers)
            if web_login is not None and url.path == "/web/api/me":
                status, body, headers = web_login.me(self.headers.get("Cookie", ""))
                return self._send(status, body, "application/json", extra=headers)
            if web_login is not None and url.path == "/web":
                status, body, headers = web_login.home(self.headers.get("Cookie", ""))
                return self._send(status, body, extra=headers)
            if url.path.startswith("/web/media/"):
                parts = url.path.split("/")
                if len(parts) != 5 or not parts[3] or not parts[4]:
                    return self._send(404, "not found", "text/plain")
                status, data, ctype = _web_media_response(
                    web_login, content_buffer, self.headers.get("Cookie", ""),
                    parts[3], parts[4])
                return self._send(status, data, ctype)
            if web_login is not None and url.path == "/oidc/callback":
                status, body, headers = web_login.callback(parse_qs(url.query), self.headers.get("Cookie", ""))
                return self._send(status, body, extra=headers)
            if web_login is not None and url.path == "/web/instagram/callback":
                status, body, headers = web_login.instagram_callback(parse_qs(url.query), self.headers.get("Cookie", ""))
                return self._send(status, body, extra=headers)
            if url.path == "/oauth/callback":
                return self._oauth(parse_qs(url.query))
            if url.path.startswith("/media/") and url.path.endswith(".jpg"):
                return self._media(url.path[len("/media/"):-len(".jpg")], "image/jpeg")
            if url.path.startswith("/media/") and url.path.endswith(".mp4"):
                return self._media(url.path[len("/media/"):-len(".mp4")], "video/mp4")
            self._send(404, "not found", "text/plain")

        def do_POST(self):
            url = urlparse(self.path)
            if web_login is None or url.path not in ("/logout", "/web/instagram/connect"):
                return self._send(404, "not found", "text/plain")
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/x-www-form-urlencoded":
                return self._send(415, "unsupported media type", "text/plain")
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._send(400, "bad request", "text/plain")
            if size < 0 or size > 8192:
                return self._send(413, "request too large", "text/plain")
            raw = self.rfile.read(size).decode("utf-8", "replace")
            form = parse_qs(raw)
            if url.path == "/web/instagram/connect":
                status, body, headers = web_login.instagram_start(
                    self.headers.get("Cookie", ""), form, self.headers.get("Origin", ""))
            else:
                status, body, headers = web_login.logout(
                    self.headers.get("Cookie", ""), form, self.headers.get("Origin", ""))
            return self._send(status, body, extra=headers)

        def _oauth(self, q):
            first = lambda k: (q.get(k) or [""])[0]
            if first("error"):
                return self._send(400, _render_page(
                    "Connection cancelled",
                    "You can close this tab and run /connect again."))
            try:
                tg_id = oauth.telegram_id_from_state(first("state"))
                token_info = oauth.exchange_code(first("code"))
                acct = accounts.connect(tg_id, token_info)
            except OAuthError as exc:
                return self._send(400, _render_page("Couldn't connect", str(exc)))
            except Exception:
                logger.exception("OAuth callback failed")
                return self._send(500, _render_page(
                    "Something went wrong", "Please run /connect again."))
            try:
                notify(tg_id, acct)
            except Exception:
                logger.exception("Could not notify Telegram user")
            self._send(200, _render_page(
                "Instagram connected",
                f"@{acct.get('username')} is linked. Go back to Telegram."))

        def _range(self, size):
            """Parse a single 'bytes=a-b' range; None = whole file, False = unsatisfiable."""
            header = self.headers.get("Range") or ""
            if not header.startswith("bytes=") or "," in header:
                return None
            first, _, last = header[6:].partition("-")
            try:
                if first == "":
                    n = int(last)
                    start, end = max(size - n, 0), size - 1
                else:
                    start = int(first)
                    end = min(int(last), size - 1) if last else size - 1
            except ValueError:
                return None
            if size == 0 or start > end or start >= size:
                return False
            return start, end

        def _media(self, token, ctype="image/jpeg"):
            body = crypto.verify(cfg.signing_secret, token)
            if not body or "b" not in body:
                return self._send(404, "not found", "text/plain")
            try:
                data = blobs.get(body["b"])
            except Exception:
                return self._send(404, "not found", "text/plain")
            rng = self._range(len(data))
            if rng is False:
                return self._send(416, "range not satisfiable", "text/plain",
                                  {"Content-Range": f"bytes */{len(data)}"})
            if rng:
                start, end = rng
                return self._send(206, data[start:end + 1], ctype, {
                    "Accept-Ranges": "bytes",
                    "Content-Range": f"bytes {start}-{end}/{len(data)}"})
            self._send(200, data, ctype, {"Accept-Ranges": "bytes"})

    return Handler


def start_server(cfg, oauth, accounts, blobs, notify, web_login=None, content_buffer=None):
    httpd = ThreadingHTTPServer(("0.0.0.0", cfg.http_port),
                                make_handler(cfg, oauth, accounts, blobs, notify, web_login=web_login, content_buffer=content_buffer))
    thread = threading.Thread(target=httpd.serve_forever, name="vibeos-http",
                              daemon=True)
    thread.start()
    logger.info("Publishing web server on :%s", cfg.http_port)
    return httpd
