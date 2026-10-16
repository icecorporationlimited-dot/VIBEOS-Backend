import unittest
from datetime import datetime, timedelta, timezone

from publishing.oidc import OIDCError, OIDCLogin, _digest, _https_url


class FakeCollection:
    def __init__(self):
        self.docs = []
        self.indexes = []

    def create_index(self, *args, **kwargs):
        self.indexes.append((args, kwargs))

    def insert_one(self, doc):
        self.docs.append(dict(doc))

    def find_one_and_delete(self, query):
        for index, doc in enumerate(self.docs):
            if doc.get("state_hash") != query.get("state_hash"):
                continue
            expiry = query["expires_at"]["$gt"]
            if doc.get("expires_at") <= expiry:
                continue
            return self.docs.pop(index)
        return None


class OIDCPrimitiveTests(unittest.TestCase):
    def test_only_https_urls_are_accepted(self):
        self.assertTrue(_https_url("https://login.example.test/callback"))
        self.assertFalse(_https_url("http://login.example.test/callback"))
        self.assertFalse(_https_url("https://user:pass@login.example.test/callback"))

    def test_state_is_stored_as_digest_not_plaintext(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        transactions = FakeCollection()
        login = OIDCLogin(
            "https://id.example.test", "client", "secret",
            "https://app.example.test/oidc/callback", transactions,
            FakeCollection(), http=object(), clock=lambda: now,
        )
        state = "opaque-state-value"
        transactions.insert_one({
            "state_hash": _digest(state), "nonce": "n", "code_verifier": "v",
            "created_at": now, "expires_at": now + timedelta(minutes=10),
        })
        self.assertNotIn(state, str(transactions.docs))
        found = transactions.find_one_and_delete({
            "state_hash": _digest(state), "expires_at": {"$gt": now},
        })
        self.assertEqual(found["code_verifier"], "v")
        self.assertIsNone(transactions.find_one_and_delete({
            "state_hash": _digest(state), "expires_at": {"$gt": now},
        }))

    def test_insecure_configuration_is_rejected(self):
        with self.assertRaises(OIDCError):
            OIDCLogin("http://id.example.test", "client", "secret",
                      "https://app.example.test/callback", FakeCollection(),
                      FakeCollection())


if __name__ == "__main__":
    unittest.main()
