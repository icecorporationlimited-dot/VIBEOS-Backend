import unittest
from types import SimpleNamespace

from publishing.web_config import load_web_auth_settings


class WebAuthConfigTests(unittest.TestCase):
    def test_unconfigured_returns_none(self):
        self.assertIsNone(load_web_auth_settings(SimpleNamespace(), {}))

    def test_settings_module_is_supported_for_local_development(self):
        settings = SimpleNamespace(
            OIDC_ISSUER=" https://id.example.test ",
            OIDC_CLIENT_ID=" client ",
            OIDC_CLIENT_SECRET=" secret ",
        )
        self.assertEqual(load_web_auth_settings(settings, {}), {
            "issuer": "https://id.example.test",
            "client_id": "client",
            "client_secret": "secret",
        })

    def test_environment_secrets_override_settings(self):
        settings = SimpleNamespace(
            OIDC_ISSUER="https://old.example.test",
            OIDC_CLIENT_ID="old-client",
            OIDC_CLIENT_SECRET="old-secret",
        )
        env = {
            "OIDC_ISSUER": "https://id.example.test",
            "OIDC_CLIENT_ID": "env-client",
            "OIDC_CLIENT_SECRET": "env-secret",
        }
        self.assertEqual(load_web_auth_settings(settings, env), {
            "issuer": "https://id.example.test",
            "client_id": "env-client",
            "client_secret": "env-secret",
        })

    def test_explicitly_empty_environment_value_does_not_fall_back(self):
        settings = SimpleNamespace(
            OIDC_ISSUER="https://id.example.test",
            OIDC_CLIENT_ID="client",
            OIDC_CLIENT_SECRET="secret",
        )
        with self.assertRaisesRegex(ValueError, "Configure OIDC_ISSUER"):
            load_web_auth_settings(settings, {"OIDC_CLIENT_SECRET": ""})

    def test_partial_configuration_is_rejected(self):
        settings = SimpleNamespace(OIDC_ISSUER="https://id.example.test")
        with self.assertRaisesRegex(ValueError, "Configure OIDC_ISSUER"):
            load_web_auth_settings(settings, {})

    def test_non_https_issuer_is_rejected(self):
        settings = SimpleNamespace(
            OIDC_ISSUER="http://id.example.test",
            OIDC_CLIENT_ID="client",
            OIDC_CLIENT_SECRET="secret",
        )
        with self.assertRaisesRegex(ValueError, "must use HTTPS"):
            load_web_auth_settings(settings, {})


if __name__ == "__main__":
    unittest.main()
