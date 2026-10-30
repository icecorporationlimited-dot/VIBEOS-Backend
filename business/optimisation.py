"""business/optimisation.py - Phase 5 Optimisation agent (feedback loop).

Evaluates a campaign ONLY on real, settled Instagram metrics captured by the
Analytics agent, then writes concrete recommendations into the campaign. The
Planning agent reads them when the plan is next regenerated, closing the loop.

Honesty rules:
  * only posts published at least SETTLE_HOURS ago with real metrics count;
  * at least MIN_PUBLISHED such posts, otherwise the run fails honestly;
  * every number handed to the LLM is computed from business_metrics;
  * the numbers section saved on the campaign is always the Python-computed
    one - the LLM only writes advisory summary/recommendation text.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from uuid import uuid4

from .agents import AgentError, _extract_json, _llm
from .analytics import _aware, latest_metric
from .store import now

logger = logging.getLogger(__name__)

MIN_PUBLISHED = 3
SETTLE_HOURS = 24
STALE_HOURS = 24


class OptimisationAgent:
    SYSTEM = (
        "You are the Optimisation agent of a social-media growth engine for "
        "small businesses. You receive REAL Instagram metrics for published "
        "posts. Rules: base every statement strictly on the numbers given; "
        "never invent or extrapolate numbers; if the data is thin, say so "
        "plainly instead of guessing; recommendations must be concrete and "
        "short; do not suggest paid promotion. Return ONLY JSON of the shape "
        "{\"summary\": string, \"recommendations\": [{\"title\": string, "
        "\"detail\": string}]}"
    )

    def __init__(self, store):
        self.store = store

    # ---------------------------------------------------------- evaluation
    def settled_posts(self, wid, cid):
        """Published posts with real metrics, at least SETTLE_HOURS old."""
        posts = []
        for sch in self.store.schedules.find(
                {"workspace_id": wid, "campaign_id": cid, "status": "published"}):
            content = self.store.content.find_one(
                {"_id": sch.get("content_id"), "workspace_id": wid})
            if content is None or content.get("status") != "published":
                continue
            pub = _aware(sch.get("published_at"))
            if pub is None or now() - pub < timedelta(hours=SETTLE_HOURS):
                continue
            metric = latest_metric(self.store, content["_id"])
            if metric is None:
                continue
            posts.append({"content": content, "metric": metric})
        posts.sort(key=lambda p: p["content"].get("day") or 0)
        return posts

    def _numbers(self, posts):
        per_post = []
        for p in posts:
            m, c = p["metric"], p["content"]
            per_post.append({"day": c.get("day"), "format": c.get("format"),
                             "likes": m.get("likes"), "comments": m.get("comments"),
                             "impressions": m.get("impressions"),
                             "reach": m.get("reach")})

        def _eng(row):
            return (row.get("likes") or 0) + (row.get("comments") or 0)

        by_format = {}
        for row in per_post:
            by_format.setdefault(row["format"] or "feed", []).append(_eng(row))
        averages = {fmt: round(sum(v) / len(v), 1)
                    for fmt, v in by_format.items()}
        best = max(per_post, key=_eng) if per_post else None
        weakest = min(per_post, key=_eng) if per_post else None
        return {"posts": per_post,
                "average_engagement_by_format": averages,
                "best_post": best, "weakest_post": weakest}

    def evaluate(self, wid, cid):
        campaign = self.store.campaigns.find_one({"_id": cid, "workspace_id": wid})
        if campaign is None:
            return None
        posts = self.settled_posts(wid, cid)
        if len(posts) < MIN_PUBLISHED:
            raise AgentError(
                "Need at least %d published posts with real metrics (each at "
                "least %d h old) before optimising. Settled with metrics now: %d."
                % (MIN_PUBLISHED, SETTLE_HOURS, len(posts)))
        numbers = self._numbers(posts)
        task_id = self._start_task(wid, cid, {"posts": len(posts)})
        try:
            user = ("Real performance data from Instagram Graph (the ONLY "
                    "numbers you may use):\n"
                    + json.dumps(numbers, default=str)[:4000]
                    + "\n\nWrite the optimisation JSON now.")
            raw = _llm().chat(self.SYSTEM, user, temperature=0.4, max_tokens=600)
            data = _extract_json(raw)
            summary = str(data.get("summary") or "").strip()[:500]
            recs = []
            for r in (data.get("recommendations") or [])[:4]:
                if isinstance(r, dict) and str(r.get("title") or "").strip():
                    recs.append({"title": str(r["title"])[:120],
                                 "detail": str(r.get("detail") or "")[:300]})
                elif r:
                    recs.append({"title": str(r)[:120], "detail": ""})
            if not summary or not recs:
                raise AgentError("Optimisation output was incomplete; nothing saved.")
        except AgentError as exc:
            self._finish_task(task_id, error=str(exc)[:200])
            raise
        except Exception:
            self._finish_task(task_id, error="optimisation agent failed")
            raise AgentError("Optimisation agent failed; try again shortly.") from None
        doc = {"generated_at": now(),
               "based_on": {"posts": len(posts), "settle_hours": SETTLE_HOURS,
                            "source": "instagram_graph"},
               "summary": summary, "recommendations": recs, "numbers": numbers}
        self.store.campaigns.update_one(
            {"_id": cid, "workspace_id": wid},
            {"$set": {"optimisation": doc, "updated_at": now()}})
        self._finish_task(task_id, output={"recommendations": len(recs)})
        self.store.event(wid, "campaign_optimised",
                         "Optimisation run on %d settled posts with real metrics"
                         % len(posts))
        return self.store.campaigns.find_one({"_id": cid, "workspace_id": wid})

    # ------------------------------------------------------------ automatic
    def try_auto(self, wid, cid):
        """Called by the analytics tick: run when useful, stay quiet otherwise."""
        campaign = self.store.campaigns.find_one({"_id": cid, "workspace_id": wid})
        if campaign is None or campaign.get("status") != "active":
            return
        prev = _aware((campaign.get("optimisation") or {}).get("generated_at"))
        if prev is not None and now() - prev < timedelta(hours=STALE_HOURS):
            return
        try:
            self.evaluate(wid, cid)
        except AgentError as exc:
            logger.info("auto-optimisation skipped for %s: %s", cid, exc)
        except Exception:
            logger.exception("auto-optimisation failed for %s", cid)

    # ---------------------------------------------------------- agent tasks
    def _start_task(self, wid, cid, data):
        doc = {"_id": uuid4().hex, "workspace_id": wid, "campaign_id": cid,
               "agent": "optimisation", "status": "running", "input": data or {},
               "output": None, "error": None, "created_at": now(),
               "finished_at": None}
        self.store.agent_tasks.insert_one(doc)
        return doc["_id"]

    def _finish_task(self, task_id, output=None, error=None):
        self.store.agent_tasks.update_one(
            {"_id": task_id},
            {"$set": {"status": "failed" if error else "succeeded",
                      "output": output, "error": error, "finished_at": now()}})
