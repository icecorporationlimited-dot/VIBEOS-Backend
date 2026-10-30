"""Business Mode for VIBEOS: workspaces, brand profiles, campaigns, AI agents.

Phase 1 (this package's foundation): business workspaces, onboarding, brand
profile, product catalogue, automation settings and an append-only audit
event log - each strictly owned by one web principal, in their OWN Mongo
collections. The personal creator experience is untouched.

Later phases (campaigns, agents, content factory, publishing, analytics) build
on business.store.BusinessStore and add their own collections next to these.
"""
from .store import BusinessStore, CATEGORIES, GOALS, owner_key
from .routes import WebBusinessRoutes
from .agents import AgentError, Orchestrator
from .campaigns import CampaignStore

__all__ = ["BusinessStore", "WebBusinessRoutes", "CATEGORIES", "GOALS", "owner_key"]
