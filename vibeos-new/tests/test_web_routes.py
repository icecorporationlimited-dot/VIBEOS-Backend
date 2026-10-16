import unittest
from urllib.parse import parse_qs, urlparse

from publishing.web_routes import (
    CSRF_COOKIE, OIDC_STATE_COOKIE, WebLoginRoutes,
)


class FakeOIDC:
    def __init__(self):
        self.calls = []
        self.state = "browser-bound-state"

    def begin(self):
        return "https://id.example.test/auth?" + "state=" + self.state

    def complete(self, state, code):
        self.calls.append((state, code))
        return "issuer|subject"


class FakeSessions:
    def __init__(self):
        self.revoked = []
        self.csrf_valid = True

    def create(self, subject):
        return {"token": "session-token", "csrf_token": "csrf-token"}

    def resolve(self, token):
        return object() if token == "session-token" else None

    def validate_csrf(self, token, csrf):
        return self.csrf_valid and token == "session-token" and csrf == "csrf-token"

    def revoke(self, token):
        self.revoked.append(token)


class FakeConfig:
    public_base_url = "https://app.example.test"


class WebLoginRouteTests(unittest.TestCase):
    def setUp(self):
        self.oidc = FakeOIDC()
        self.sessions = FakeSessions()
        self.routes = WebLoginRoutes(FakeConfig(), self.oidc, self.sessions)

    def test_login_binds_state_to_browser_cookie(self):
        status, _, headers = self.routes.login()
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], self.oidc.begin())
        self.assertIn(OIDC_STATE_COOKIE + "=" + self.oidc.state, headers["Set-Cookie-2"])

    def test_callback_rejects_state_from_another_browser(self):
        status, _, _ = self.routes.callback(
            {"state": [self.oidc.state], "code": ["code"]},
            OIDC_STATE_COOKIE + "=different-state",
        )
        self.assertEqual(status, 400)
        self.assertEqual(self.oidc.calls, [])

    def test_callback_issues_secure_cookies_after_matching_state(self):
        status, _, headers = self.routes.callback(
            {"state": [self.oidc.state], "code": ["auth-code"]},
            OIDC_STATE_COOKIE + "=" + self.oidc.state,
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/web")
        self.assertIn("Secure", headers["Set-Cookie"])
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn(CSRF_COOKIE, headers["Set-Cookie-2"])
        self.assertEqual(self.oidc.calls, [(self.oidc.state, "auth-code")])

    def test_logout_requires_same_origin_and_session_csrf(self):
        cookies = "__Host-vibeos_session=session-token; " + CSRF_COOKIE + "=csrf-token"
        status, _, _ = self.routes.logout(cookies, {"csrf_token": ["csrf-token"]},
                                          "https://attacker.example")
        self.assertEqual(status, 403)
        self.assertEqual(self.sessions.revoked, [])
        status, _, headers = self.routes.logout(cookies, {"csrf_token": ["csrf-token"]},
                                                "https://app.example.test")
        self.assertEqual(status, 303)
        self.assertEqual(self.sessions.revoked, ["session-token"])
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertIn("Max-Age=0", headers["Set-Cookie-2"])


if __name__ == "__main__":
    unittest.main()
