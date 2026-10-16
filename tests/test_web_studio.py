import io
import json
import random
import unittest
from datetime import datetime, timezone

from PIL import Image

from content_buffer import ContentBuffer, MemoryBlobStore, READY
from publishing.autopilot import AutopilotStore
from publishing.identity import Principal
from publishing.web_pipeline import WebIngest, WebPipeline
from publishing.web_studio import WebStudio, to_clean_jpeg

ORIGIN = "https://app.example.test"


# ---------------------------------------------------------------- tiny Mongo fake
def matches(doc, query):
    for key, exp in query.items():
        if key == "$and":
            if not all(matches(doc, q) for q in exp):
                return False
        elif key == "$or":
            if not any(matches(doc, q) for q in exp):
                return False
        elif isinstance(exp, dict) and any(k.startswith("$") for k in exp):
            val = doc.get(key)
            for op, arg in exp.items():
                if op == "$exists" and (key in doc) != arg:
                    return False
                if op == "$in" and val not in arg:
                    return False
                if op == "$ne" and val == arg:
                    return False
                if op == "$gte" and not (val is not None and val >= arg):
                    return False
                if op == "$regex":
                    import re
                    if not (isinstance(val, str) and re.search(arg, val)):
                        return False
        elif doc.get(key) != exp:
            return False
    return True


class Res:
    def __init__(self, matched=0, deleted=0):
        self.matched_count, self.deleted_count = matched, deleted


class FakeCol:
    def __init__(self):
        self.rows = []

    def create_index(self, *a, **k):
        pass

    def insert_one(self, doc):
        self.rows.append(doc)

    def find_one(self, query):
        return next((r for r in self.rows if matches(r, query)), None)

    def find(self, query=None):
        return [r for r in self.rows if matches(r, query or {})]

    def count_documents(self, query):
        return len(self.find(query))

    def delete_one(self, query):
        row = self.find_one(query)
        if row:
            self.rows.remove(row)
        return Res(deleted=1 if row else 0)

    def update_one(self, query, update, upsert=False):
        row = self.find_one(query)
        if row is None:
            if not upsert:
                return Res()
            row = {"_id": "doc-%d" % len(self.rows)}
            row.update({k: v for k, v in query.items() if not k.startswith("$")})
            row.update(update.get("$setOnInsert", {}))
            self.rows.append(row)
        row.update(update.get("$set", {}))
        for k, v in update.get("$push", {}).items():
            row.setdefault(k, []).append(v)
        for k in update.get("$unset", {}):
            row.pop(k, None)
        return Res(matched=1)


class FakeSessions:
    def resolve(self, token):
        return {"tok-alice": Principal.from_web("alice"),
                "tok-bob": Principal.from_web("bob")}.get(token)

    def validate_csrf(self, token, csrf):
        return csrf == "csrf-" + token.split("-", 1)[1]


class Cfg:
    public_base_url = ORIGIN


# ------------------------------------------------------------------ helpers
def photo(seed=1, fmt="JPEG", size=(900, 1100)):
    rnd = random.Random(seed)
    img = Image.new("RGB", size)
    px = img.load()
    for x in range(0, size[0], 6):
        for y in range(0, size[1], 6):
            c = (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
            for dx in range(6):
                for dy in range(6):
                    if x + dx < size[0] and y + dy < size[1]:
                        px[x + dx, y + dy] = c
    out = io.BytesIO()
    img.save(out, fmt)
    return out.getvalue()


def multipart(files):
    boundary = "----vibeosboundary"
    body = b""
    for name, data in files:
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; "
                 f"filename=\"{name}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode()
        body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def cookies(who):
    return f"__Host-vibeos_session=tok-{who}; __Host-vibeos_csrf=csrf-{who}"


def headers(who, ctype="application/json", origin=ORIGIN, csrf=None):
    return {"Origin": origin, "X-CSRF-Token": csrf or f"csrf-{who}", "Content-Type": ctype}


class FakePipeline:
    """Stands in for the AI: marks a selected asset ready without network calls."""

    def __init__(self, buffer):
        self.buffer = buffer
        self.notes = {}

    def prepare(self, principal, asset_id):
        self.buffer.prepare_ready(
            asset_id, "caption", ["#vibe"], "natural", ("post", "story"),
            lambda data, _v, size: data)
        return "ready"

    def note(self, asset_id, **fields):
        self.buffer.assets.update_one({"_id": asset_id}, {"$set": fields})


class StudioCase(unittest.TestCase):
    def setUp(self):
        self.assets = FakeCol()
        self.buffer = ContentBuffer(self.assets, MemoryBlobStore())
        self.autopilot = AutopilotStore(FakeCol())
        self.pipeline = FakePipeline(self.buffer)
        self.ingest = WebIngest(self.buffer, self.autopilot, self.pipeline)
        self.studio = WebStudio(Cfg(), FakeSessions(), self.buffer, self.autopilot,
                                ingest=self.ingest)
        self.submitted = []
        self.ingest.submit = lambda p, b: self.submitted.append((p.owner_id, b))

    def upload(self, who, files, **kw):
        body, ctype = multipart(files)
        return self.studio.upload(cookies(who), headers(who, ctype, **kw), body)

    def state(self, who):
        status, body, _ = self.studio.state(cookies(who))
        return status, json.loads(body)

    def post_autopilot(self, who, payload, **kw):
        status, body, _ = self.studio.set_autopilot(
            cookies(who), headers(who, **kw), json.dumps(payload).encode())
        return status, json.loads(body)


class UploadTests(StudioCase):
    def test_signed_out_user_cannot_upload_or_read(self):
        body, ctype = multipart([("a.jpg", photo())])
        status, out, _ = self.studio.upload("", headers("alice", ctype), body)
        self.assertEqual(status, 401)
        self.assertEqual(self.studio.state("")[0], 401)

    def test_upload_requires_matching_origin_and_csrf(self):
        self.assertEqual(self.upload("alice", [("a.jpg", photo())], origin="https://evil.test")[0], 403)
        self.assertEqual(self.upload("alice", [("a.jpg", photo())], csrf="wrong")[0], 403)
        self.assertEqual(self.assets.rows, [])

    def test_upload_stores_owner_scoped_clean_jpeg(self):
        status, out, _ = self.upload("alice", [("a.png", photo(1, "PNG"))])
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(out)["added"], 1)
        row = self.assets.rows[0]
        self.assertEqual(row["owner_id"], "web:alice")
        self.assertNotIn("telegram_id", row)
        self.assertTrue(self.buffer.blobs.get(row["blob_id"]).startswith(b"\xff\xd8\xff"))
        self.assertEqual(self.submitted[0][0], "web:alice")

    def test_non_image_and_empty_uploads_are_rejected(self):
        self.assertEqual(self.upload("alice", [("x.jpg", b"not an image")])[0], 400)
        self.assertEqual(self.upload("alice", [("x.jpg", b"")])[0], 400)
        self.assertEqual(self.assets.rows, [])

    def test_same_photo_twice_is_a_duplicate(self):
        data = photo(2)
        self.upload("alice", [("a.jpg", data)])
        status, out, _ = self.upload("alice", [("b.jpg", data)])
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(out)["duplicates"], 1)
        self.assertEqual(len(self.assets.rows), 1)

    def test_too_many_files_rejected(self):
        files = [("p%d.jpg" % i, photo(i)) for i in range(11)]
        self.assertEqual(self.upload("alice", files)[0], 413)

    def test_exif_is_stripped(self):
        img = Image.new("RGB", (64, 64), (120, 40, 200))
        exif = Image.Exif()
        exif[0x010F] = "SecretCameraMaker"
        buf = io.BytesIO()
        img.save(buf, "JPEG", exif=exif)
        self.assertIn(b"SecretCameraMaker", buf.getvalue())
        self.assertNotIn(b"SecretCameraMaker", to_clean_jpeg(buf.getvalue()))


class TenantIsolationTests(StudioCase):
    def test_users_only_see_their_own_photos(self):
        self.upload("alice", [("alice.jpg", photo(10))])
        self.upload("bob", [("bob.jpg", photo(11))])
        _, a = self.state("alice")
        _, b = self.state("bob")
        self.assertEqual([m["name"] for m in a["media"]], ["alice.jpg"])
        self.assertEqual([m["name"] for m in b["media"]], ["bob.jpg"])

    def test_cannot_delete_someone_elses_photo(self):
        self.upload("alice", [("alice.jpg", photo(12))])
        alice_id = self.assets.rows[0]["_id"]
        status, _, _ = self.studio.delete_media(
            cookies("bob"), headers("bob"), json.dumps({"id": alice_id}).encode())
        self.assertEqual(status, 404)
        self.assertEqual(len(self.assets.rows), 1)
        status, _, _ = self.studio.delete_media(
            cookies("alice"), headers("alice"), json.dumps({"id": alice_id}).encode())
        self.assertEqual(status, 200)
        self.assertEqual(self.assets.rows, [])

    def test_web_assets_invisible_to_telegram_ids_and_vice_versa(self):
        self.upload("alice", [("alice.jpg", photo(13))])
        self.buffer.add_asset("123456", photo(14), "photo", "b1", "tg.jpg")
        self.assertEqual(self.buffer.list_assets("123456")[0]["filename"], "tg.jpg")
        self.assertEqual(len(self.buffer.list_assets("123456")), 1)
        self.assertEqual(len(self.buffer.list_assets(Principal.from_web("alice"))), 1)
        self.assertEqual(self.buffer.list_assets(Principal.from_web("bob")), [])
        self.assertFalse(self.buffer.delete_asset("123456", self.buffer.list_assets(Principal.from_web("alice"))[0]["_id"]))

    def test_autopilot_is_per_user(self):
        self.post_autopilot("alice", {"enabled": True, "daily_limit": 4})
        self.assertTrue(self.state("alice")[1]["autopilot"]["active"])
        self.assertFalse(self.state("bob")[1]["autopilot"]["active"])
        self.assertEqual(self.state("bob")[1]["autopilot"]["daily_limit"], 2)


class AutopilotApiTests(StudioCase):
    def test_toggle_on_and_off(self):
        status, data = self.post_autopilot("alice", {"enabled": True})
        self.assertEqual(status, 200)
        self.assertTrue(data["autopilot"]["active"])
        s = self.autopilot.get(Principal.from_web("alice"))
        self.assertTrue(s["enabled"] and s["full_auto"])
        status, data = self.post_autopilot("alice", {"enabled": False})
        self.assertFalse(data["autopilot"]["active"])
        self.assertFalse(self.autopilot.get(Principal.from_web("alice"))["full_auto"])

    def test_turning_on_processes_photos_uploaded_while_off(self):
        calls = []
        self.ingest.submit_pending = lambda p: calls.append(p.owner_id)
        self.post_autopilot("alice", {"enabled": True})
        self.assertEqual(calls, ["web:alice"])

    def test_validates_input_and_rejects_unknown_keys(self):
        self.assertEqual(self.post_autopilot("alice", {"daily_limit": 99})[0], 400)
        self.assertEqual(self.post_autopilot("alice", {"slots": ["25:99"]})[0], 400)
        self.assertEqual(self.post_autopilot("alice", {"timezone": "Mars/Base"})[0], 400)
        self.assertEqual(self.post_autopilot("alice", {"full_auto": True})[0], 400)
        self.assertEqual(self.post_autopilot("alice", {"formats": ["reel"]})[0], 400)
        self.assertEqual(self.post_autopilot("alice", {})[0], 400)

    def test_saves_limit_slots_quiet_and_timezone(self):
        status, data = self.post_autopilot("alice", {
            "daily_limit": 3, "slots": ["19:30", "8:05"], "quiet_start": 22,
            "quiet_end": 6, "timezone": "Asia/Kolkata"})
        self.assertEqual(status, 200)
        ap = data["autopilot"]
        self.assertEqual((ap["daily_limit"], ap["slots"], ap["timezone"]),
                         (3, ["08:05", "19:30"], "Asia/Kolkata"))

    def test_write_routes_need_csrf(self):
        status, _ = self.post_autopilot("alice", {"enabled": True}, csrf="nope")
        self.assertEqual(status, 403)
        self.assertFalse(self.autopilot.get(Principal.from_web("alice"))["enabled"])


class IngestTests(StudioCase):
    def setUp(self):
        super().setUp()
        self.ingest = WebIngest(self.buffer, self.autopilot, self.pipeline)   # real submit
        alice = Principal.from_web("alice")
        self.alice = alice
        self.batch = "web-batch-1"
        self.buffer.add_asset(alice, photo(21), "photo", self.batch, "one.jpg", source="web")

    def test_autopilot_off_leaves_photo_in_buffer(self):
        self.ingest.process_batch(self.alice, self.batch)
        self.assertEqual(self.buffer.list_assets(self.alice)[0]["status"], "uploaded")

    def test_autopilot_on_makes_photo_ready(self):
        self.autopilot.set_web_autopilot(self.alice, True)
        out = self.ingest.process_batch(self.alice, self.batch)
        self.assertEqual(out["ready"], 1)
        row = self.buffer.list_assets(self.alice)[0]
        self.assertEqual(row["status"], READY)
        self.assertEqual(row["owner_id"], "web:alice")
        self.assertIn("post", row["creative"]["variants"])

    def test_paused_user_is_not_processed(self):
        self.autopilot.set_web_autopilot(self.alice, True)
        self.autopilot.update(self.alice, paused=True)
        self.ingest.process_batch(self.alice, self.batch)
        self.assertEqual(self.buffer.list_assets(self.alice)[0]["status"], "uploaded")

    def test_pending_batches_found_for_catch_up(self):
        self.assertEqual(self.ingest._pending_batches(self.alice), [self.batch])
        self.assertEqual(self.ingest._pending_batches(Principal.from_web("bob")), [])

    def test_near_duplicate_of_posted_photo_is_not_prepared_again(self):
        self.autopilot.set_web_autopilot(self.alice, True)
        self.ingest.process_batch(self.alice, self.batch)
        self.buffer.add_asset(self.alice, photo(21, size=(901, 1101)), "photo", "web-batch-2", "dup.jpg")
        out = self.ingest.process_batch(self.alice, "web-batch-2")
        self.assertEqual(out["ready"], 0)


class PipelineScoringTests(unittest.TestCase):
    def test_low_score_photo_is_skipped_not_prepared(self):
        assets = FakeCol()
        buffer = ContentBuffer(assets, MemoryBlobStore())
        alice = Principal.from_web("alice")
        res = buffer.add_asset(alice, photo(31), "photo", "b", "x.jpg")
        buffer.analyze(res["asset"]["_id"])
        buffer.select_shortlist(alice, "b", n=1)
        pipe = WebPipeline(buffer, lambda: None, "m", edit_photo=lambda *a, **k: None, styles=["natural"])
        pipe.score_and_style = lambda b64, vibe="natural": {"score": 10, "style": "natural"}
        self.assertEqual(pipe.prepare(alice, res["asset"]["_id"]), "skipped")
        row = buffer.list_assets(alice)[0]
        self.assertEqual((row["status"], row["web_note"]), ("analyzed", "low_score"))


if __name__ == "__main__":
    unittest.main()
