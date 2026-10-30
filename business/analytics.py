"""business/analytics.py - Phase 5 Analytics agent (REAL metrics only).

A background thread that, for every published business post:
  * fetches the like/comment counts and (when the token has the
    manage-insights scope) impressions/reach from the Instagram Graph API,
  * stores each capture as an append-only document in business_metrics,
  * mirrors the latest numbers onto the content document for the UI.

Nothing is ever estimated, invented or backfilled: if Instagram does not
answer, that capture is skipped and retried on a later tick. Optimisation
runs automatically (auto_publish workspaces only) once enough settled posts
have real metrics.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from publishing.graph import GraphError
from publishing.identity import principal_from_owner_id

from .store import now

logger = logging.getLogger(__name__)


def _aware(dt):
    if dt is None or getattr(dt, "tzinfo", None):
        return dt
    return dt.replace(tzinfo=timezone.utc)


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def latest_metric(store, content_id):
    return store.metrics.find_one({"content_id": content_id},
                                  sort=[("captured_at", -1)])


def campaign_metrics(store, wid, cid):
    """Latest REAL metrics per published post + honest campaign totals."""
    rows = {}
    for m in store.metrics.find({"workspace_id": wid, "campaign_id": cid}
                                ).sort("captured_at", 1):
        rows[m["content_id"]] = m
    items = [{"content_id": cid_, "likes": m.get("likes"),
              "comments": m.get("comments"), "impressions": m.get("impressions"),
              "reach": m.get("reach"), "captured_at": m.get("captured_at")}
             for cid_, m in rows.items()]
    totals = {
        "likes": sum(m.get("likes") or 0 for m in rows.values()),
        "comments": sum(m.get("comments") or 0 for m in rows.values()),
        "impressions": sum(m.get("impressions") or 0 for m in rows.values()),
        "reach": sum(m.get("reach") or 0 for m in rows.values()),
        "posts_with_metrics": len(rows),
    }
    return {"items": items, "totals": totals, "source": "instagram_graph"}


class BusinessAnalyticsAgent:
    """Background thread: capture real metrics, then trigger optimisation."""

    CAPTURE_GAP = timedelta(hours=6)      # re-poll a post at most this often
    FINAL_AFTER = timedelta(hours=72)     # stop polling this long after publish

    def __init__(self, store, accounts, graph, db, interval=900.0):
        self.store = store
        self.accounts = accounts
        self.graph = graph
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
            target=self._loop, name="business-analytics", daemon=True)
        self._thread.start()
        logger.info("Business analytics agent started")

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
                logger.exception("analytics tick failed")

    # -------------------------------------------------------------- tick
    def tick(self):
        for ws in self.store.workspaces.find({"status": "active"}):
            try:
                self._workspace(ws)
            except Exception:
                logger.exception("analytics failed for workspace %s", ws.get("_id"))

    def _workspace(self, ws):
        wid = ws["_id"]
        principal = principal_from_owner_id(ws.get("owner"))
        if principal is None:
            return
        try:
            _ig_id, token = self.accounts.get_token_for(principal)
        except Exception as exc:
            logger.info("analytics skipped for %s: %s", wid, exc)
            return
        for sch in self.store.schedules.find(
                {"workspace_id": wid, "status": "published"}):
            try:
                self._capture(sch, token)
            except Exception:
                logger.exception("metric capture failed for %s",
                                 str(sch.get("content_id"))[:8])
        # Optimisation feedback loop (autonomous workspaces only)
        settings = self.store.settings.find_one({"workspace_id": wid}) or {}
        if settings.get("auto_publish"):
            from .optimisation import OptimisationAgent
            for campaign in self.store.campaigns.find(
                    {"workspace_id": wid, "status": "active"}, {"_id": 1}):
                try:
                    OptimisationAgent(self.store).try_auto(wid, campaign["_id"])
                except Exception:
                    logger.exception("auto-optimisation failed for %s",
                                     campaign["_id"])

    def _capture(self, sch, token):
        wid = sch.get("workspace_id")
        content_id = sch.get("content_id")
        pub = _aware(sch.get("published_at"))
        if not content_id or pub is None:
            return
        latest = latest_metric(self.store, content_id)
        if latest is not None:
            if now() - pub > self.FINAL_AFTER:
                return                      # settled; stop polling
            if now() - _aware(latest.get("captured_at")) < self.CAPTURE_GAP:
                return                      # captured recently
        job = self.db["publish_jobs"].find_one(
            {"_id": (sch.get("publish") or {}).get("job_id")})
        if job is None or not job.get("media_id"):
            return                          # nothing confirmed to measure
        try:
            stats = self.graph.media_stats(job["media_id"], token)
        except GraphError as exc:
            logger.info("metrics fetch failed for %s: %s",
                        str(content_id)[:8], exc.summary())
            return
        insights = {}
        try:
            insights = self.graph.media_insights(job["media_id"], token)
        except GraphError:
            pass            # manage-insights scope may be absent; counts still real
        doc = {"_id": uuid4().hex, "workspace_id": wid,
               "campaign_id": sch.get("campaign_id"), "content_id": content_id,
               "media_id": job["media_id"],
               "likes": _int(stats.get("like_count")),
               "comments": _int(stats.get("comments_count")),
               "impressions": _int(insights.get("impressions")),
               "reach": _int(insights.get("reach")),
               "captured_at": now(), "source": "instagram_graph"}
        self.store.metrics.insert_one(doc)
        self.store.content.update_one(
            {"_id": content_id},
            {"$set": {"metrics": {"likes": doc["likes"], "comments": doc["comments"],
                                  "impressions": doc["impressions"],
                                  "reach": doc["reach"],
                                  "captured_at": doc["captured_at"]}}})
        self.store.event(wid, "metrics_captured",
                         "Real Instagram metrics captured for a published post")
