"""
publishing/scheduler.py — the always-on worker (Phase 3).

One background thread, every `interval` seconds:
  1. run due jobs      (service.due_jobs(): queued/retry whose time has come, plus
                        `running` jobs whose lease expired = process died mid-run)
  2. tell the user     about outcomes that need attention (once per status)
  3. autofill          (only if /autopilot on) put ready photos into calendar slots
  4. token care        refresh tokens that are close to expiry, alert on dead logins

Owners: the worker serves Telegram owners and, once enabled (`web_owners=True`),
web owners. Every per-user step works on a verified `Principal` (never a bare
Telegram ID), so one user's photos, tokens, slots and jobs can't leak into
another's. Web owners get no Telegram messages; their dashboard shows status.

Safety does not live here: the claim lease, idempotency key, container resume and
policy gate are all inside PublishService, so even two workers (or a restart in
the middle of a publish) can't post twice. The worker only decides *when to look*.
"""
import logging
import random
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autonomous.router import plan_routes

from . import service as svc
from .accounts import NotConnected, ReauthRequired
from .identity import Principal, document_principal
from .service import PublishError

logger = logging.getLogger(__name__)

NOTIFY_ON = {svc.PUBLISHED, svc.FAILED, svc.HELD, svc.NEEDS_REAUTH, svc.NEEDS_ATTENTION}
ALERT_EXPIRY_DAYS = 7


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Worker:
    def __init__(self, service, accounts, autopilot, calendar, buffer, notify, *,
                 interval=10.0, autofill_every=60.0, token_check_every=1800.0,
                 batch=5, refresh_days=10, clock=_now, planner=None,
                 reel_audio_dir=None, trend_cfg=None, trend_every=1800.0,
                 engagement_ai=None, engagement_events=None, engagement_every=300.0,
                 web_owners=False):
        self.service, self.accounts, self.autopilot = service, accounts, autopilot
        # One switch for the whole stack: the worker and the service must agree on
        # whether web-owned jobs may run. False keeps everything Telegram-only.
        self.handles_web_owners = bool(web_owners)
        self.service.handles_web_owners = self.handles_web_owners
        self.calendar, self.buffer, self.notify = calendar, buffer, notify
        self.planner = planner
        self.reel_audio_dir = reel_audio_dir   # optional folder of audio YOU have rights to
        self._reel_blocked = {}                # owner_id -> retry-after time after a failed render
        self.interval, self.batch, self.refresh_days = interval, batch, refresh_days
        self.trend_cfg = trend_cfg             # trends.auto_story.TrendStoryConfig or None
        self.engagement_ai = engagement_ai
        self.engagement_events = engagement_events
        if self.engagement_events is not None:
            self._ensure_engagement_indexes()
        self._engagement_last = None
        self.engagement_every = engagement_every
        self.every = {"autofill": autofill_every, "tokens": token_check_every,
                      "trends": trend_every}
        self.clock = clock
        self._last = {"autofill": None, "tokens": None, "trends": None}
        self._stop = threading.Event()
        self._thread = None

    # ---- lifecycle -------------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        orphans = [j for j in self.service.jobs.find({"status": svc.RUNNING})]
        if orphans:
            logger.warning("%d job(s) were mid-run at startup; they resume when their "
                           "lease expires (Instagram is asked first, nothing is re-posted).",
                           len(orphans))
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="vibeos-worker", daemon=True)
        self._thread.start()
        logger.info("Publishing worker started (every %.0fs).", self.interval)

    def stop(self, timeout=15):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("Worker tick failed; continuing")
            self._stop.wait(self.interval)

    # ---- one pass --------------------------------------------------------
    def tick(self):
        ran = []
        for job in self.service.due_jobs()[:self.batch]:
            if self._stop.is_set():
                break
            done = self.service.run(job["_id"])
            if not done:
                continue
            self._announce(done)        # idempotent: tells the user once per status
            if self._progressed(job, done):
                ran.append((done["_id"], done["status"]))
        if self._due("autofill"):
            self._guard(self._expire_trend_jobs)
            self._guard(self._autofill)
        if self.trend_cfg is not None and self._due("trends"):
            self._guard(self._trend_stories)
        if self.engagement_ai is not None and self._engagement_due():
            self._guard(self._engagement)
        if self._due("tokens"):
            self._guard(self._check_tokens)
        return ran

    @staticmethod
    def _progressed(before, after):
        """False when the job came back untouched (paused / not yet allowed)."""
        keys = ("status", "attempts", "next_attempt_at", "container_id", "media_id")
        return any(before.get(k) != after.get(k) for k in keys)

    def _due(self, name):
        now = self.clock()
        last = self._last[name]
        if last is None or (now - last).total_seconds() >= self.every[name]:
            self._last[name] = now
            return True
        return False

    @staticmethod
    def _guard(fn):
        try:
            fn()
        except Exception:
            logger.exception("Worker task %s failed", fn.__name__)

    def _principals(self):
        """Owners with any autopilot feature on (web owners only when enabled)."""
        return self.autopilot.enabled_principals(include_web=self.handles_web_owners)

    def _job_principal(self, job):
        """Verified owner of a job, or None (unknown / web while disabled)."""
        principal = document_principal(job)
        if principal is None or (principal.kind == "web" and not self.handles_web_owners):
            return None
        return principal

    def _ensure_engagement_indexes(self):
        """Idempotency markers are unique per owner. The old Telegram index was a
        plain unique index, which would make every web marker share one `null`
        telegram_id; make it partial and add the matching web index."""
        col = self.engagement_events
        try:
            if hasattr(col, "list_indexes"):
                for index in list(col.list_indexes()):
                    if (dict(index.get("key", {})) == {"telegram_id": 1, "event_key": 1}
                            and not index.get("partialFilterExpression")):
                        col.drop_index(index["name"])
            col.create_index([("telegram_id", 1), ("event_key", 1)], unique=True,
                             partialFilterExpression={"telegram_id": {"$exists": True}})
            col.create_index([("owner_id", 1), ("event_key", 1)], unique=True,
                             partialFilterExpression={"owner_id": {"$exists": True}})
        except Exception:
            logger.exception("Could not prepare engagement indexes")

    # ---- telling the user ------------------------------------------------
    def _announce(self, job):
        st = job["status"]
        if st not in NOTIFY_ON or job.get("notified") == st:
            return
        principal = document_principal(job)
        if principal is None:
            return
        short = job["_id"][:6]
        label = job["format"]
        if st == svc.PUBLISHED:
            text = f"✅ Your {label} was published." + (f"\n{job['permalink']}" if job.get("permalink") else "")
        elif st == svc.HELD:
            text = (f"⏸ I held a {label} post: {job.get('hold_reason')}.\n"
                    f"/release {short} to post it anyway, or /cancel {short}.")
        elif st == svc.NEEDS_REAUTH:
            text = "🔑 Instagram login expired, so a post is waiting. Use /connect, then retry from /queue."
        elif st == svc.NEEDS_ATTENTION:
            text = "⚠️ " + (job.get("last_error") or "A post needs your attention.")
        else:
            text = f"❌ A {label} post failed: {job.get('last_error') or 'unknown error'}\nSee /queue."
        self._tell(principal, text)
        self.service.jobs.update_one({"_id": job["_id"]}, {"$set": {"notified": st}})

    def _tell(self, principal, text):
        """Telegram owners get a message; web owners see the same state in the dashboard."""
        if principal.kind == "telegram":
            self._send(principal.subject, text)

    def _send(self, telegram_id, text):
        try:
            self.notify(telegram_id, text)
        except Exception:
            logger.exception("Could not notify %s", telegram_id)

    # ---- trend Stories ---------------------------------------------------
    def _expire_trend_jobs(self):
        """A trend Story that did not go out in time is cancelled, never posted late."""
        now = self.clock()
        for job in self.service.jobs.find({
                "status": {"$in": [svc.QUEUED, svc.RETRY, svc.HELD]},
                "trend_expires_at": {"$lt": now}}):
            principal = self._job_principal(job)
            if principal is None:
                continue
            if self.service.cancel(job["_id"], principal):
                logger.info("Trend Story %s expired before posting; cancelled", job["_id"][:6])

    def _trend_stories(self):
        from trends import auto_story
        cfg = self.trend_cfg
        users = []
        for principal in self._principals():
            s = self.autopilot.get(principal)
            acct = self.accounts.get_for(principal)
            if (s.get("trend_stories") == "auto" and not s["paused"]
                    and "story" in s["formats"] and acct and acct.get("status") == "active"):
                users.append((principal, s))
        if not users:
            return
        items = cfg.fetch()               # one network fetch shared by all users
        if not items:
            return
        for principal, s in users:
            done = auto_story.post_one(principal, items, cfg=cfg, settings=s, buffer=self.buffer,
                                       service=self.service, now=self.clock())
            if done:
                src = ", ".join(done["publishers"][:3]) or "news sources"
                self._tell(principal, f"📰 Trend Story queued: “{done['title']}” ({done['category']}, "
                               f"via {src}). It posts within {cfg.max_age_hours:g}h or is dropped. "
                               "/queue to see it, /cancel to stop it.")

    # ---- AI comment engagement -------------------------------------------
    def _engagement_due(self):
        now = self.clock()
        if self._engagement_last is None or (now - self._engagement_last).total_seconds() >= self.engagement_every:
            self._engagement_last = now
            return True
        return False

    @staticmethod
    def _engagement_owner(principal):
        """Marker fields identifying one owner. Telegram keeps its legacy field."""
        if principal.kind == "telegram":
            return {"telegram_id": principal.subject}
        return {"owner_id": principal.owner_id}

    def _engagement_seen(self, principal, key):
        return bool(self.engagement_events and self.engagement_events.find_one({
            **self._engagement_owner(principal), "event_key": key}))

    def _engagement_mark(self, principal, key, kind, result_id=""):
        if self.engagement_events is None:
            return
        owner = self._engagement_owner(principal)
        try:
            self.engagement_events.update_one(
                {**owner, "event_key": key},
                {"$setOnInsert": {**owner, "event_key": key,
                                  "kind": kind, "result_id": result_id,
                                  "created_at": self.clock()}}, upsert=True)
        except Exception:
            logger.exception("Could not store engagement idempotency marker")

    def _engagement(self):
        """Reply to recent comments on the user's own posts; never comment on strangers' posts."""
        if self.engagement_events is None:
            return
        from datetime import datetime
        for tg in self._principals():
            settings = self.autopilot.get(tg)
            if settings.get("paused") or not (settings.get("auto_replies") or settings.get("auto_comments")):
                continue
            acct = self.accounts.get_for(tg)
            if not acct or acct.get("status") != "active":
                continue
            try:
                ig_id, token = self.accounts.get_token_for(tg)
                username = str(acct.get("username") or "").lstrip("@").lower()
                media = self.accounts.graph.list_media(ig_id, token, limit=10)
            except Exception:
                logger.exception("Could not load Instagram media for engagement owner %s", tg.owner_id)
                continue
            replies_sent = 0
            starter_sent = False
            recent_cutoff = self.clock() - timedelta(hours=24)
            try:
                replies_today = self.engagement_events.count_documents({
                    **self._engagement_owner(tg), "kind": "comment_reply",
                    "created_at": {"$gte": recent_cutoff}})
            except Exception:
                replies_today = 0
            for post in media:
                media_id = str(post.get("id") or "")
                if not media_id:
                    continue
                if settings.get("auto_comments") and not starter_sent:
                    comment_key = "starter:" + media_id
                    # Only add a starter comment to a post published in the last 45 minutes.
                    try:
                        stamp = datetime.fromisoformat(str(post.get("timestamp", "")).replace("Z", "+00:00"))
                        if stamp.tzinfo is None:
                            stamp = stamp.replace(tzinfo=timezone.utc)
                        recent = (self.clock() - stamp).total_seconds() <= 2700
                    except (ValueError, TypeError):
                        recent = False
                    if recent and not self._engagement_seen(tg, comment_key):
                        try:
                            caption = str(post.get("caption") or "")[:1000]
                            text = self.engagement_ai.generate_reply(
                                incoming_text="Write a friendly first comment for this post.",
                                post_context=caption, tone="natural", username=username, max_chars=220)
                            result = self.accounts.graph.create_comment(media_id, text, token)
                            self._engagement_mark(tg, comment_key, "starter_comment", str(result.get("id") or ""))
                            starter_sent = True
                        except Exception:
                            logger.exception("AI starter comment failed for media %s", media_id)
                if not settings.get("auto_replies"):
                    continue
                try:
                    comments = self.accounts.graph.list_comments(media_id, token, limit=50)
                except Exception:
                    logger.exception("Could not list comments for media %s", media_id)
                    continue
                for comment in comments:
                    if replies_sent >= 3 or replies_today >= 10:
                        break
                    cid = str(comment.get("id") or "")
                    text_in = str(comment.get("text") or "").strip()
                    commenter = str(comment.get("username") or "").lstrip("@").lower()
                    if not cid or not text_in or (username and commenter == username):
                        continue
                    key = "reply:" + cid
                    if self._engagement_seen(tg, key):
                        continue
                    # Only automatically respond to comments from the last 24 hours.
                    try:
                        stamp = datetime.fromisoformat(str(comment.get("timestamp", "")).replace("Z", "+00:00"))
                        if stamp.tzinfo is None:
                            stamp = stamp.replace(tzinfo=timezone.utc)
                        if (self.clock() - stamp).total_seconds() > 86400:
                            continue
                    except (ValueError, TypeError):
                        continue
                    try:
                        reply = self.engagement_ai.generate_reply(
                            incoming_text=text_in, post_context=str(post.get("caption") or "")[:1000],
                            tone="friendly", username=username, max_chars=500)
                        result = self.accounts.graph.reply_to_comment(cid, reply, token)
                        self._engagement_mark(tg, key, "comment_reply", str(result.get("id") or ""))
                        replies_sent += 1
                        replies_today += 1
                    except Exception:
                        logger.exception("AI reply failed for comment %s", cid)

    # ---- autofill --------------------------------------------------------
    def _detections(self, tg, assets):
        """Cached event detection per asset; empty when no planner/AI is set."""
        out = {}
        if self.planner is None:
            return out
        for asset in assets:
            try:
                out[asset["_id"]] = self.planner.classify_asset(tg, asset)
            except Exception:
                logger.exception("Classification failed for asset %s", asset.get("_id"))
        return out

    def _store_route(self, route):
        """Remember the decision so the same photo is never routed twice."""
        group = route.get("group_id")
        for aid in route["asset_ids"]:
            self.buffer.assets.update_one({"_id": aid}, {"$set": {"route": {
                "format": route["format"], "asset_ids": list(route["asset_ids"]),
                "group_id": group, "decision": route.get("decision") or {}}}})

    def _clear_route(self, route):
        for aid in route["asset_ids"]:
            self.buffer.assets.update_one({"_id": aid}, {"$unset": {"route": ""}})

    def _reels_ready(self, tg):
        until = self._reel_blocked.get(tg.owner_id)
        if until and self.clock() < until:
            return False
        try:
            from creative_studio import reel_engine
            return reel_engine.check_video_tools()["ffmpeg"]
        except Exception:
            return False

    def _pick_audio(self):
        """Random file from reel_audio_dir, if configured. Rights are the owner's job."""
        if not self.reel_audio_dir:
            return None
        files = [p for p in Path(self.reel_audio_dir).glob("*")
                 if p.suffix.lower() in (".mp3", ".m4a", ".aac", ".wav")]
        return random.choice(files) if files else None

    def _build_reel(self, tg, route):
        """Render a Reel from the route's photos (their 9:16 variants).

        Returns a normal {"format": "reel", ...} route, or None on failure. A failed
        render pauses Reel building for this user for an hour instead of retrying
        every minute.
        """
        try:
            from creative_studio import reel_engine
            assets = [self.buffer.assets.find_one({"_id": i}) for i in route["asset_ids"]]
            with tempfile.TemporaryDirectory(prefix="vibeos_reelsrc_") as tmp:
                paths = []
                for n, a in enumerate(assets):
                    p = Path(tmp) / f"{n:02d}.jpg"
                    p.write_bytes(self.buffer.get_media(a["_id"], "story"))
                    paths.append(p)
                out = Path(tmp) / "reel.mp4"
                reel_engine.render_photo_reel(paths, out, audio_path=self._pick_audio())
                meta = reel_engine.probe_video(out)
                data = out.read_bytes()
            best = max(assets, key=lambda a: float((a.get("analysis") or {}).get("quality_score") or 0))
            tags = []
            for a in [best] + [a for a in assets if a is not best]:
                for t in (a.get("creative") or {}).get("hashtags") or []:
                    if t not in tags:
                        tags.append(t)
            creative = best.get("creative") or {}
            res = self.buffer.add_reel(
                tg, data, meta=meta, caption=creative.get("caption", ""), hashtags=tags[:svc.HASHTAG_LIMIT],
                vibe=creative.get("vibe", "natural"), source_ids=route["asset_ids"],
                batch_id=best.get("batch_id"))
        except Exception:
            logger.exception("Reel render failed for %s; pausing Reel building for 1h", tg.owner_id)
            self._reel_blocked[tg.owner_id] = self.clock() + timedelta(hours=1)
            return None
        reel_id = res["asset"]["_id"]
        for aid in route["asset_ids"]:
            self.buffer.assets.update_one({"_id": aid}, {"$set": {"route": {
                "format": "reel_source", "reel_id": reel_id}}})
        return {"format": "reel", "asset_ids": [reel_id], "decision": route.get("decision") or {}}

    def _slot_for(self, tg, route):
        """Event photos use their calendar window; everything else a normal slot."""
        meta = {"event_status": "disabled"}
        slot = None
        decision = route.get("decision") or {}
        if (self.planner is not None and len(route["asset_ids"]) == 1
                and route.get("format") in ("photo", "story")):
            asset = self.buffer.assets.find_one({"_id": route["asset_ids"][0]})
            try:
                slot, meta = self.planner.schedule_for(tg, asset)
            except Exception:
                logger.exception("Content planning failed for %s; using normal slot",
                                 route["asset_ids"][0])
        # A dated poster must not silently drift to a random day if its printed
        # date has passed or that day's calendar is full.
        if slot is None and meta.get("event_status") not in ("printed_date_passed", "date_full"):
            slot = self.calendar.next_slot(tg)
        return slot, meta

    PREPARING_WINDOW = timedelta(minutes=10)

    def _batches_still_preparing(self, tg):
        """Batch ids with photos still being analysed/edited a moment ago.

        Full auto makes photos ready one by one. Routing too early would see
        only 1-2 of them and lock each in as a lone photo, so a batch (carousel
        / Reel candidate) waits until its last photo settles. The wait is capped:
        photos untouched for PREPARING_WINDOW no longer hold anything back.
        """
        cutoff = self.clock() - self.PREPARING_WINDOW
        held = set()
        for a in self.buffer.list_assets(tg):
            if a.get("status") not in ("uploaded", "analyzed", "selected"):
                continue
            if a.get("batch_id") is None:
                continue
            touched = a.get("updated_at") or a.get("created_at")
            if touched is not None and _aware(touched) > cutoff:
                held.add(a["batch_id"])
        return held

    def _story_companion(self, tg, asset_id, photo_slot, extra):
        """Also queue the same photo as a Story, later the same local day."""
        asset = self.buffer.assets.find_one({"_id": asset_id}) or {}
        if "story" not in ((asset.get("creative") or {}).get("variants") or {}):
            return
        when = self.calendar.companion_slot(tg, photo_slot)
        if when is None:
            return
        try:
            job = self.service.enqueue_for(tg, [asset_id], "story",
                                           scheduled_at=when, source="scheduled")
        except (PublishError, NotConnected):
            return
        if job.get("duplicate"):
            return
        fields = {k: v for k, v in extra.items() if k != "route"}
        fields["companion"] = True
        self.service.jobs.update_one({"_id": job["_id"]}, {"$set": fields})

    def _autofill(self):
        for tg in self._principals():
            settings = self.autopilot.get(tg)
            if settings["paused"] or not settings["formats"]:
                continue
            acct = self.accounts.get_for(tg)
            if not acct or acct.get("status") != "active":
                continue
            room = settings["daily_limit"] * 2 - len(
                [j for j in self.service.queue(tg)
                 if j["status"] in (svc.QUEUED, svc.RETRY) and j.get("source") == "scheduled"
                 and not j.get("companion")])      # a photo's Story rides along, no extra room
            if room <= 0:
                continue
            held = self._batches_still_preparing(tg)
            ready = [a for a in sorted(self.buffer.list_assets(tg, "ready"),
                                       key=lambda a: a["created_at"])
                     if not a.get("last_published_at")
                     and a.get("batch_id") not in held]
            if not ready:
                continue
            reels_on = "reel" in settings["formats"] and self._reels_ready(tg)
            routes = plan_routes(ready, self._detections(tg, [a for a in ready
                                                              if a.get("kind", "photo") == "photo"
                                                              and not a.get("route")]),
                                 settings["formats"], reels_enabled=reels_on)
            built = False
            for route in routes:
                if room <= 0:
                    break
                if route["format"] == "reel_build":
                    if built:          # one render per pass keeps the worker responsive
                        continue
                    built = True
                    route = self._build_reel(tg, route)
                    if route is None:
                        continue
                slot, meta = self._slot_for(tg, route)
                if slot is None:
                    if meta.get("event_status") in ("printed_date_passed", "date_full"):
                        logger.info("Skipping dated content %s: %s",
                                    route["asset_ids"][0], meta.get("event_status"))
                        continue
                    break
                self._store_route(route)   # before enqueue: format stays stable after a crash
                try:
                    job = self.service.enqueue_for(tg, route["asset_ids"], route["format"],
                                                   scheduled_at=slot, source="scheduled")
                except (PublishError, NotConnected):
                    self._clear_route(route)   # can be re-routed on the next pass
                    continue
                if job.get("duplicate"):
                    continue
                room -= 1
                extra = {"route": route.get("decision") or {}}
                if meta.get("event_status") in ("reserved", "date_pinned"):
                    extra.update({"planning": meta, "event_key": meta.get("event"),
                                  "event_date": meta.get("event_date")})
                self.service.jobs.update_one({"_id": job["_id"]}, {"$set": extra})
                if (route["format"] == "photo" and len(route["asset_ids"]) == 1
                        and "story" in settings["formats"]):
                    self._story_companion(tg, route["asset_ids"][0], slot, extra)

    # ---- tokens ----------------------------------------------------------
    def _check_tokens(self):
        web = self.handles_web_owners
        # 1) refresh anything close to expiry now, not only when a post happens to run
        for acct in self.accounts.expiring_soon(within_days=self.refresh_days, include_web=web):
            owner = document_principal(acct)
            if owner is None:
                continue
            try:
                self.accounts.get_token_for(owner)
            except (ReauthRequired, NotConnected):
                pass
            except Exception:
                logger.exception("Token check failed for %s", owner.owner_id)
        # 2) tell Telegram users (once per expiry date / per login). Web owners have
        #    no push channel; their dashboard shows the account status instead.
        for acct in self.accounts.needing_reauth():
            owner = document_principal(acct)
            if owner is None or owner.kind != "telegram":
                continue
            marker = _aware(acct["connected_at"]).isoformat()
            if acct.get("alert_reauth_for") != marker:
                self._tell(owner,
                           "🔑 Your Instagram login expired. Scheduled posts are paused "
                           "until you /connect again.")
                self.accounts.mark_alerted_for(owner, "reauth", marker)
        for acct in self.accounts.expiring_soon(within_days=ALERT_EXPIRY_DAYS):
            owner = document_principal(acct)
            if owner is None or owner.kind != "telegram":
                continue
            marker = _aware(acct["token_expires_at"]).isoformat()
            if acct.get("alert_expiry_for") != marker:
                days = max((_aware(acct["token_expires_at"]) - self.clock()).days, 0)
                self._tell(owner,
                           f"⏳ Your Instagram login expires in ~{days} day(s) and auto-refresh "
                           "isn't working. Use /connect to renew it.")
                self.accounts.mark_alerted_for(owner, "expiry", marker)
