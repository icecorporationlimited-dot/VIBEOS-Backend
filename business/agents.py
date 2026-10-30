"""business/agents.py - Phase 2 agents: Strategy, Planning + Orchestrator.

Real LLM calls through the shared OpenAI-compatible provider class
(engagement.openrouter_ai.OpenRouterAI on the Groq endpoint) using Business
Mode's OWN key: GROQ_BUSINESS_API_KEY - never the personal GROQ_API_KEY.
No fabricated output: the prompts may only
use facts that exist in the workspace (products, brand, audience), and when no
provider is configured the agent task fails with a clear error instead of
inventing anything. Every agent run is recorded in business_agent_tasks.
"""
from __future__ import annotations

import json
import logging
import os
import re
from uuid import uuid4

from .campaigns import FORMATS, MAX_PLAN_ITEMS, CampaignStore
from .store import now

logger = logging.getLogger(__name__)


class AgentError(RuntimeError):
    """An agent could not complete its task. Nothing was fabricated."""


def _llm():
    """Business Mode uses its OWN Groq account, separate from personal mode.

    Reads GROQ_BUSINESS_API_KEY / GROQ_BUSINESS_MODEL (settings.py or env).
    It deliberately never falls back to the personal GROQ_API_KEY, so the two
    modes never share quota or billing.
    """
    try:
        from engagement.openrouter_ai import GROQ_URL, OpenRouterAI
    except ImportError:
        raise AgentError("AI provider module unavailable.") from None
    import settings
    api_key = (os.environ.get("GROQ_BUSINESS_API_KEY")
               or str(getattr(settings, "GROQ_BUSINESS_API_KEY", "") or "")).strip()
    if not api_key:
        raise AgentError(
            "Business Mode needs its own Groq key. Set GROQ_BUSINESS_API_KEY.")
    model = (os.environ.get("GROQ_BUSINESS_MODEL")
             or str(getattr(settings, "GROQ_BUSINESS_MODEL", "") or "")
             or "llama-3.3-70b-versatile")
    return OpenRouterAI(api_key, model, api_url=GROQ_URL)


def _extract_json(text):
    text = str(text or "")
    fence = re.search(r"\`\`\`(?:json)?\s*(.+?)\`\`\`", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise AgentError("AI did not return JSON.")
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        raise AgentError("AI returned malformed JSON.") from None


def _perf_block(campaign):
    """Phase 5 feedback loop: the Planning agent sees the last optimisation
    (built ONLY from real Instagram metrics) so the next plan adjusts."""
    optimisation = campaign.get("optimisation") or {}
    if not optimisation.get("summary"):
        return ""
    data = {"summary": optimisation.get("summary"),
            "recommendations": optimisation.get("recommendations"),
            "numbers": optimisation.get("numbers")}
    return ("\n\nPrevious campaign performance (REAL Instagram metrics only - "
            "the numbers and recommendations from the last optimisation run; "
            "use them to adjust angles and formats, never invent numbers):\n"
            + json.dumps(data, default=str)[:1800])


class Orchestrator:
    """Coordinates the Business Mode agents. Phase 2: strategy + planning."""

    STRATEGY_SYSTEM = (
        "You are the Strategy agent of a social-media growth engine for small "
        "businesses. You turn a business profile into a campaign brief. Rules: "
        "use ONLY the facts provided; never invent prices, awards, reviews, "
        "statistics, claims or comparisons; if information is missing, write "
        "generic, safe guidance instead of guessing; keep every item short. "
        "Return ONLY valid JSON."
    )

    PLAN_SYSTEM = (
        "You are the Planning agent of a social-media growth engine. Turn the "
        "campaign brief into a day-by-day content plan. Rules: reference ONLY "
        "the products given (use the given price exactly or no price at all; "
        "never invent availability, events or offers); vary formats; keep "
        "every item short. Return ONLY valid JSON."
    )

    def __init__(self, store):
        self.store = store
        self.campaigns = CampaignStore(store)

    # -------------------------------------------------------------- task log
    def _start_task(self, workspace_id, campaign_id, agent, data):
        doc = {"_id": uuid4().hex, "workspace_id": workspace_id,
               "campaign_id": campaign_id, "agent": agent,
               "status": "running", "input": data or {},
               "output": None, "error": None,
               "created_at": now(), "finished_at": None}
        self.store.agent_tasks.insert_one(doc)
        return doc["_id"]

    def _finish_task(self, task_id, output=None, error=None):
        self.store.agent_tasks.update_one(
            {"_id": task_id},
            {"$set": {"status": "failed" if error else "succeeded",
                      "output": output, "error": error,
                      "finished_at": now()}})

    def _get(self, owner, wid, cid):
        ws = self.store.get(owner, wid)
        if ws is None:
            return None, None
        return ws, self.campaigns.get(owner, wid, cid)

    # -------------------------------------------------------------- strategy
    def run_strategy(self, owner, wid, cid):
        ws, campaign = self._get(owner, wid, cid)
        if ws is None or campaign is None:
            return None
        if campaign.get("status") == "stopped":
            raise AgentError("This campaign is stopped.")
        task = self._start_task(wid, cid, "strategy",
                                {"objective": campaign.get("objective")})
        brand = ws.get("brand") or {}
        audience = ws.get("audience") or {}
        facts = {
            "business": ws.get("name"), "category": ws.get("category"),
            "description": ws.get("description"), "location": ws.get("location"),
            "website": ws.get("website"),
            "brand": {"personality": brand.get("personality"), "voice": brand.get("voice"),
                      "language": brand.get("language"), "guidelines": brand.get("guidelines")},
            "audience": audience, "goals": ws.get("goals"),
            "campaign": {"name": campaign.get("name"),
                         "objective": campaign.get("objective"),
                         "duration_days": campaign.get("duration_days")},
        }
        user = (
            "Business facts (the ONLY facts you may use):\n"
            + json.dumps(facts, default=str)[:5500]
            + "\n\nWrite a campaign brief as JSON with keys: "
            + '"positioning" (1-2 sentences), "key_messages" (3-5 short strings), '
            + '"content_angles" (4-6 short strings), "do_not_say" (2-4 short strings '
            + "the writers must avoid), "cta_ideas" (2-3 short strings). "
            + "Write it in the business's content language."
        )
        try:
            raw = _llm().chat(self.STRATEGY_SYSTEM, user, temperature=0.4, max_tokens=700)
            data = _extract_json(raw)
            brief = {
                "positioning": str(data.get("positioning") or "")[:500],
                "key_messages": [str(m)[:200] for m in (data.get("key_messages") or []) if m][:5],
                "content_angles": [str(m)[:200] for m in (data.get("content_angles") or []) if m][:6],
                "do_not_say": [str(m)[:200] for m in (data.get("do_not_say") or []) if m][:4],
                "cta_ideas": [str(m)[:160] for m in (data.get("cta_ideas") or []) if m][:3],
                "generated_at": now(),
            }
            if not brief["positioning"] or not brief["key_messages"]:
                raise AgentError("Strategy brief was incomplete; nothing saved.")
        except AgentError as exc:
            self._finish_task(task, error=str(exc)[:200])
            raise
        except Exception as exc:
            self._finish_task(task, error=str(exc)[:200])
            raise AgentError("Strategy agent failed; try again shortly.") from None
        self.campaigns.set_brief(wid, cid, brief)
        self._finish_task(task, output={"positioning": brief["positioning"]})
        self.store.event(wid, "strategy_brief",
                         "Strategy brief generated for '" + campaign.get("name", "") + "'")
        return self.campaigns.get(owner, wid, cid)

    # -------------------------------------------------------------- planning
    def run_planning(self, owner, wid, cid):
        ws, campaign = self._get(owner, wid, cid)
        if ws is None or campaign is None:
            return None
        if campaign.get("status") == "stopped":
            raise AgentError("This campaign is stopped.")
        brief = campaign.get("brief")
        if not brief:
            raise AgentError("Generate the strategy brief first.")
        task = self._start_task(wid, cid, "planning",
                                {"objective": campaign.get("objective")})
        products = [p for p in (self.store.list_products(owner, wid) or [])
                    if p.get("availability") != "unavailable"][:20]
        product_facts = [{"name": p.get("name"), "description": p.get("description"),
                          "price": p.get("price"), "currency": p.get("currency"),
                          "promotional": p.get("promotional")} for p in products]
        settings = self.store.get_settings(owner, wid) or {}
        try:
            duration = int(campaign.get("duration_days") or 14)
        except (TypeError, ValueError):
            duration = 14
        try:
            daily = int(settings.get("daily_posts") or 2)
        except (TypeError, ValueError):
            daily = 2
        target = max(1, min(MAX_PLAN_ITEMS, daily * duration))
        user = (
            "Campaign brief:\n" + json.dumps(brief, default=str)[:3500]
            + _perf_block(campaign)
            + "\n\nProducts/services the business actually offers (the ONLY "
              "products you may reference; use the given price exactly or no "
              "price at all):\n"
            + json.dumps(product_facts, default=str)[:3000]
            + "\n\nCampaign duration: " + str(duration) + " days. Objective: "
            + str(campaign.get("objective")) + ".\n\nProduce exactly "
            + str(target) + " planned posts spread across the duration, mixing "
              "formats. Return ONLY JSON of the shape {\"items\": [{\"day\": 1.."
            + str(duration) + ", \"format\": \"feed\"|\"reel\"|\"story\", "
              "\"angle\": \"short description\", \"goal\": \"short goal\", "
              "\"note\": \"optional short note\"}]}"
        )
        try:
            raw = _llm().chat(self.PLAN_SYSTEM, user, temperature=0.5, max_tokens=900)
            data = _extract_json(raw)
            items = []
            for it in (data.get("items") or [])[:MAX_PLAN_ITEMS]:
                if not isinstance(it, dict):
                    continue
                try:
                    day = int(it.get("day"))
                except (TypeError, ValueError):
                    continue
                if not (1 <= day <= duration):
                    continue
                fmt = str(it.get("format") or "feed").strip().lower()
                items.append({
                    "id": uuid4().hex, "day": day,
                    "format": fmt if fmt in FORMATS else "feed",
                    "angle": str(it.get("angle") or "")[:200],
                    "goal": str(it.get("goal") or "")[:120],
                    "note": str(it.get("note") or "")[:200],
                    "status": "planned",
                })
            items = items[:target]
            if not items:
                raise AgentError("Planning agent returned no usable items; nothing saved.")
        except AgentError as exc:
            self._finish_task(task, error=str(exc)[:200])
            raise
        except Exception as exc:
            self._finish_task(task, error=str(exc)[:200])
            raise AgentError("Planning agent failed; try again shortly.") from None
        self.campaigns.set_plan(wid, cid, items)
        self._finish_task(task, output={"items": len(items)})
        self.store.event(wid, "campaign_planned",
                         "Content plan generated for '" + campaign.get("name", "")
                         + "' (" + str(len(items)) + " items)")
        return self.campaigns.get(owner, wid, cid)
