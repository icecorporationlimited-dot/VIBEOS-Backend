import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from content_buffer import ContentBuffer, MediaTooLarge, MemoryBlobStore, QuotaExceeded


class QuotaCollection:
    """Minimal collection: find_one (no match), insert_one, count_documents."""

    def __init__(self):
        self.docs = []

    def find_one(self, query, *a, **k):
        return None

    def insert_one(self, doc):
        self.docs.append(doc)

    def count_documents(self, query):
        since = query["created_at"]["$gte"]
        return sum(1 for d in self.docs
                   if d["telegram_id"] == query["telegram_id"] and d["created_at"] >= since)


class UploadQuotaTests(unittest.TestCase):
    def setUp(self):
        self.col = QuotaCollection()
        self.buf = ContentBuffer(self.col, MemoryBlobStore())

    def test_user_is_blocked_at_limit_but_others_are_not(self):
        with mock.patch.dict(os.environ, {"USER_DAILY_UPLOADS": "2"}):
            self.buf.add_asset("1", b"a", "photo")
            self.buf.add_asset("1", b"b", "photo")
            with self.assertRaises(QuotaExceeded):
                self.buf.add_asset("1", b"c", "photo")
            self.buf.add_asset("2", b"c", "photo")          # other user unaffected

    def test_old_uploads_do_not_count(self):
        with mock.patch.dict(os.environ, {"USER_DAILY_UPLOADS": "1"}):
            self.buf.add_asset("1", b"a", "photo")
            self.col.docs[0]["created_at"] = datetime.now(timezone.utc) - timedelta(hours=25)
            self.buf.add_asset("1", b"b", "photo")           # allowed again

    def test_quota_error_is_a_media_too_large(self):
        self.assertTrue(issubclass(QuotaExceeded, MediaTooLarge))

    def test_process_batch_reports_quota_blocked(self):
        with mock.patch.dict(os.environ, {"USER_DAILY_UPLOADS": "1"}):
            with mock.patch.object(self.buf, "analyze_batch"), \
                 mock.patch.object(self.buf, "select_shortlist", return_value={"selected": []}):
                report = self.buf.process_batch("1", "b", [(b"a", "x"), (b"b", "y")])
        self.assertEqual(report["quota_blocked"], 1)
        self.assertEqual(len(report["added"]), 1)

    def test_bad_env_value_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {"USER_DAILY_UPLOADS": "nope"}):
            self.buf.add_asset("1", b"a", "photo")


if __name__ == "__main__":
    unittest.main()
