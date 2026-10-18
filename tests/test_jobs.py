"""Run with:  python -m unittest tests.test_jobs -v   (no MongoDB, no network)."""
import io
import os
import shutil
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import fakemongo  # noqa: E402

db = fakemongo.DB()
fakemongo.install(db)

import jobs  # noqa: E402
import worker  # noqa: E402
from content_buffer import ContentBuffer, GridFSBlobStore, SELECTED, READY  # noqa: E402
from PIL import Image  # noqa: E402


def jpeg(w, h, color=(120, 90, 200)):
    out = io.BytesIO()
    Image.new("RGB", (w, h), color).save(out, "JPEG", quality=90)
    return out.getvalue()


def run_worker_once(types, db_=None):
    """What worker.main() does for one job, but in-process."""
    job = jobs.claim(db_ or db, types)
    if job is None:
        return None
    worker.run_one(job["_id"])
    return job["_id"]


class QueueTests(unittest.TestCase):
    def setUp(self):
        db["media_jobs"].docs.clear()
        worker._db = lambda: db

    def test_claim_is_exclusive_and_ordered(self):
        a = jobs.enqueue(db, "edit", {}); b = jobs.enqueue(db, "edit", {})
        first = jobs.claim(db, ["edit"]); second = jobs.claim(db, ["edit"])
        self.assertEqual([first["_id"], second["_id"]], [a, b])
        self.assertIsNone(jobs.claim(db, ["edit"]))
        self.assertEqual(first["attempts"], 1)

    def test_claim_respects_types(self):
        jobs.enqueue(db, "reel", {})
        self.assertIsNone(jobs.claim(db, ["edit", "prepare_ready"]))
        self.assertIsNotNone(jobs.claim(db, ["reel"]))

    def test_expired_lease_is_reclaimed(self):
        jid = jobs.enqueue(db, "edit", {})
        jobs.claim(db, ["edit"], lease_seconds=-1)           # worker "died"
        again = jobs.claim(db, ["edit"])
        self.assertEqual((again["_id"], again["attempts"]), (jid, 2))

    def test_three_failures_mark_failed(self):
        jid = jobs.enqueue(db, "edit", {})
        states = []
        for _ in range(3):
            job = jobs.claim(db, ["edit"])
            states.append(jobs.fail(db, job, "boom"))
        self.assertEqual(states, [jobs.QUEUED, jobs.QUEUED, jobs.FAILED])
        self.assertIsNone(jobs.claim(db, ["edit"]))
        with self.assertRaises(jobs.JobFailed):
            jobs.wait(db, jid, timeout=1)

    def test_worker_that_dies_on_last_attempt_is_reaped(self):
        jid = jobs.enqueue(db, "edit", {})
        for _ in range(3):
            jobs.claim(db, ["edit"], lease_seconds=-1)
        self.assertEqual(jobs.reap_exhausted(db, ["edit"]), 1)
        self.assertEqual(jobs.get(db, jid)["status"], jobs.FAILED)

    def test_wait_times_out(self):
        jid = jobs.enqueue(db, "edit", {})
        with self.assertRaises(jobs.JobTimeout):
            jobs.wait(db, jid, timeout=0.3, poll=0.05)

    def test_heartbeat(self):
        self.assertFalse(jobs.worker_available(db, "reel", cap="ffmpeg"))
        jobs.heartbeat(db, "w1", ["reel"], {"ffmpeg": True})
        self.assertTrue(jobs.worker_available(db, "reel", cap="ffmpeg"))
        self.assertFalse(jobs.worker_available(db, "edit"))

    def test_scratch_blob_sweep(self):
        old = jobs.put_blob(db, b"x"); new = jobs.put_blob(db, b"y")
        for f in db["jobblobs.files"].docs:
            if f["_id"] == old:
                f["uploadDate"] = datetime.now(timezone.utc) - timedelta(hours=7)
        self.assertGreaterEqual(jobs.cleanup_blobs(db), 1)
        self.assertNotIn(old, db.blobs["jobblobs"])
        self.assertIn(new, db.blobs["jobblobs"])      # fresh blob untouched


class HandlerTests(unittest.TestCase):
    def setUp(self):
        db["media_jobs"].docs.clear()
        db["assets"].docs.clear()
        worker._db = lambda: db

    def test_edit_job_roundtrip(self):
        in_blob = jobs.put_blob(db, jpeg(3000, 2000))
        jid = jobs.enqueue(db, "edit", {"in_blob": in_blob, "style": "golden", "size": "post",
                                        "text": "hello", "text_style": "bold", "text_position": "bottom"})
        self.assertEqual(run_worker_once(["edit"]), jid)
        res = jobs.wait(db, jid, timeout=1)
        out = Image.open(io.BytesIO(jobs.get_blob(db, res["blob_id"])))
        self.assertEqual(out.size, (1080, 1350))
        self.assertNotIn(in_blob, db.blobs["jobblobs"])      # input cleaned up

    def test_failing_handler_goes_back_to_queue_via_parent_logic(self):
        jid = jobs.enqueue(db, "edit", {"in_blob": "does-not-exist", "style": "natural"})
        job = jobs.claim(db, ["edit"])
        self.assertEqual(worker.run_one(job["_id"]), 1)      # child exits 1 ...
        self.assertEqual(jobs.fail(db, jobs.get(db, jid), "x"), jobs.QUEUED)   # ... parent re-queues

    def _buffer(self):
        return ContentBuffer(db["assets"], GridFSBlobStore(db))

    def test_upload_is_downscaled_to_2048(self):
        buf = self._buffer()
        res = buf.add_asset("42", jpeg(4000, 3000), "photo", "b1", "big.jpg")
        stored = Image.open(io.BytesIO(buf.get_media(res["asset"]["_id"])))
        self.assertEqual(max(stored.size), 2048)
        small = buf.add_asset("42", jpeg(800, 600, (1, 2, 3)), "photo", "b1", "small.jpg")
        self.assertEqual(Image.open(io.BytesIO(buf.get_media(small["asset"]["_id"]))).size, (800, 600))
        dup = buf.add_asset("42", jpeg(4000, 3000), "photo", "b1", "big.jpg")
        self.assertTrue(dup["duplicate"])

    def test_prepare_ready_job_end_to_end(self):
        buf = self._buffer()
        a = buf.add_asset("42", jpeg(2500, 1800), "photo", "b1", "p.jpg")["asset"]["_id"]
        db["assets"].update_one({"_id": a}, {"$set": {"status": SELECTED}})
        stop = threading.Event()

        def fake_worker():
            while not stop.is_set():
                if run_worker_once(["prepare_ready"]) is None:
                    time.sleep(0.05)
        t = threading.Thread(target=fake_worker, daemon=True); t.start()
        try:
            asset = buf.prepare_ready_job(a, "cap", ["#a"], "golden", ("post", "story"),
                                          style="golden", overlay="hi", text_style="minimal", timeout=60)
        finally:
            stop.set(); t.join(2)
        self.assertEqual(asset["status"], READY)
        self.assertEqual(Image.open(io.BytesIO(buf.get_media(a, "story"))).size, (1080, 1920))
        self.assertEqual(Image.open(io.BytesIO(buf.get_media(a, "post"))).size, (1080, 1350))

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_reel_job(self):
        buf = self._buffer()
        ids = []
        for i in range(2):
            aid = buf.add_asset("42", jpeg(1500, 1500, (i * 90, 50, 100)), "photo", "b1", f"{i}.jpg")["asset"]["_id"]
            blob = buf.blobs.put(jpeg(1080, 1920, (i * 90, 50, 100)))
            db["assets"].update_one({"_id": aid}, {"$set": {"status": READY, "creative": {"variants": {"story": blob}}}})
            ids.append(aid)
        jid = jobs.enqueue(db, "reel", {"principal": {"kind": "telegram", "subject": "42"}, "asset_ids": ids,
                                        "caption": "c", "hashtags": ["#x"], "vibe": "natural", "batch_id": "b1"})
        run_worker_once(["reel"])
        res = jobs.wait(db, jid, timeout=1)
        reel = db["assets"].find_one({"_id": res["reel_id"]})
        self.assertEqual((reel["kind"], reel["status"]), ("video", READY))
        self.assertGreater(reel["analysis"]["video"]["height"], reel["analysis"]["video"]["width"])


if __name__ == "__main__":
    unittest.main()


class TrendStoryJobTests(unittest.TestCase):
    """auto_story.post_one now renders in the worker; this process only enqueues + waits."""

    def setUp(self):
        db["media_jobs"].docs.clear(); db["assets"].docs.clear()
        worker._db = lambda: db

    def _item(self, now):
        news = [{"source": s, "title": "India wins cricket world cup final", "url": "https://x.test/" + s,
                 "snippet": ""} for s in ("NDTV", "The Hindu", "HT")]
        return {"title": "India cricket world cup", "approx_traffic_numeric": 500000, "news": news,
                "expires_at": now + timedelta(hours=3), "category": "sports",
                "source_url": "https://x.test/trend", "source_name": "google_trends", "confidence": 0.9}

    def test_post_one_enqueues_renders_and_queues_story(self):
        from trends import auto_story
        from publishing.identity import Principal
        buf = ContentBuffer(db["assets"], GridFSBlobStore(db))
        now = datetime.now(timezone.utc)
        calls = []

        class Svc:
            jobs = db["publish_jobs"]
            def enqueue(self, *a, **k):
                calls.append((a, k)); db["publish_jobs"].insert_one({"_id": "pj1"}); return {"_id": "pj1"}
            enqueue_for = enqueue
        cfg = auto_story.TrendStoryConfig(fetch=lambda: [], smart=True, ai=None)
        stop = threading.Event()

        def fake_worker():
            while not stop.is_set():
                if run_worker_once(["smart_story", "story_card"]) is None:
                    time.sleep(0.05)
        t = threading.Thread(target=fake_worker, daemon=True); t.start()
        try:
            done = auto_story.post_one(Principal.from_telegram("42"), [self._item(now)], cfg=cfg,
                                       settings={"blocked_topics": []}, buffer=buf, service=Svc(), now=now)
        finally:
            stop.set(); t.join(3)
        self.assertIsNotNone(done, "story should have been built by the worker and queued")
        asset = db["assets"].find_one({"_id": done["asset_id"]})
        img = Image.open(io.BytesIO(buf.get_media(asset["_id"])))
        self.assertEqual(img.size, (1080, 1920))
        self.assertEqual(len(calls), 1)

    def test_post_one_gives_up_quickly_when_no_worker(self):
        from trends import auto_story
        from publishing.identity import Principal
        buf = ContentBuffer(db["assets"], GridFSBlobStore(db))
        now = datetime.now(timezone.utc)
        auto_story.STORY_JOB_TIMEOUT = 0.4
        try:
            cfg = auto_story.TrendStoryConfig(fetch=lambda: [], smart=True, ai=None)
            items = [self._item(now), {**self._item(now), "title": "Another cricket trend"}]
            t0 = time.monotonic()
            res = auto_story.post_one(Principal.from_telegram("43"), items, cfg=cfg,
                                      settings={}, buffer=buf, service=None, now=now)
            self.assertIsNone(res)
            self.assertLess(time.monotonic() - t0, 2)     # one wait, not one per trend
        finally:
            auto_story.STORY_JOB_TIMEOUT = 90
