import unittest
from datetime import datetime, timedelta, timezone

from content_buffer import ContentBuffer, MemoryBlobStore
from publishing.accounts import AccountStore, NotConnected
from publishing.identity import Principal
from publishing.service import PublishError, PublishService, QUEUED


def matches(doc, query):
    for key, expected in query.items():
        if key == "$and":
            if not all(matches(doc, part) for part in expected):
                return False
        elif key == "$or":
            if not any(matches(doc, part) for part in expected):
                return False
        elif isinstance(expected, dict):
            if "$exists" in expected and ((key in doc) != expected["$exists"]):
                return False
            if "$in" in expected and doc.get(key) not in expected["$in"]:
                return False
            if "$ne" in expected and doc.get(key) == expected["$ne"]:
                return False
            if "$lt" in expected and not (doc.get(key) is not None and doc[key] < expected["$lt"]):
                return False
        elif doc.get(key) != expected:
            return False
    return True


class Result:
    def __init__(self, matched_count=0):
        self.matched_count = matched_count


class FakeCollection:
    def __init__(self, docs=()):
        self.docs = [dict(row) for row in docs]
        self.update_calls = []

    def create_index(self, *args, **kwargs):
        pass

    def find_one(self, query):
        return next((row for row in self.docs if matches(row, query)), None)

    def find(self, query):
        return [row for row in self.docs if matches(row, query)]

    def insert_one(self, document):
        row = dict(document)
        row.setdefault("_id", str(len(self.docs) + 1))
        self.docs.append(row)
        return type("InsertResult", (), {"inserted_id": row["_id"]})()

    def update_one(self, query, update, upsert=False):
        self.update_calls.append((query, update))
        row = self.find_one(query)
        if row is None:
            return Result()
        row.update(update.get("$set", {}))
        for key in update.get("$unset", {}):
            row.pop(key, None)
        for key, value in update.get("$inc", {}).items():
            row[key] = row.get(key, 0) + value
        for key, value in update.get("$push", {}).items():
            row.setdefault(key, []).append(value)
        return Result(1)


class Cipher:
    def encrypt(self, token):
        return "enc:" + token

    def decrypt(self, token):
        return token.removeprefix("enc:")


class Graph:
    def me(self, token):
        return {"user_id": "ig-" + token, "username": token, "account_type": "BUSINESS"}


class OAuth:
    def refresh(self, token):
        return {"access_token": token + "-refreshed", "expires_in": 3600}


class MultiUserSecurityE2ETests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 15, tzinfo=timezone.utc)
        self.alice = Principal.from_web("alice")
        self.bob = Principal.from_web("bob")
        self.accounts_col = FakeCollection()
        self.accounts = AccountStore(
            self.accounts_col, Cipher(), OAuth(), Graph(), clock=lambda: self.now
        )
        self.accounts.connect_for(self.alice, {"access_token": "alice-token", "expires_in": 30 * 86400})
        self.accounts.connect_for(self.bob, {"access_token": "bob-token", "expires_in": 30 * 86400})

    def test_account_tokens_are_isolated_between_web_users(self):
        self.assertEqual(self.accounts.get_token_for(self.alice)[1], "alice-token")
        self.assertEqual(self.accounts.get_token_for(self.bob)[1], "bob-token")
        with self.assertRaises(NotConnected):
            self.accounts.get_token_for(Principal.from_web("mallory"))

    def test_refresh_write_rechecks_account_owner(self):
        acct = self.accounts.get_for(self.alice)
        self.accounts._refresh(self.alice, acct, "alice-token")
        query, _ = self.accounts_col.update_calls[-1]
        self.assertEqual(query["$and"][0], {"_id": acct["_id"]})
        self.assertEqual(query["$and"][1], {"owner_id": "web:alice"})
        self.assertNotEqual(query["$and"][1], {"owner_id": "web:bob"})

    def test_media_read_cannot_cross_tenant_boundary(self):
        blobs = MemoryBlobStore()
        alice_blob = blobs.put(b"alice-private")
        bob_blob = blobs.put(b"bob-private")
        assets = FakeCollection([
            {"_id": "alice-asset", "owner_id": "web:alice", "blob_id": alice_blob},
            {"_id": "bob-asset", "owner_id": "web:bob", "blob_id": bob_blob},
        ])
        buffer = ContentBuffer(assets, blobs)
        self.assertEqual(buffer.get_media_for(self.alice, "alice-asset"), b"alice-private")
        with self.assertRaisesRegex(KeyError, "Media not found"):
            buffer.get_media_for(self.alice, "bob-asset")

    def test_job_queue_and_cancellation_are_owner_scoped(self):
        jobs = FakeCollection([
            {"_id": "alice-job", "owner_id": "web:alice", "status": QUEUED,
             "created_at": self.now, "next_attempt_at": self.now},
            {"_id": "bob-job", "owner_id": "web:bob", "status": QUEUED,
             "created_at": self.now, "next_attempt_at": self.now},
        ])
        service = PublishService(jobs, object(), object(), self.accounts, object(), object(),
                                 clock=lambda: self.now)
        self.assertEqual([job["_id"] for job in service.queue(self.alice)], ["alice-job"])
        self.assertTrue(service.cancel("alice-job", self.alice))
        self.assertFalse(service.cancel("bob-job", self.alice))
        self.assertEqual(jobs.find_one({"_id": "bob-job"})["status"], QUEUED)

    def test_enqueue_for_scopes_account_and_assets_to_principal(self):
        assets = FakeCollection([
            {"_id": "alice-asset", "owner_id": "web:alice", "status": "ready",
             "sha256": "alice-hash", "creative": {"caption": "Alice"}},
            {"_id": "bob-asset", "owner_id": "web:bob", "status": "ready",
             "sha256": "bob-hash", "creative": {"caption": "Bob"}},
        ])
        buffer = ContentBuffer(assets, MemoryBlobStore())
        jobs = FakeCollection()
        service = PublishService(
            jobs, object(), buffer, self.accounts, object(), object(),
            clock=lambda: self.now,
        )

        job = service.enqueue_for(self.alice, ["alice-asset"], "photo")
        self.assertEqual(job["owner_id"], "web:alice")
        self.assertNotIn("telegram_id", job)
        self.assertEqual(job["ig_user_id"], "ig-alice-token")
        self.assertTrue(job["idempotency_key"].startswith("web:alice:"))

        with self.assertRaises(PublishError):
            service.enqueue_for(self.bob, ["alice-asset"], "photo")
        self.assertEqual(len(jobs.docs), 1)

    def test_telegram_enqueue_compatibility_keeps_legacy_identity(self):
        telegram = Principal.from_telegram("123")
        self.accounts.connect_for(
            telegram, {"access_token": "telegram-token", "expires_in": 30 * 86400}
        )
        assets = FakeCollection([
            {"_id": "telegram-asset", "owner_id": "tg:123", "status": "ready",
             "sha256": "telegram-hash", "creative": {"caption": "Telegram"}},
        ])
        service = PublishService(
            FakeCollection(), object(), ContentBuffer(assets, MemoryBlobStore()),
            self.accounts, object(), object(), clock=lambda: self.now,
        )
        job = service.enqueue("123", ["telegram-asset"], "photo")
        self.assertEqual(job["owner_id"], "tg:123")
        self.assertEqual(job["telegram_id"], "123")

    def test_telegram_worker_never_claims_web_owned_jobs(self):
        jobs = FakeCollection([
            {"_id": "web-job", "owner_id": "web:alice", "status": QUEUED,
             "created_at": self.now, "next_attempt_at": self.now},
            {"_id": "telegram-job", "owner_id": "tg:123", "telegram_id": "123",
             "status": QUEUED, "created_at": self.now, "next_attempt_at": self.now},
        ])
        service = PublishService(jobs, object(), object(), object(), object(), object(),
                                 clock=lambda: self.now)
        self.assertEqual([job["_id"] for job in service.due_jobs()], ["telegram-job"])
        before = len(jobs.update_calls)
        result = service.run("web-job")
        self.assertEqual(result["_id"], "web-job")
        self.assertEqual(len(jobs.update_calls), before)
        self.assertEqual(jobs.find_one({"_id": "web-job"})["status"], QUEUED)

    def test_worker_rejects_mismatched_telegram_and_owner_ids(self):
        jobs = FakeCollection([
            {"_id": "mismatched-job", "owner_id": "tg:456", "telegram_id": "123",
             "status": QUEUED, "created_at": self.now, "next_attempt_at": self.now},
            {"_id": "invalid-id-job", "owner_id": "tg:not-numeric", "telegram_id": "not-numeric",
             "status": QUEUED, "created_at": self.now, "next_attempt_at": self.now},
            {"_id": "legacy-job", "telegram_id": "789", "status": QUEUED,
             "created_at": self.now, "next_attempt_at": self.now},
        ])
        service = PublishService(jobs, object(), object(), object(), object(), object(),
                                 clock=lambda: self.now)
        self.assertEqual([job["_id"] for job in service.due_jobs()], ["legacy-job"])
        for job_id in ("mismatched-job", "invalid-id-job"):
            before = len(jobs.update_calls)
            result = service.run(job_id)
            self.assertEqual(result["_id"], job_id)
            self.assertEqual(len(jobs.update_calls), before)
            self.assertEqual(jobs.find_one({"_id": job_id})["status"], QUEUED)


if __name__ == "__main__":
    unittest.main()
