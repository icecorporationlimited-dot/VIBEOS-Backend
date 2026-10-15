import unittest

from publishing.identity import Principal
from publishing.service import _owned_query


class PublishJobOwnershipQueryTests(unittest.TestCase):
    def test_telegram_query_accepts_owned_and_legacy_jobs_only(self):
        principal = Principal.from_telegram("123")
        query = _owned_query(principal, {"_id": "job-1"})
        self.assertEqual(query["$and"][0], {"_id": "job-1"})
        self.assertEqual(query["$and"][1]["$or"][0], {"owner_id": "tg:123"})
        self.assertEqual(
            query["$and"][1]["$or"][1]["$and"],
            [{"owner_id": {"$exists": False}}, {"telegram_id": "123"}],
        )

    def test_web_owned_job_cannot_match_telegram_legacy_fallback(self):
        # owner_filter deliberately gives web principals no Telegram fallback.
        query = _owned_query(Principal.from_web("alice"), {"_id": "job-1"})
        self.assertEqual(query, {
            "$and": [{"_id": "job-1"}, {"owner_id": "web:alice"}]
        })


if __name__ == "__main__":
    unittest.main()
