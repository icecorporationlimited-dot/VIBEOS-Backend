import unittest

from publishing.identity import Principal
from publishing.server import _web_media_response


class FakeSessions:
    def resolve(self, token):
        return Principal.from_web("alice") if token == "valid-session" else None


class FakeWebLogin:
    sessions = FakeSessions()


class FakeContentBuffer:
    def get_media_for(self, principal, asset_id, variant=None):
        if principal.owner_id != "web:alice" or asset_id != "alice-asset":
            raise KeyError("Media not found")
        return b"private-media:" + (variant or "original").encode()


class WebMediaRouteTests(unittest.TestCase):
    def setUp(self):
        self.login = FakeWebLogin()
        self.buffer = FakeContentBuffer()

    def test_requires_authenticated_web_session(self):
        status, body, ctype = _web_media_response(
            self.login, self.buffer, "", "alice-asset", "original"
        )
        self.assertEqual((status, body, ctype), (404, b"not found", "text/plain"))

    def test_owner_can_read_own_media_variant(self):
        status, body, ctype = _web_media_response(
            self.login, self.buffer,
            "__Host-vibeos_session=valid-session", "alice-asset", "post"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"private-media:post")
        self.assertEqual(ctype, "image/jpeg")

    def test_cross_tenant_media_is_hidden_as_not_found(self):
        status, body, ctype = _web_media_response(
            self.login, self.buffer,
            "__Host-vibeos_session=valid-session", "bob-asset", "original"
        )
        self.assertEqual((status, body, ctype), (404, b"not found", "text/plain"))

    def test_reel_uses_video_content_type(self):
        status, _, ctype = _web_media_response(
            self.login, self.buffer,
            "__Host-vibeos_session=valid-session", "alice-asset", "reel"
        )
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "video/mp4")

    def test_unknown_variant_is_not_served(self):
        status, body, ctype = _web_media_response(
            self.login, self.buffer,
            "__Host-vibeos_session=valid-session", "alice-asset", "private"
        )
        self.assertEqual((status, body, ctype), (404, b"not found", "text/plain"))


if __name__ == "__main__":
    unittest.main()
