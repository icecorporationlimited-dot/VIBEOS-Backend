"""business/store.py - Business Mode persistence (Phase 1 foundation).

Independent collections, ownership enforced on EVERY query, UTC timestamps.
Nothing here touches the personal-mode collections, so a business workspace
can never read or write personal data (and vice versa).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from uuid import uuid4

HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

CATEGORIES = (
    "restaurant_cafe", "clothing_fashion", "ecommerce", "retail",
    "beauty_skincare", "fitness_gym", "photography", "freelancing",
    "personal_brand", "digital_products", "education", "travel",
    "real_estate", "technology", "other",
)
GOALS = ("awareness", "audience_growth", "engagement", "website_traffic",
         "enquiries", "lead_generation", "product_sales", "service_promotion",
         "local_discovery")


def now():
    return datetime.now(timezone.utc)


def owner_key(principal):
    """Stable owner string for a principal (web or telegram)."""
    kind = getattr(principal, "kind", "web")
    subject = getattr(principal, "subject", "")
    return str(kind) + ":" + str(subject)


def _str(value, limit=300):
    return str(value or "").strip()[:limit]


def _hex_list(values, cap=6):
    out = []
    for v in (values or [])[:cap]:
        v = str(v).strip()
        if HEX_RE.match(v):
            out.append(v.lower())
    return out


class BusinessStore:
    """All Business Mode collections. Owns nothing personal."""

    def __init__(self, db):
        self.db = db
        self.workspaces = db["business_workspaces"]
        self.campaigns = db["business_campaigns"]
        self.products = db["business_products"]
        self.content = db["business_content_assets"]
        self.agent_tasks = db["business_agent_tasks"]
        self.jobs = db["business_jobs"]
        self.schedules = db["business_schedules"]
        self.integrations = db["business_integrations"]
        self.metrics = db["business_metrics"]
        self.leads = db["business_leads"]
        self.events = db["business_events"]
        self.settings = db["business_automation_settings"]

    def ensure_indexes(self):
        self.workspaces.create_index([("owner", 1), ("created_at", -1)])
        self.campaigns.create_index([("workspace_id", 1), ("status", 1), ("created_at", -1)])
        self.products.create_index([("workspace_id", 1), ("created_at", -1)])
        self.content.create_index([("workspace_id", 1), ("campaign_id", 1), ("status", 1)])
        self.agent_tasks.create_index([("workspace_id", 1), ("campaign_id", 1), ("status", 1)])
        self.jobs.create_index([("workspace_id", 1), ("status", 1), ("created_at", -1)])
        self.schedules.create_index([("workspace_id", 1), ("scheduled_at", 1)])
        self.metrics.create_index([("workspace_id", 1), ("created_at", -1)])
        self.leads.create_index([("workspace_id", 1), ("status", 1), ("created_at", -1)])
        self.events.create_index([("workspace_id", 1), ("created_at", -1)])
        self.settings.create_index("workspace_id", unique=True)

    # ------------------------------------------------------------ workspaces
    def list_for(self, owner):
        return list(self.workspaces.find({"owner": owner}).sort("created_at", 1))

    def get(self, owner, workspace_id):
        """Ownership is part of the query itself, not a filter applied later."""
        return self.workspaces.find_one({"_id": workspace_id, "owner": owner})

    def create(self, owner, data):
        data = data or {}
        name = _str(data.get("name"), 120)
        if not name:
            raise ValueError("Business name is required.")
        category = _str(data.get("category"), 40)
        if category and category not in CATEGORIES:
            raise ValueError("Unknown business category.")
        brand = data.get("brand") or {}
        audience = data.get("audience") or {}
        doc = {
            "_id": uuid4().hex,
            "owner": owner,
            "name": name,
            "category": category or "other",
            "description": _str(data.get("description"), 2000),
            "location": _str(data.get("location"), 200),
            "website": _str(data.get("website"), 300),
            "contact": _str(data.get("contact"), 200),
            "logo_blob": None,                     # blob id (jobs.put_blob) once uploads are wired
            "brand": {
                "colors": _hex_list(brand.get("colors")),
                "personality": _str(brand.get("personality"), 120),
                "voice": _str(brand.get("voice"), 120),
                "language": _str(brand.get("language"), 40) or "english",
                "typography": _str(brand.get("typography"), 120),
                "guidelines": _str(brand.get("guidelines"), 2000),
            },
            "goals": [g for g in (data.get("goals") or []) if g in GOALS][:6],
            "audience": {
                "description": _str(audience.get("description"), 1000),
                "age_ranges": [_str(a, 40) for a in (audience.get("age_ranges") or [])[:5]],
                "locations": [_str(l, 80) for l in (audience.get("locations") or [])[:6]],
                "interests": [_str(i, 60) for i in (audience.get("interests") or [])[:10]],
                "language": _str(audience.get("language"), 40),
                "needs": _str(audience.get("needs"), 1000),
            },
            "onboarding": {"step": "basics", "completed": False, "updated_at": now()},
            "status": "onboarding",                # onboarding -> active
            "created_at": now(),
            "updated_at": now(),
        }
        self.workspaces.insert_one(doc)
        self.event(doc["_id"], "workspace_created", "Business workspace created")
        return doc

    def update(self, owner, workspace_id, patch_data):
        """Partial update used by the onboarding wizard's autosave."""
        ws = self.get(owner, workspace_id)
        if ws is None:
            return None
        patch_data = patch_data or {}
        fields = {}
        for key, cap in (("name", 120), ("description", 2000), ("location", 200),
                         ("website", 300), ("contact", 200), ("category", 40)):
            if key in patch_data:
                value = _str(patch_data.get(key), cap)
                if key == "name" and not value:
                    raise ValueError("Business name is required.")
                if key == "category" and value and value not in CATEGORIES:
                    raise ValueError("Unknown business category.")
                fields[key] = value
        if "brand" in patch_data:
            brand = dict(ws.get("brand") or {})
            incoming = patch_data.get("brand") or {}
            if "colors" in incoming:
                brand["colors"] = _hex_list(incoming.get("colors"))
            for key, cap in (("personality", 120), ("voice", 120), ("language", 40),
                             ("typography", 120), ("guidelines", 2000)):
                if key in incoming:
                    brand[key] = _str(incoming.get(key), cap) or brand.get(key)
            fields["brand"] = brand
        if "goals" in patch_data:
            fields["goals"] = [g for g in (patch_data.get("goals") or []) if g in GOALS][:6]
        if "audience" in patch_data:
            aud = dict(ws.get("audience") or {})
            incoming = patch_data.get("audience") or {}
            for key, cap in (("description", 1000), ("language", 40), ("needs", 1000)):
                if key in incoming:
                    aud[key] = _str(incoming.get(key), cap)
            for key, cap, n in (("age_ranges", 40, 5), ("locations", 80, 6),
                                ("interests", 60, 10)):
                if key in incoming:
                    aud[key] = [_str(v, cap) for v in (incoming.get(key) or [])[:n]]
            fields["audience"] = aud
        if "onboarding" in patch_data:
            ob = dict(ws.get("onboarding") or {})
            incoming = patch_data.get("onboarding") or {}
            for key in ("step", "completed"):
                if key in incoming:
                    ob[key] = incoming[key]
            ob["updated_at"] = now()
            fields["onboarding"] = ob
        if not fields:
            return ws
        fields["updated_at"] = now()
        self.workspaces.update_one({"_id": workspace_id, "owner": owner}, {"$set": fields})
        return self.get(owner, workspace_id)

    def activate(self, owner, workspace_id):
        ws = self.get(owner, workspace_id)
        if ws is None:
            return None
        if not ws.get("name") or not ws.get("category"):
            raise ValueError("Add at least the business name and category first.")
        self.workspaces.update_one({"_id": workspace_id, "owner": owner},
                                   {"$set": {"status": "active", "updated_at": now()}})
        self.event(workspace_id, "workspace_activated", "Business Mode activated")
        return self.get(owner, workspace_id)

    def delete(self, owner, workspace_id):
        if self.get(owner, workspace_id) is None:
            return False
        query = {"workspace_id": workspace_id}
        self.products.delete_many(query)
        self.leads.delete_many(query)
        self.settings.delete_many(query)
        self.events.delete_many(query)
        self.workspaces.delete_one({"_id": workspace_id, "owner": owner})
        return True

    # ------------------------------------------------------- products/services
    def list_products(self, owner, workspace_id):
        if self.get(owner, workspace_id) is None:
            return None
        return list(self.products.find({"workspace_id": workspace_id}).sort("created_at", 1))

    def _product_fields(self, data):
        data = data or {}
        return {
            "name": _str(data.get("name"), 160),
            "description": _str(data.get("description"), 2000),
            "price": _str(data.get("price"), 60),
            "currency": _str(data.get("currency"), 8),
            "category": _str(data.get("category"), 80),
            "availability": "unavailable" if data.get("availability") == "unavailable" else "available",
            "url": _str(data.get("url"), 500),
            "promotional": bool(data.get("promotional")),
        }

    def add_product(self, owner, workspace_id, data):
        if self.get(owner, workspace_id) is None:
            return None
        fields = self._product_fields(data)
        if not fields["name"]:
            raise ValueError("Product name is required.")
        doc = {"_id": uuid4().hex, "workspace_id": workspace_id,
               "image_blob": None, "created_at": now(), "updated_at": now()}
        doc.update(fields)
        self.products.insert_one(doc)
        self.event(workspace_id, "product_added", "Product added: " + fields["name"])
        return doc

    def update_product(self, owner, workspace_id, product_id, data):
        if self.get(owner, workspace_id) is None:
            return None
        data = data or {}
        fields = self._product_fields(data)
        if "name" in data and not fields["name"]:
            raise ValueError("Product name is required.")
        for key in list(fields):
            if key not in data:
                fields.pop(key)
        if not fields:
            return self.products.find_one({"_id": product_id, "workspace_id": workspace_id})
        fields["updated_at"] = now()
        self.products.update_one({"_id": product_id, "workspace_id": workspace_id},
                                 {"$set": fields})
        return self.products.find_one({"_id": product_id, "workspace_id": workspace_id})

    def delete_product(self, owner, workspace_id, product_id):
        if self.get(owner, workspace_id) is None:
            return False
        return self.products.delete_one({"_id": product_id, "workspace_id": workspace_id}).deleted_count > 0

    # -------------------------------------------------- automation settings
    def get_settings(self, owner, workspace_id):
        if self.get(owner, workspace_id) is None:
            return None
        doc = self.settings.find_one({"workspace_id": workspace_id})
        if doc is None:
            doc = {"_id": uuid4().hex, "workspace_id": workspace_id,
                   "auto_publish": False, "auto_comments": False, "auto_replies": False,
                   "daily_posts": 2, "quiet_start": 22, "quiet_end": 8,
                   "created_at": now(), "updated_at": now()}
            self.settings.insert_one(doc)
        return doc

    def update_settings(self, owner, workspace_id, patch_data):
        current = self.get_settings(owner, workspace_id)
        if current is None:
            return None
        patch_data = patch_data or {}
        fields = {}
        for key in ("auto_publish", "auto_comments", "auto_replies"):
            if key in patch_data:
                fields[key] = bool(patch_data.get(key))
        for key in ("daily_posts", "quiet_start", "quiet_end"):
            if key in patch_data:
                try:
                    limit = 12 if key == "daily_posts" else 23
                    fields[key] = max(0, min(limit, int(patch_data.get(key))))
                except (TypeError, ValueError):
                    pass
        if not fields:
            return current
        fields["updated_at"] = now()
        self.settings.update_one({"workspace_id": workspace_id}, {"$set": fields})
        return self.get_settings(owner, workspace_id)

    # ------------------------------------------------------ audit event log
    def event(self, workspace_id, kind, message, data=None):
        """Append-only audit entry. Business actions must never be edited."""
        self.events.insert_one({
            "_id": uuid4().hex, "workspace_id": workspace_id,
            "kind": _str(kind, 60), "message": _str(message, 300),
            "data": data or {}, "created_at": now()})

    def recent_events(self, owner, workspace_id, limit=30):
        if self.get(owner, workspace_id) is None:
            return None
        return list(self.events.find({"workspace_id": workspace_id})
                    .sort("created_at", -1).limit(limit))

    # ------------------------------------------------------------ dashboard
    def dashboard(self, owner, workspace_id):
        ws = self.get(owner, workspace_id)
        if ws is None:
            return None
        return {
            "workspace": ws,
            "counts": {
                "products": self.products.count_documents({"workspace_id": workspace_id}),
                "leads": self.leads.count_documents({"workspace_id": workspace_id}),
                "campaigns": self.campaigns.count_documents({"workspace_id": workspace_id}),
            },
            "settings": self.get_settings(owner, workspace_id),
            "events": self.recent_events(owner, workspace_id, 10),
        }
