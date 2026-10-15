import unittest

from content_buffer import ContentBuffer, MemoryBlobStore
from publishing.identity import Principal


def matches(document, query):
    """Small Mongo selector subset used by the ownership regression tests."""
    for key, expected in query.items():
        if key == "$and":
            if not all(matches(document, part) for part in expected):
                return False
        elif key == "$or":
            if not any(matches(document, part) for part in expected):
                return False
        elif isinstance(expected, dict) and "$exists" in expected:
            if (key in document) != expected["$exists"]:
                return False
        elif document.get(key) != expected:
            return False
    return True


class FakeAssets:
    def __init__(self, rows):
        self.rows = rows

    def find_one(self, query):
        return next((row for row in self.rows if matches(row, query)), None)


class TenantMediaTests(unittest.TestCase):
    def setUp(self):
        self.blobs = MemoryBlobStore()
        self.alice_blob = self.blobs.put(b"alice-private-media")
        self.bob_blob = self.blobs.put(b"bob-private-media")
        self.legacy_blob = self.blobs.put(b"legacy-telegram-media")
        self.assets = FakeAssets([
            {"_id": "alice-asset", "owner_id": "web:alice", "blob_id": self.alice_blob},
            {"_id": "bob-asset", "owner_id": "web:bob", "blob_id": self.bob_blob},
            {"_id": "telegram-asset", "telegram_id": "123", "blob_id": self.legacy_blob},
        ])
        self.buffer = ContentBuffer(self.assets, self.blobs)

    def test_web_owner_can_read_own_asset(self):
        self.assertEqual(
            self.buffer.get_media_for(Principal.from_web("alice"), "alice-asset"),
            b"alice-private-media",
        )

    def test_web_owner_cannot_read_another_users_asset(self):
        with self.assertRaisesRegex(KeyError, "Media not found"):
            self.buffer.get_media_for(Principal.from_web("alice"), "bob-asset")

    def test_web_owner_cannot_read_legacy_telegram_asset(self):
        with self.assertRaisesRegex(KeyError, "Media not found"):
            self.buffer.get_media_for(Principal.from_web("alice"), "telegram-asset")

    def test_telegram_owner_can_read_legacy_asset(self):
        self.assertEqual(
            self.buffer.get_media_for(Principal.from_telegram("123"), "telegram-asset"),
            b"legacy-telegram-media",
        )

    def test_raw_user_id_is_not_accepted_as_principal(self):
        with self.assertRaises(TypeError):
            self.buffer.get_media_for("alice", "alice-asset")


if __name__ == "__main__":
    unittest.main()
