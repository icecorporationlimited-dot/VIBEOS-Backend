import unittest

from publishing.autopilot import AutopilotStore
from publishing.identity import Principal
from publishing.slots import Calendar


class RecordingCollection:
    def __init__(self, doc=None):
        self.doc = doc
        self.find_one_queries = []
        self.update_calls = []
        self.find_queries = []
        self.indexes = []

    def create_index(self, *args, **kwargs):
        self.indexes.append((args, kwargs))

    def find_one(self, query):
        self.find_one_queries.append(query)
        if self.doc is None:
            return None
        if query == {"_id": self.doc.get("_id")}:
            return self.doc
        if "owner_id" in query and self.doc.get("owner_id") == query["owner_id"]:
            return self.doc
        if "$or" in query:
            for part in query["$or"]:
                if part.get("owner_id") == self.doc.get("owner_id") and self.doc.get("owner_id") is not None:
                    return self.doc
                if "$and" in part:
                    clauses = part["$and"]
                    if (self.doc.get("owner_id") is None
                            and clauses[0].get("owner_id", {}).get("$exists") is False
                            and self.doc.get("telegram_id") == clauses[1].get("telegram_id")):
                        return self.doc
        return None

    def update_one(self, query, update, upsert=False):
        self.update_calls.append((query, update, upsert))

    def find(self, query):
        self.find_queries.append(query)
        return []


class AutopilotOwnershipTests(unittest.TestCase):
    def test_web_preferences_use_namespaced_owner(self):
        col = RecordingCollection()
        store = AutopilotStore(col)
        principal = Principal.from_web("alice")
        store.update(principal, enabled=True)
        self.assertEqual(col.find_one_queries[0], {"owner_id": "web:alice"})
        query, update, upsert = col.update_calls[0]
        self.assertEqual(query, {"owner_id": "web:alice"})
        self.assertEqual(update["$setOnInsert"], {"owner_id": "web:alice"})
        self.assertTrue(upsert)

    def test_telegram_reads_allow_only_owner_or_unowned_legacy_record(self):
        col = RecordingCollection()
        store = AutopilotStore(col)
        store.get("123")
        self.assertEqual(col.find_one_queries[0]["$or"][0], {"owner_id": "tg:123"})
        self.assertEqual(
            col.find_one_queries[0]["$or"][1]["$and"],
            [{"owner_id": {"$exists": False}}, {"telegram_id": "123"}],
        )

    def test_legacy_preference_update_stamps_owner_id(self):
        legacy = {"_id": "pref-1", "telegram_id": "123", "enabled": True}
        col = RecordingCollection(doc=legacy)
        store = AutopilotStore(col)
        store.update("123", paused=True)
        query, update, _ = col.update_calls[0]
        self.assertEqual(query, {"_id": "pref-1"})
        self.assertEqual(update["$set"]["owner_id"], "tg:123")
        self.assertTrue(update["$set"]["paused"])


class CalendarOwnershipTests(unittest.TestCase):
    def test_busy_time_query_is_scoped_to_telegram_owner(self):
        jobs = RecordingCollection()
        calendar = Calendar(jobs, object(), lambda _: "UTC")
        calendar._busy_times("123")
        query = jobs.find_queries[0]
        self.assertEqual(query["$and"][0], {"telegram_id": "123"})
        self.assertEqual(query["$and"][1]["$or"][0], {"owner_id": "tg:123"})

    def test_web_busy_time_query_never_uses_telegram_fallback(self):
        jobs = RecordingCollection()
        calendar = Calendar(jobs, object(), lambda _: "UTC")
        calendar._busy_times(Principal.from_web("alice"))
        self.assertEqual(jobs.find_queries[0], {"owner_id": "web:alice"})


if __name__ == "__main__":
    unittest.main()
