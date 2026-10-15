import hashlib
import unittest
from datetime import datetime, timedelta, timezone

from publishing.web_instagram import WebInstagramConnect, STATE_COOKIE, CSRF_COOKIE
from publishing.identity import Principal


class FakeCollection:
    def __init__(self):
        self.docs = []
    def create_index(self, *args, **kwargs):
        pass
    def insert_one(self, doc):
        self.docs.append(dict(doc))
    def delete_one(self, query):
        self.docs = [d for d in self.docs if not all(d.get(k) == v for k, v in query.items())]
    def find_one_and_delete(self, query):
        for i, doc in enumerate(self.docs):
            if all((doc.get(k) == v if not isinstance(v, dict) else
                    (doc.get(k) is not None and doc[k] > v["$gt"])) for k, v in query.items()):
                return self.docs.pop(i)
        return None


class FakeSessions:
    def __init__(self):
        self.principal = Principal.from_web("user-1")
    def resolve(self, token):
        return self.principal if token == "session" else None
    def validate_csrf(self, token, csrf):
        return token == "session" and csrf == "csrf"


class FakeOAuth:
    def __init__(self):
        self.url_state = None
        self.code_calls = []
    def web_authorize_url(self, state):
        self.url_state = state
        return "https://www.instagram.com/oauth/authorize?state=" + state
    def exchange_code(self, code, redirect_uri=None):
        self.code_calls.append((code, redirect_uri))
        return {"access_token": "token", "expires_in": 3600}


class FakeAccounts:
    def __init__(self):
        self.calls = []
    def connect_for(self, principal, token_info):
        self.calls.append((principal, token_info))
        return {"username": "testuser"}


class FakeConfig:
    public_base_url = "https://app.example.test"


class WebInstagramConnectTests(unittest.TestCase):
    def setUp(self):
        self.tx = FakeCollection()
        self.sessions = FakeSessions()
        self.oauth = FakeOAuth()
        self.accounts = FakeAccounts()
        self.routes = WebInstagramConnect(FakeConfig(), self.oauth, self.accounts,
                                          self.sessions, self.tx)
        self.cookies = "__Host-vibeos_session=session; " + CSRF_COOKIE + "=csrf"

    def test_start_requires_origin_and_session_csrf(self):
        status, _, _ = self.routes.start(self.cookies, {"csrf_token": ["csrf"]},
                                         "https://attacker.example")
        self.assertEqual(status, 403)
        self.assertEqual(self.tx.docs, [])

    def test_start_sets_one_time_browser_state_cookie(self):
        status, _, headers = self.routes.start(self.cookies, {"csrf_token": ["csrf"]},
                                               "https://app.example.test")
        self.assertEqual(status, 303)
        self.assertIn(STATE_COOKIE + "=", headers["Set-Cookie"])
        self.assertEqual(len(self.tx.docs), 1)
        self.assertEqual(self.tx.docs[0]["owner_id"], "web:user-1")

    def test_callback_requires_matching_browser_state(self):
        self.routes.start(self.cookies, {"csrf_token": ["csrf"]}, "https://app.example.test")
        status, _, _ = self.routes.callback(
            {"state": [self.oauth.url_state], "code": ["code"]},
            self.cookies + "; " + STATE_COOKIE + "=other")
        self.assertEqual(status, 400)
        self.assertEqual(self.oauth.code_calls, [])
        self.assertEqual(self.accounts.calls, [])

    def test_callback_consumes_state_and_links_to_session_principal(self):
        self.routes.start(self.cookies, {"csrf_token": ["csrf"]}, "https://app.example.test")
        status, body, headers = self.routes.callback(
            {"state": [self.oauth.url_state], "code": ["code"]},
            self.cookies + "; " + STATE_COOKIE + "=" + self.oauth.url_state)
        self.assertEqual(status, 200)
        self.assertIn("@testuser", body)
        self.assertEqual(self.oauth.code_calls[0], (
            "code", "https://app.example.test/web/instagram/callback"))
        self.assertEqual(self.accounts.calls[0][0], self.sessions.principal)
        self.assertEqual(self.tx.docs, [])
        self.assertIn("Max-Age=0", headers["Set-Cookie"])

    def test_callback_cannot_be_replayed(self):
        self.routes.start(self.cookies, {"csrf_token": ["csrf"]}, "https://app.example.test")
        query = {"state": [self.oauth.url_state], "code": ["code"]}
        cookies = self.cookies + "; " + STATE_COOKIE + "=" + self.oauth.url_state
        self.assertEqual(self.routes.callback(query, cookies)[0], 200)
        self.assertEqual(self.routes.callback(query, cookies)[0], 400)
        self.assertEqual(len(self.accounts.calls), 1)


if __name__ == "__main__":
    unittest.main()
