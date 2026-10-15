import unittest

from publishing.identity import (
    IdentityError,
    Principal,
    owner_filter,
    owns_document,
)


class PrincipalTests(unittest.TestCase):
    def test_owner_ids_are_namespaced(self):
        self.assertEqual(Principal.from_telegram("123").owner_id, "tg:123")
        self.assertEqual(Principal.from_web("123").owner_id, "web:123")

    def test_invalid_telegram_subject_is_rejected(self):
        for value in ("", "abc", "12:34", "-1", "1\\n"):
            with self.subTest(value=value), self.assertRaises(IdentityError):
                Principal.from_telegram(value)

    def test_web_principal_never_falls_back_to_telegram_owner(self):
        principal = Principal.from_web("provider|subject-123")
        self.assertEqual(owner_filter(principal), {"owner_id": "web:provider|subject-123"})
        self.assertFalse(owns_document(principal, {"telegram_id": "123"}))

    def test_telegram_can_read_legacy_document_without_owner_id(self):
        principal = Principal.from_telegram(123)
        self.assertTrue(owns_document(principal, {"telegram_id": "123"}))
        self.assertFalse(owns_document(principal, {"telegram_id": "456"}))

    def test_explicit_owner_id_overrides_legacy_telegram_id(self):
        principal = Principal.from_telegram("123")
        self.assertFalse(owns_document(
            principal, {"owner_id": "web:other", "telegram_id": "123"}
        ))
        self.assertTrue(owns_document(
            principal, {"owner_id": "tg:123", "telegram_id": "456"}
        ))

    def test_filter_has_legacy_fallback_only_for_telegram(self):
        telegram_filter = owner_filter(Principal.from_telegram("123"))
        self.assertEqual(telegram_filter["$or"][0], {"owner_id": "tg:123"})
        self.assertEqual(
            telegram_filter["$or"][1]["$and"],
            [{"owner_id": {"$exists": False}}, {"telegram_id": "123"}],
        )
        self.assertEqual(owner_filter(Principal.from_web("abc")), {"owner_id": "web:abc"})

    def test_unsupported_identity_kind_is_rejected(self):
        with self.assertRaises(IdentityError):
            Principal("admin", "1")


if __name__ == "__main__":
    unittest.main()
