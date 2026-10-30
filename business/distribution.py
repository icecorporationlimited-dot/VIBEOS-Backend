"""business/distribution.py - Phase 4 Distribution agent (autonomous publishing).

The last step of the business pipeline: QC-passed content -> branded card
(worker render job) -> the EXISTING PublishService (the same verified path the
personal mode uses) -> Instagram -> verified published state.

Hard rules, same as the rest of Business Mode:
  * auto_publish must be switched ON and the campaign must be active;
    anything else holds the schedule instead of posting.
  * quiet hours and the daily_posts cap are respected.
  * Reel-format items are HELD (reel publishing is not implemented yet) -
    they are never faked or silently dropped.
  * A schedule only becomes "published" when the publish job is PUBLISHED,
    i.e. Instagram confirmed it. Nothing else ever counts as published.
  * Every transition is appended to the audit event log.
"""
from __future__ import annotations

import logging
import threading
from datetime import timedelta
from uuid import uuid4

from publishing.identity import principal_from_owner_id

from .store import now

logger = logging.getLogger(__name__)

PUBLISH_FMT = {"feed": "photo", "story": "story"}


def _in_quiet_hours(hour, quiet_start, quiet_end):
    start, end = int(quiet_start or 0), int(quiet_end or 0)
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end      # window wraps midnight


class BusinessDistributionAgent:
    """Background thread: schedule -> render -> publish -> verify."""

    def __init__(self, store, buffer, publish_service, accounts, db,
                 interval=60.0):
        self.store = store
        self.buffer = buffer
        self.publish = publish_service
        self.accounts = accounts
        self.db = db
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None

    # ------------------------------------------------------------ thread
    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="business-distribution", daemon=True)
        self._thread.start()
        logger.info("Business distribution agent started")

    def stop(self):
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)

    def _loop(self):
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception:
                logger.exception("distribution tick failed")

    # -------------------------------------------------------------- tick
    def tick(self):
        for settings in self.store.settings.find({"auto_publish": True}):
            try:
                self._workspace(settings)
            except Exception:
                logger.exception("distribution failed for workspace %s",
                                 settings.get("workspace_id"))
        self._hold_when_off()

    def _hold_when_off(self):
        """auto_publish switched OFF: hold every still-scheduled item."""
        for settings in self.store.settings.find({"auto_publish": False}):
            wid = settings.get("workspace_id")
            for sch in self.store.schedules.find(
                    {"workspace_id": wid, "status": "scheduled"}):
                self._hold(sch, "auto-publish switched off")

    # ------------------------------------------------------- one workspace
    def _workspace(self, settings):
        wid = settings["workspace_id"]
        ws = self.store.workspaces.find_one({"_id": wid})
        if ws is None or ws.get("status") != "active":
            return
        principal = principal_from_owner_id(ws.get("owner"))
        if principal is None:
            return
        self._resume_recoverable(wid, principal)
        self._track_results(wid)
        self._schedule_ready(wid, settings)
        self._process_due(wid, principal)

    def _resume_recoverable(self, wid, principal):
        """Reconnect/resume un-holds only what a fixed condition caused."""
        acct = self.accounts.get_for(principal)
        connected = bool(acct) and acct.get("status") != "disconnected"
        for sch in self.store.schedules.find({"workspace_id": wid, "status": "held"}):
            reason = str(sch.get("hold_reason") or "")
            if reason.startswith("auto-publish"):
                self.store.schedules.update_one(
                    {"_id": sch["_id"]},
                    {"$set": {"status": "scheduled", "updated_at": now()}})
            elif ("Instagram" in reason
                    and ("connected" in reason or "reconnect" in reason)
                    and connected):
                self.store.schedules.update_one(
                    {"_id": sch["_id"]},
                    {"$set": {"status": "scheduled", "updated_at": now()}})

    # results first: only Instagram's confirmed PUBLISHED counts as published
    def _track_results(self, wid):
        for sch in self.store.schedules.find({"workspace_id": wid, "status": "queued"}):
            jid = (sch.get("publish") or {}).get("job_id")
            if not jid:
                self._hold(sch, "queued without a publish job")
                continue
            job = self.db["publish_jobs"].find_one({"_id": jid})
            if job is None:
                self._hold(sch, "publish job disappeared")
                continue
            status = job.get("status")
            if status == "published":
                self._mark_published(sch, job)
            elif status == "failed":
                self._fail(sch, (job.get("last_error") or "publish failed")[:200])
            elif status == "cancelled":
                self._fail(sch, "publish job was cancelled")
            elif status == "needs_reauth":
                self._hold(sch, "Instagram login expired - reconnect Instagram")
            elif status == "held":
                self._hold(sch, "held by publishing policy: "
                            + str(job.get("hold_reason") or "policy")[:120])
            # queued / retry / running / needs_attention: still in flight, wait

    def _schedule_ready(self, wid, settings):
        for campaign in self.store.campaigns.find({"workspace_id": wid, "status": "active"}):
            for item in campaign.get("plan") or []:
                if item.get("status") != "ready" or not item.get("content_id"):
                    continue
                content = self.store.content.find_one(
                    {"_id": item["content_id"], "workspace_id": wid})
                if content is None or content.get("status") != "ready":
                    continue
                if self.store.schedules.find_one({"content_id": content["_id"]}):
                    continue
                slot = self._next_slot(wid, settings)
                if slot is None:
                    return          # daily cap reached; try again next tick
                self.store.schedules.insert_one({
                    "_id": uuid4().hex, "workspace_id": wid,
                    "campaign_id": campaign["_id"], "item_id": item.get("id"),
                    "content_id": content["_id"], "status": "scheduled",
                    "scheduled_at": slot, "created_at": now(), "updated_at": now()})
                self._mirror(content["_id"], {"status": "scheduled",
                                              "scheduled_at": slot})
                self.store.event(wid, "content_scheduled",
                                 "Scheduled a %s post for %s"
                                 % (content.get("format") or "feed", slot))

    def _next_slot(self, wid, settings):
        limit = int(settings.get("daily_posts") or 0)
        if limit <= 0:
            return None
        day = now().replace(minute=0, hour=0, second=0, microsecond=0)
        used = self.store.schedules.count_documents({
            "workspace_id": wid,
            "status": {"$in": ["scheduled", "queued", "published"]},
            "scheduled_at": {"$gte": day, "$lt": day + timedelta(days=1)}})
        if used >= limit:
            return None
        slot = now() + timedelta(minutes=2)
        end = int(settings.get("quiet_end") or 0)
        while _in_quiet_hours(slot.hour, settings.get("quiet_start"),
                              settings.get("quiet_end")):
            slot = (slot.replace(minute=0, second=0, microsecond=0)
                    + timedelta(hours=((end - slot.hour) % 24) or 24))
        return slot

    def _process_due(self, wid, principal):
        due = self.store.schedules.find(
            {"workspace_id": wid, "status": "scheduled",
             "scheduled_at": {"$lte": now()}})
        for sch in list(due):
            campaign = self.store.campaigns.find_one(
                {"_id": sch.get("campaign_id"), "workspace_id": wid})
            if campaign is None or campaign.get("status") != "active":
                self._hold(sch, "campaign is not active")
                continue
            content = self.store.content.find_one(
                {"_id": sch.get("content_id"), "workspace_id": wid})
            if content is None:
                self._hold(sch, "content document missing")
                continue
            status = content.get("status")
            if status == "held":
                self._hold(sch, ((content.get("render") or {}).get("reason")
                                 or "content held"))
            elif status == "ready":
                self._enqueue_render(sch, content)
            elif status == "rendered":
                self._enqueue_publish(sch, content, principal)
            else:
                self._hold(sch, "content is " + str(status))

    def _enqueue_render(self, sch, content):
        import jobs
        if jobs.find_open(self.db, "business_render", content["_id"]):
            return
        jobs.enqueue(self.db, "business_render",
                     {"workspace_id": sch["workspace_id"],
                      "campaign_id": sch.get("campaign_id"),
                      "content_id": content["_id"]},
                     asset_id=content["_id"])
        self._mirror(content["_id"], {"status": "rendering"})

    def _enqueue_publish(self, sch, content, principal):
        import jobs
        from publishing.service import PublishError
        from publishing.accounts import NotConnected
        fmt = PUBLISH_FMT.get(content.get("format") or "feed")
        render = content.get("render") or {}
        if fmt is None or not render.get("blob_id"):
            self._hold(sch, "no rendered media for this item")
            return
        acct = self.accounts.get_for(principal)
        if not acct or acct.get("status") == "disconnected":
            self._hold(sch, "Instagram is not connected")
            return
        try:
            data = jobs.get_blob(self.db, render["blob_id"])
            res = self.buffer.add_business_card(
                principal, data, caption=content.get("caption") or "",
                hashtags=content.get("hashtags") or [], fmt=fmt)
            job = self.publish.enqueue_for(principal, [res["asset"]["_id"]], fmt,
                                           scheduled_at=None, source="scheduled")
        except (PublishError, NotConnected) as exc:
            self._hold(sch, str(exc)[:200])
            return
        except Exception:
            logger.exception("business publish enqueue failed for %s",
                             str(content.get("_id"))[:8])
            self._hold(sch, "publishing failed before it started")
            return
        self.store.schedules.update_one(
            {"_id": sch["_id"]},
            {"$set": {"status": "queued", "updated_at": now(),
                      "publish": {"job_id": job["_id"], "format": fmt,
                                  "asset_id": res["asset"]["_id"],
                                  "duplicate": bool(job.get("duplicate"))}}})
        self._mirror(content["_id"], {"status": "queued",
                                      "asset_id": res["asset"]["_id"],
                                      "job_id": job["_id"]})
        self.store.event(sch["workspace_id"], "content_queued",
                         "Sent a %s post to the publishing queue" % fmt)

    # ----------------------------------------------------------- transitions
    def _hold(self, sch, reason):
        self.store.schedules.update_one(
            {"_id": sch["_id"], "status": {"$in": ["scheduled", "queued"]}},
            {"$set": {"status": "held", "hold_reason": str(reason)[:200],
                      "updated_at": now()}})
        if sch.get("content_id"):
            self._mirror(sch["content_id"], {"status": "held",
                                             "reason": str(reason)[:200]})
        self.store.event(sch.get("workspace_id"), "distribution_held",
                         "Publishing held: " + str(reason)[:160])

    def _fail(self, sch, reason):
        self.store.schedules.update_one(
            {"_id": sch["_id"]},
            {"$set": {"status": "failed", "hold_reason": str(reason)[:200],
                      "updated_at": now()}})
        if sch.get("content_id"):
            self._mirror(sch["content_id"], {"status": "failed",
                                             "reason": str(reason)[:200]})
        self.store.event(sch.get("workspace_id"), "distribution_failed",
                         "Publishing failed: " + str(reason)[:160])

    def _mark_published(self, sch, job):
        wid = sch.get("workspace_id")
        permalink = job.get("permalink")
        self.store.schedules.update_one(
            {"_id": sch["_id"]},
            {"$set": {"status": "published", "permalink": permalink,
                      "published_at": now(), "updated_at": now()}})
        if sch.get("content_id"):
            self.store.content.update_one(
                {"_id": sch["content_id"]},
                {"$set": {"status": "published", "updated_at": now(),
                          "schedule": {"status": "published",
                                        "permalink": permalink,
                                        "updated_at": now()}}})
        if wid and sch.get("campaign_id") and sch.get("item_id"):
            self.store.campaigns.update_one(
                {"_id": sch["campaign_id"], "workspace_id": wid,
                 "plan.id": sch["item_id"]},
                {"$set": {"plan.$.status": "published"}})
        self.store.event(wid, "content_published",
                         "Instagram confirmed the post is published"
                         + (" - " + str(permalink) if permalink else ""))

    def _mirror(self, content_id, fields):
        """Keep the display copy on the content doc in sync with the schedule."""
        doc = {"status": fields.get("status"), "updated_at": now()}
        for key in ("reason", "scheduled_at", "asset_id", "job_id", "permalink"):
            if fields.get(key) is not None:
                doc[key] = fields[key]
        self.store.content.update_one({"_id": content_id},
                                      {"$set": {"schedule": doc}})
