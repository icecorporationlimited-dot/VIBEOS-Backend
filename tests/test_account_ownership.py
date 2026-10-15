import unittest
from datetime import datetime, timedelta, timezone

from publishing.accounts import AccountStore, ACTIVE, DISCONNECTED, NotConnected
from publishing.identity import Principal


class FakeCollection:
    def __init__(self):
        self.docs = []
        self.indexes = []

    def create_index(self, *args, **kwargs):
        self.indexes.append((args, kwargs))

    def list_indexes(self):
        for args, options in self.indexes:
            field = args[0]
            key = {field: 1} if isinstance(field, str) else dict(field)
            yield {"name": "_".join(key) + "_1", "key": key, **options}

    def drop_index(self, name):
        self.indexes = [
            (args, options) for args, options in self.indexes
            if "_".join(args[0] if isinstance(args[0], tuple) else (args[0],)) + "_1" != name
        ]

    def _matches(self, doc, query):
        if "$or" in query:
            return any(self._matches(doc, part) for part in query["$or"])
        if "$and" in query:
            return all(self._matches(doc, part) for part in query["$and"])
        for key, expected in query.items():
            if isinstance(expected, dict) and "$exists" in expected:
                if (key in doc) != expected["$exists"]:
                    return False
            elif doc.get(key) != expected:
                return False
        return True

    def find_one(self, query):
        return next((d for d in self.docs if self._matches(d, query)), None)

    def insert_one(self, doc):
        doc = dict(doc)
        doc.setdefault("_id", str(len(self.docs) + 1))
        self.docs.append(doc)

    def update_one(self, query, update, upsert=False):
        doc = self.find_one(query)
        if not doc:
            return
        doc.update(update.get("$set", {}))
        for key in update.get("$unset", {}):
            doc.pop(key, None)


class FakeCipher:
    def encrypt(self, token):
        return "enc:" + token

    def decrypt(self, token):
        return token.removeprefix("enc:")


class FakeGraph:
    def me(self, token):
        return {"user_id": "ig-" + token, "username": "sample", "account_type": "BUSINESS"}


class FakeOAuth:
    def refresh(self, token):
        return {"access_token": token + "-fresh", "expires_in": 86400}


class AccountOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.collection = FakeCollection()
        self.store = AccountStore(self.collection, FakeCipher(), FakeOAuth(), FakeGraph())

    def test_indexes_keep_legacy_and_add_owner_id(self):
        self.store.ensure_indexes()
        self.assertIn((("telegram_id",), {"unique": True, "sparse": True}), self.collection.indexes)
        self.assertIn((("owner_id",), {"unique": True, "sparse": True}), self.collection.indexes)

    def test_old_non_sparse_telegram_index_is_migrated(self):
        self.collection.indexes.append((("telegram_id",), {"unique": True}))
        self.store.ensure_indexes()
        self.assertNotIn((("telegram_id",), {"unique": True}), self.collection.indexes)
        self.assertIn((("telegram_id",), {"unique": True, "sparse": True}), self.collection.indexes)

    def test_web_accounts_are_namespaced_and_not_visible_to_telegram(self):
        web = Principal.from_web("issuer|subject-123")
        self.store.connect_for(web, {"access_token": "webtoken", "expires_in": 3600})
        self.assertEqual(self.store.get_for(web)["owner_id"], "web:issuer|subject-123")
        self.assertIsNone(self.store.get("123456"))

    def test_legacy_telegram_account_is_migrated_on_reconnect(self):
        self.collection.insert_one({
            "telegram_id": "123456", "ig_user_id": "old-ig",
            "token_enc": "enc:old", "token_expires_at": datetime.now(timezone.utc) + timedelta(days=30),
            "status": ACTIVE,
        })
        acct = self.store.connect("123456", {"access_token": "new", "expires_in": 3600})
        self.assertEqual(acct["owner_id"], "tg:123456")
        self.assertEqual(acct["telegram_id"], "123456")
        self.assertEqual(len(self.collection.docs), 1)

    def test_disconnect_only_affects_matching_web_owner(self):
        one, two = Principal.from_web("idp|one"), Principal.from_web("idp|two")
        self.store.connect_for(one, {"access_token": "one", "expires_in": 3600})
        self.store.connect_for(two, {"access_token": "two", "expires_in": 3600})
        self.store.disconnect_for(one)
        self.assertEqual(self.store.get_for(one)["status"], DISCONNECTED)
        self.assertEqual(self.store.get_for(two)["status"], ACTIVE)

    def test_missing_account_does_not_return_another_users_token(self):
        self.store.connect_for(Principal.from_web("idp|one"), {
            "access_token": "one", "expires_in": 3600
        })
        with self.assertRaises(NotConnected):
            self.store.get_token_for(Principal.from_web("idp|two"))


if __name__ == "__main__":
    unittest.main()
