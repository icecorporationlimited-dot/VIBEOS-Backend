import unittest
from datetime import datetime, timedelta, timezone

from publishing.identity import Principal
from publishing.web_auth import (
    WebSessionStore,
    clear_session_cookie,
    session_cookie,
)


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = [dict(d) for d in (docs or [])]
        self.indexes = []

    def create_index(self, *args, **kwargs):
        self.indexes.append((args, kwargs))

    def find_one(self, query):
        for doc in self.docs:
            if self._matches(doc, query):
                return dict(doc)
        return None

    def insert_one(self, doc):
        item = dict(doc)
        item.setdefault("_id", str(len(self.docs) + 1))
        self.docs.append(item)
        return type("InsertResult", (), {"inserted_id": item["_id"]})()

    def delete_one(self, query):
        before = len(self.docs)
        self.docs = [d for d in self.docs if not self._matches(d, query)]
        return type("DeleteResult", (), {"deleted_count": before - len(self.docs)})()

    @staticmethod
    def _matches(doc, query):
        for key, value in query.items():
            actual = doc.get(key)
            if isinstance(value, dict) and "$ne" in value:
                if actual == value["$ne"]:
                    return False
            elif actual != value:
                return False
        return True


class WebSessionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.users = FakeCollection([{"_id": "user-123", "auth_subject": "oidc|abc"}])
        self.sessions = FakeCollection()
        self.store = WebSessionStore(
            self.sessions, self.users, ttl_seconds=600, clock=lambda: self.now
        )

    def test_issue_resolve_and_revoke(self):
        issued = self.store.create("oidc|abc")
        self.assertEqual(issued["principal"], Principal.from_web("user-123"))
        self.assertNotIn(issued["token"], str(self.sessions.docs))
        self.assertEqual(self.store.resolve(issued["token"]), Principal.from_web("user-123"))
        self.store.revoke(issued["token"])
        self.assertIsNone(self.store.resolve(issued["token"]))

    def test_unknown_or_disabled_identity_cannot_get_session(self):
        with self.assertRaises(ValueError):
            self.store.create("not-linked")
        self.users.docs[0]["disabled"] = True
        with self.assertRaises(ValueError):
            self.store.create("oidc|abc")

    def test_expired_session_is_rejected(self):
        issued = self.store.create("oidc|abc")
        self.now += timedelta(seconds=601)
        self.assertIsNone(self.store.resolve(issued["token"]))

    def test_csrf_token_is_bound_to_session(self):
        issued = self.store.create("oidc|abc")
        self.assertTrue(self.store.validate_csrf(issued["token"], issued["csrf_token"]))
        self.assertFalse(self.store.validate_csrf(issued["token"], "wrong"))
        other = self.store.create("oidc|abc")
        self.assertFalse(self.store.validate_csrf(issued["token"], other["csrf_token"]))

    def test_disabled_user_invalidates_existing_session(self):
        issued = self.store.create("oidc|abc")
        self.users.docs[0]["disabled"] = True
        self.assertIsNone(self.store.resolve(issued["token"]))

    def test_cookie_has_secure_flags_and_clears(self):
        value = session_cookie("abc")
        self.assertIn("Secure", value)
        self.assertIn("HttpOnly", value)
        self.assertIn("SameSite=Lax", value)
        self.assertIn("Path=/", value)
        self.assertIn("Max-Age=0", clear_session_cookie())

    def test_cookie_rejects_header_injection(self):
        with self.assertRaises(ValueError):
            session_cookie("abc\\r\\nSet-Cookie: attacker=1")


if __name__ == "__main__":
    unittest.main()
