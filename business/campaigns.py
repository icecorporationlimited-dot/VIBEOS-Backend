"""business/campaigns.py - campaign lifecycle for Business Mode (Phase 2).

A workspace defines the brand; a campaign defines what to achieve over a
period. The agents (strategy + planning here; copywriting/creative/QC/
distribution/analytics in later phases) work on the campaign. Nothing in this
module publishes anything or spends anything - publishing is a later phase and
only ever goes through the automation settings the owner switches on.
"""
from __future__ import annotations

from uuid import uuid4

from .store import GOALS, _str, now

FORMATS = ("feed", "reel", "story")
STATUSES = ("draft", "active", "paused", "stopped")
MAX_PLAN_ITEMS = 24
MAX_DURATION_DAYS = 90


class CampaignStore:
    """Campaign CRUD + lifecycle on business_campaigns."""

    TRANSITIONS = {
        "start": (("draft", "paused"), "active"),
        "pause": (("active",), "paused"),
        "resume": (("paused",), "active"),
        "stop": (("draft", "active", "paused"), "stopped"),
    }

    def __init__(self, store):
        self.store = store
        self.col = store.campaigns

    def _ws(self, owner, workspace_id):
        return self.store.get(owner, workspace_id)

    def list(self, owner, workspace_id):
        if self._ws(owner, workspace_id) is None:
            return None
        out = list(self.col.find({"workspace_id": workspace_id}).sort("created_at", 1))
        for c in out:
            c["content_ready"] = self.store.content.count_documents(
                {"campaign_id": c["_id"], "status": "ready"})
            c["content_failed"] = self.store.content.count_documents(
                {"campaign_id": c["_id"], "status": "failed"})
        return out

    def get(self, owner, workspace_id, campaign_id):
        if self._ws(owner, workspace_id) is None:
            return None
        return self.col.find_one({"_id": campaign_id, "workspace_id": workspace_id})

    def create(self, owner, workspace_id, data):
        ws = self._ws(owner, workspace_id)
        if ws is None:
            return None
        data = data or {}
        name = _str(data.get("name"), 120)
        if not name:
            raise ValueError("Campaign name is required.")
        objective = _str(data.get("objective"), 40)
        if objective and objective not in GOALS:
            raise ValueError("Unknown campaign objective.")
        try:
            duration = int(data.get("duration_days") or 14)
        except (TypeError, ValueError):
            duration = 14
        duration = max(3, min(MAX_DURATION_DAYS, duration))
        doc = {
            "_id": uuid4().hex,
            "workspace_id": workspace_id,
            "name": name,
            "objective": objective or (ws.get("goals") or ["awareness"])[0],
            "duration_days": duration,
            "brief": None,          # written by the Strategy agent
            "plan": [],             # written by the Planning agent
            "status": "draft",
            "status_history": [{"status": "draft", "at": now()}],
            "created_at": now(),
            "updated_at": now(),
        }
        self.col.insert_one(doc)
        self.store.event(workspace_id, "campaign_created", "Campaign created: " + name)
        return doc

    def update(self, owner, workspace_id, campaign_id, data):
        doc = self.get(owner, workspace_id, campaign_id)
        if doc is None:
            return None
        if doc.get("status") == "stopped":
            raise ValueError("A stopped campaign cannot be edited.")
        data = data or {}
        fields = {}
        if "name" in data:
            name = _str(data.get("name"), 120)
            if not name:
                raise ValueError("Campaign name is required.")
            fields["name"] = name
        if "objective" in data:
            objective = _str(data.get("objective"), 40)
            if objective and objective not in GOALS:
                raise ValueError("Unknown campaign objective.")
            if objective:
                fields["objective"] = objective
        if "duration_days" in data:
            try:
                fields["duration_days"] = max(3, min(MAX_DURATION_DAYS, int(data.get("duration_days"))))
            except (TypeError, ValueError):
                pass
        if not fields:
            return doc
        fields["updated_at"] = now()
        self.col.update_one({"_id": campaign_id, "workspace_id": workspace_id}, {"$set": fields})
        return self.get(owner, workspace_id, campaign_id)

    def delete(self, owner, workspace_id, campaign_id):
        doc = self.get(owner, workspace_id, campaign_id)
        if doc is None:
            return False
        if doc.get("status") in ("active", "paused"):
            raise ValueError("Stop the campaign before deleting it.")
        self.col.delete_one({"_id": campaign_id, "workspace_id": workspace_id})
        self.store.event(workspace_id, "campaign_deleted", "Campaign deleted: " + doc.get("name", ""))
        return True

    # ------------------------------------------------------------- lifecycle
    def transition(self, owner, workspace_id, campaign_id, action):
        doc = self.get(owner, workspace_id, campaign_id)
        if doc is None:
            return None
        rule = self.TRANSITIONS.get(action)
        if not rule:
            raise ValueError("Unknown campaign action.")
        allowed, target = rule
        if doc.get("status") not in allowed:
            raise ValueError("Campaign is '%s'; cannot %s it." % (doc.get("status"), action))
        if action == "start":
            ws = self._ws(owner, workspace_id)
            if not ws or ws.get("status") != "active":
                raise ValueError("Activate the business workspace first.")
            if not doc.get("brief"):
                raise ValueError("Generate the strategy brief before starting.")
            if not doc.get("plan"):
                raise ValueError("Generate the content plan before starting.")
        self.col.update_one(
            {"_id": campaign_id, "workspace_id": workspace_id},
            {"$set": {"status": target, "updated_at": now()},
             "$push": {"status_history": {"status": target, "at": now()}}})
        self.store.event(workspace_id, "campaign_" + action,
                         "Campaign '%s' is now %s" % (doc.get("name", ""), target))
        return self.get(owner, workspace_id, campaign_id)

    # -------------------------------------------------- brief / plan (agents)
    def set_brief(self, workspace_id, campaign_id, brief):
        self.col.update_one({"_id": campaign_id, "workspace_id": workspace_id},
                            {"$set": {"brief": brief, "updated_at": now()}})

    def set_plan(self, workspace_id, campaign_id, items):
        self.col.update_one({"_id": campaign_id, "workspace_id": workspace_id},
                            {"$set": {"plan": items, "updated_at": now()}})

    def tasks(self, owner, workspace_id, campaign_id, limit=30):
        if self.get(owner, workspace_id, campaign_id) is None:
            return None
        return list(self.store.agent_tasks.find(
            {"workspace_id": workspace_id, "campaign_id": campaign_id}
        ).sort("created_at", -1).limit(limit))
