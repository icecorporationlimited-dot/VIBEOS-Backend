import unittest

from publishing.autopilot import AutopilotStore


class EngagementCollection:
    def __init__(self, docs):
        self.docs = docs
        self.query = None

    def find(self, query):
        self.query = query
        clauses = query.get("$or", [])
        return [
            doc for doc in self.docs
            if any(doc.get(field) is value for clause in clauses
                   for field, value in clause.items())
        ]


class EngagementUserSelectionTests(unittest.TestCase):
    def test_comment_opt_in_does_not_require_posting_autopilot(self):
        col = EngagementCollection([
            {"telegram_id": "posting-only", "enabled": True},
            {"telegram_id": "replies-only", "enabled": False, "auto_replies": True},
            {"telegram_id": "starter-only", "enabled": False, "auto_comments": True},
            {"telegram_id": "off", "enabled": False},
            {"owner_id": "web:alice", "auto_replies": True},
        ])

        users = AutopilotStore(col).enabled_users()

        self.assertEqual(set(users), {"posting-only", "replies-only", "starter-only"})
        self.assertIn({"auto_replies": True}, col.query["$or"])
        self.assertIn({"auto_comments": True}, col.query["$or"])


if __name__ == "__main__":
    unittest.main()
