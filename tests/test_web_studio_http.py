import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer

from content_buffer import ContentBuffer, MemoryBlobStore
from publishing.autopilot import AutopilotStore
from publishing.server import make_handler
from publishing.web_studio import WebStudio
from tests.test_web_studio import (
    Cfg, FakeCol, FakeSessions, ORIGIN, cookies, headers, multipart, photo,
)


class StudioHttpTests(unittest.TestCase):
    """End-to-end through the real stdlib handler (routing, limits, content types)."""

    @classmethod
    def setUpClass(cls):
        cls.buffer = ContentBuffer(FakeCol(), MemoryBlobStore())
        cls.autopilot = AutopilotStore(FakeCol())
        studio = WebStudio(Cfg(), FakeSessions(), cls.buffer, cls.autopilot)
        handler = make_handler(Cfg(), None, None, None, None, web_studio=studio)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.port = cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def req(self, method, path, body=None, who="alice", ctype=None, **kw):
        h = headers(who, ctype or "application/json", **kw)
        h["Cookie"] = cookies(who)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=body, headers=h)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        return res.status, res.getheader("Content-Type"), json.loads(data or b"{}")

    def test_upload_state_autopilot_delete_roundtrip(self):
        body, ctype = multipart([("pic.jpg", photo(41))])
        status, out_type, data = self.req("POST", "/web/api/media", body, ctype=ctype)
        self.assertEqual((status, data["added"]), (201, 1))
        self.assertEqual(out_type, "application/json")

        status, _, state = self.req("GET", "/web/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(len(state["media"]), 1)
        self.assertFalse(state["posting_engine"])

        status, _, state = self.req("POST", "/web/api/autopilot", json.dumps({"enabled": True}))
        self.assertTrue(state["autopilot"]["active"])

        status, _, other = self.req("GET", "/web/api/state", who="bob")
        self.assertEqual(other["media"], [])

        media_id = state["media"][0]["id"]
        status, _, _ = self.req("POST", "/web/api/media/delete", json.dumps({"id": media_id}), who="bob")
        self.assertEqual(status, 404)
        status, _, _ = self.req("POST", "/web/api/media/delete", json.dumps({"id": media_id}))
        self.assertEqual(status, 200)

    def test_wrong_content_type_and_forged_origin_are_rejected(self):
        status, _, _ = self.req("POST", "/web/api/autopilot", "enabled=1",
                                ctype="application/x-www-form-urlencoded")
        self.assertEqual(status, 415)
        status, _, _ = self.req("POST", "/web/api/autopilot", json.dumps({"enabled": True}),
                                origin="https://evil.test")
        self.assertEqual(status, 403)

    def test_json_body_size_limit(self):
        status, _, _ = self.req("POST", "/web/api/autopilot", json.dumps({"slots": ["12:00"] * 5000}))
        self.assertEqual(status, 413)

    def test_state_requires_session(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/web/api/state")
        self.assertEqual(conn.getresponse().status, 401)


if __name__ == "__main__":
    unittest.main()
