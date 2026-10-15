import unittest
from datetime import datetime, timezone

from publishing.identity import Principal
from publishing.service import PublishService, CANCELLED


class Result:
    def __init__(self, matched_count=1):
        self.matched_count = matched_count


class RecordingJobs:
    def __init__(self):
        self.update_calls = []
        self.find_queries = []

    def update_one(self, query, update):
        self.update_calls.append((query, update))
        return Result()

    def find(self, query):
        self.find_queries.append(query)
        return []


class JobLifecycleOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.jobs = RecordingJobs()
        self.service = PublishService(
            self.jobs, object(), object(), object(), object(), object(),
            clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    def assert_web_owner_query(self, query):
        self.assertEqual(query["$and"][1], {"owner_id": "web:alice"})

    def test_web_retry_is_owner_scoped(self):
        self.service.retry("job-1", Principal.from_web("alice"))
        query, _ = self.jobs.update_calls[-1]
        self.assert_web_owner_query(query)
        self.assertEqual(query["$and"][0]["_id"], "job-1")

    def test_web_cancel_is_owner_scoped(self):
        self.service.cancel("job-1", Principal.from_web("alice"))
        query, update = self.jobs.update_calls[-1]
        self.assert_web_owner_query(query)
        self.assertEqual(update["$set"]["status"], CANCELLED)

    def test_web_release_is_owner_scoped(self):
        self.service.release("job-1", Principal.from_web("alice"))
        query, update = self.jobs.update_calls[-1]
        self.assert_web_owner_query(query)
        self.assertEqual(query["$and"][0]["status"], "held")
        self.assertTrue(update["$set"]["policy_override"])

    def test_telegram_retry_keeps_legacy_owner_fallback(self):
        self.service.retry("job-1", "123")
        query, _ = self.jobs.update_calls[-1]
        owner = query["$and"][1]
        self.assertEqual(owner["$or"][0], {"owner_id": "tg:123"})
        self.assertEqual(owner["$or"][1]["$and"],
                         [{"owner_id": {"$exists": False}}, {"telegram_id": "123"}])

    def test_queue_and_prefix_reads_are_owner_scoped(self):
        principal = Principal.from_web("alice")
        self.service.queue(principal)
        self.service.find_by_prefix(principal, "abc")
        self.assertEqual(self.jobs.find_queries[0], {"$and": [{}, {"owner_id": "web:alice"}]})
        self.assertEqual(self.jobs.find_queries[1], {"$and": [{}, {"owner_id": "web:alice"}]})


if __name__ == "__main__":
    unittest.main()
