"""business/content.py - Phase 3 content factory.

Three agents work on ONE campaign plan item, queued through the existing
persistent job queue (jobs.py type "business_content", run by worker.py - so
generation never happens inside an HTTP cycle):

  Copywriting agent -> caption + hashtags (shared copywriter.py rules)
  Creative agent    -> visual direction (layout, overlay text, brand colors)
  QC agent          -> verifies the copy: no forbidden phrases, no unverified
                      numbers/prices, hashtag limit, availability respected

Output: one document in business_content_assets with status ready|failed and
the plan item updated to generating -> ready|failed. Nothing is published
here; publishing is a later phase and only via the automation settings.
"""
from __future__ import annotations

import re
from uuid import uuid4

from .agents import AgentError, _extract_json, _llm
from .store import now

LAYOUTS = ("brand_card", "quote_card", "product_promo", "simple_overlay")
RISKY_CLAIMS = ("guaranteed results", "best in the world", "no.1 in", "#1 in",
                "miracle", "risk-free investment")
HASHTAG_LIMIT = 5


class ContentFactory:
    COPY_SYSTEM = (
        "You are the Copywriting agent of a business social-media engine. Write "
        "one Instagram caption for a planned post. Rules: use ONLY the facts "
        "given (business, brief, products); never invent prices, discounts, "
        "events, awards, reviews or statistics; if a fact is missing write "
        "generic safe copy without the claim; respect the do_not_say list; "
        "match the brand voice; no hashtags inside the caption. Return ONLY "
        'JSON: {"caption": "...", "hashtags": ["...", "..."]}'
    )
    CREATIVE_SYSTEM = (
        "You are the Creative agent of a business social-media engine. Choose "
        "the visual direction for a text-on-brand graphic (colors come from "
        "the brand palette, overlay text is short). Rules: overlay text must "
        "use only facts already given; no invented claims or prices. Return "
        'ONLY JSON: {"layout": "brand_card"|"quote_card"|"product_promo"|'
        '"simple_overlay", "overlay_text": "...", "note": "..."}'
    )

    def __init__(self, store):
        self.store = store

    # ---------------------------------------------------------------- helpers
    def _ws(self, wid):
        return self.store.workspaces.find_one({"_id": wid})

    def _campaign(self, wid, cid):
        return self.store.campaigns.find_one({"_id": cid, "workspace_id": wid})

    def _set_item(self, wid, cid, item_id, fields):
        self.store.campaigns.update_one(
            {"_id": cid, "workspace_id": wid, "plan.id": item_id},
            {"$set": {("plan.$." + k): v for k, v in fields.items()}})

    def _task(self, wid, cid, agent, status, output=None, error=None):
        self.store.agent_tasks.insert_one({
            "_id": uuid4().hex, "workspace_id": wid, "campaign_id": cid,
            "agent": agent, "status": status, "input": {},
            "output": output, "error": error,
            "created_at": now(), "finished_at": now()})

    def _digits(self, text):
        return set(re.findall(r"\d+(?:[.,]\d+)?", str(text or "")))

    def _fail_item(self, wid, cid, item_id, error):
        self._set_item(wid, cid, item_id, {"status": "failed", "error": str(error)[:200]})

    # ------------------------------------------------------------------ job
    def run_job(self, wid, cid, item_id, job_id):
        ws = self._ws(wid)
        campaign = self._campaign(wid, cid)
        if ws is None or campaign is None:
            raise KeyError("unknown campaign " + cid)
        item = next((i for i in (campaign.get("plan") or [])
                     if i.get("id") == item_id), None)
        if item is None:
            raise KeyError("unknown plan item " + item_id)
        if item.get("status") == "ready":
            return {"skipped": "already ready"}

        self._set_item(wid, cid, item_id, {"status": "generating", "error": None})
        try:
            llm = _llm()
        except AgentError as exc:
            self._task(wid, cid, "copywriting", "failed", error=str(exc)[:200])
            self._fail_item(wid, cid, item_id, exc)
            return {"status": "failed", "error": str(exc)[:200]}

        brief = campaign.get("brief") or {}
        brand = ws.get("brand") or {}
        products = [p for p in self.store.products.find(
            {"workspace_id": wid, "availability": "available"})][:20]
        product_facts = [{"name": p.get("name"), "description": p.get("description"),
                          "price": p.get("price"), "currency": p.get("currency"),
                          "promotional": p.get("promotional")} for p in products]
        context = {
            "business": ws.get("name"), "brand": brand,
            "post": {"day": item.get("day"), "format": item.get("format"),
                     "angle": item.get("angle"), "goal": item.get("goal"),
                     "note": item.get("note")},
            "brief": {"key_messages": brief.get("key_messages"),
                      "do_not_say": brief.get("do_not_say"),
                      "cta_ideas": brief.get("cta_ideas")},
            "products": product_facts,
        }

        # ------------------------------------------------------------ copywriting
        try:
            raw = llm.chat(self.COPY_SYSTEM,
                           "Facts (the ONLY facts you may use):\n"
                           + _jdump(context)[:6000],
                           temperature=0.6, max_tokens=600)
            data = _extract_json(raw)
            from copywriter import clean_caption, sanitize_hashtags
            caption = clean_caption(data.get("caption"))
            hashtags = sanitize_hashtags(data.get("hashtags") or [],
                                          HASHTAG_LIMIT, avoid_words=caption)
            if not caption:
                raise AgentError("Copywriting returned an empty caption.")
            self._task(wid, cid, "copywriting", "succeeded",
                       output={"hook": caption[:80]})
        except AgentError as exc:
            self._task(wid, cid, "copywriting", "failed", error=str(exc)[:200])
            self._fail_item(wid, cid, item_id, exc)
            return {"status": "failed", "error": str(exc)[:200]}

        # ------------------------------------------------------------ creative
        visual = {"layout": "brand_card", "overlay_text": "", "colors": [], "note": ""}
        try:
            raw2 = llm.chat(self.CREATIVE_SYSTEM,
                            "Brand palette and post:\n"
                            + _jdump({"brand": brand, "post": context["post"],
                                      "caption_hook": caption[:100],
                                      "campaign_name": campaign.get("name")})[:2500],
                            temperature=0.5, max_tokens=350)
            d2 = _extract_json(raw2)
            layout = str(d2.get("layout") or "").strip().lower()
            visual = {
                "layout": layout if layout in LAYOUTS else "brand_card",
                "overlay_text": str(d2.get("overlay_text") or "")[:60],
                "colors": (brand.get("colors") or [])[:3],
                "note": str(d2.get("note") or "")[:200],
            }
            self._task(wid, cid, "creative", "succeeded",
                       output={"layout": visual["layout"]})
        except AgentError as exc:
            self._task(wid, cid, "creative", "failed", error=str(exc)[:200])
            self._fail_item(wid, cid, item_id, exc)
            return {"status": "failed", "error": str(exc)[:200]}

        # ------------------------------------------------------------ QC
        issues = self._qc(caption, hashtags, brief, item, products)
        status = "failed" if issues else "ready"
        doc = {
            "_id": uuid4().hex, "workspace_id": wid, "campaign_id": cid,
            "item_id": item_id, "day": item.get("day"), "format": item.get("format"),
            "angle": item.get("angle"),
            "caption": caption, "hashtags": hashtags, "visual": visual,
            "qc": {"status": "passed" if not issues else "failed", "issues": issues},
            "status": status, "job_id": job_id,
            "created_at": now(), "updated_at": now(),
        }
        self.store.content.insert_one(doc)
        if issues:
            self._task(wid, cid, "qc", "failed", output={"issues": issues[:6]})
            self._set_item(wid, cid, item_id, {"status": "failed",
                                               "content_id": doc["_id"],
                                               "error": issues[0][:200]})
            self.store.event(wid, "content_failed",
                             "QC rejected a %s post (day %s): %s"
                             % (item.get("format"), item.get("day"), issues[0][:120]))
        else:
            self._task(wid, cid, "qc", "succeeded", output={"issues": []})
            self._set_item(wid, cid, item_id, {"status": "ready",
                                               "content_id": doc["_id"]})
            self.store.event(wid, "content_ready",
                             "Content ready for day %s (%s)"
                             % (item.get("day"), item.get("format")))
        return {"status": status, "content_id": doc["_id"], "issues": issues}

    # ------------------------------------------------------------------ QC
    def _qc(self, caption, hashtags, brief, item, products):
        issues = []
        text = str(caption or "").lower()
        # 1) forbidden phrases from the strategy brief
        for phrase in (brief.get("do_not_say") or []):
            p = str(phrase or "").strip().lower()
            if p and p in text:
                issues.append("forbidden phrase: " + p[:40])
        # 2) numbers must be verified facts: product prices or facts already in
        #    the brief/plan (never invented prices, stats or years)
        allowed = set()
        for p in products:
            allowed |= self._digits(p.get("price"))
        for src in ((brief.get("key_messages") or []) + [item.get("angle") or "",
                    item.get("goal") or "", item.get("note") or ""]):
            allowed |= self._digits(src)
        for num in self._digits(caption):
            if num not in allowed:
                issues.append("unverified number: " + num)
        # 3) risky absolute claims
        for claim in RISKY_CLAIMS:
            if claim in text:
                issues.append("risky claim: " + claim)
        # 4) hashtag discipline (copywriter already caps at 5)
        if len(hashtags) > HASHTAG_LIMIT:
            issues.append("too many hashtags")
        return issues


def _jdump(obj):
    import json
    return json.dumps(obj, default=str)
