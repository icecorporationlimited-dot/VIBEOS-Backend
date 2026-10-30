"""business/routes.py - Business Mode HTTP routes for the existing stdlib server.

One authenticated dispatch point: every /web/api/business* request resolves the
web session, checks the CSRF token on mutating calls, and enforces workspace
ownership inside the store queries themselves. One owner can never reach
another owner's business documents.
"""
from __future__ import annotations

import json
import logging
from http.cookies import SimpleCookie

from .agents import AgentError, Orchestrator
from .campaigns import CampaignStore
from .store import owner_key

logger = logging.getLogger(__name__)

PREFIX = "/web/api/business"
SESSION_COOKIE = "__Host-vibeos_session"
CSRF_COOKIE = "__Host-vibeos_csrf"


def _dump(obj):
    return json.dumps(obj, default=str)


def _cookie_value(header, name):
    jar = SimpleCookie()
    try:
        jar.load(header or "")
    except Exception:
        return ""
    morsel = jar.get(name)
    return morsel.value if morsel else ""


class WebBusinessRoutes:
    """Route service in the same style as WebStudio: (status, body, headers)."""

    def __init__(self, cfg, sessions, store):
        self.cfg = cfg
        self.sessions = sessions
        self.store = store

    # ------------------------------------------------------------------ auth
    def _principal(self, cookie_header, headers, mutating):
        """(principal, None) or (None, error_response)."""
        token = _cookie_value(cookie_header, SESSION_COOKIE)
        principal = self.sessions.resolve(token)
        if principal is None:
            return None, (401, '{"error":"Sign in first."}', {})
        if not mutating:
            return principal, None
        cookie_csrf = _cookie_value(cookie_header, CSRF_COOKIE)
        header_csrf = (headers.get("X-CSRF-Token") or "").strip()
        if not cookie_csrf or not header_csrf or cookie_csrf != header_csrf:
            return None, (403, '{"error":"Bad CSRF token."}', {})
        if not self.sessions.validate_csrf(token, header_csrf):
            return None, (403, '{"error":"Session expired. Sign in again."}', {})
        return principal, None

    @staticmethod
    def _body(method, raw):
        if method == "GET" or not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            raise ValueError("Expected a JSON body.")
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object.")
        return data

    # ---------------------------------------------------------------- router
    def handle(self, method, path, cookie_header, headers, raw_body):
        try:
            return self._route(method, path, cookie_header, headers, raw_body)
        except ValueError as exc:
            return 400, _dump({"error": str(exc)[:200]}), {}
        except AgentError as exc:
            return 502, _dump({"error": str(exc)[:200]}), {}
        except Exception:
            logger.exception("Business route failed: %s %s", method, path)
            return 500, '{"error":"Something went wrong."}', {}

    def _route(self, method, path, cookie_header, headers, raw_body):
        principal, err = self._principal(cookie_header, headers, mutating=method != "GET")
        if err:
            return err
        owner = owner_key(principal)
        parts = [p for p in path[len(PREFIX):].split("/") if p]
        data = self._body(method, raw_body)

        if not parts:
            if method == "GET":
                return 200, _dump({"workspaces": self.store.list_for(owner)}), {}
            if method == "POST":
                ws = self.store.create(owner, data)
                return 201, _dump({"workspace": ws}), {}
            return 405, '{"error":"Method not allowed."}', {}

        wid = parts[0]
        rest = parts[1:]
        if not rest:
            if method == "GET":
                ws = self.store.get(owner, wid)
                return (200, _dump({"workspace": ws}), {}) if ws else (
                    404, '{"error":"Workspace not found."}', {})
            if method == "PATCH":
                ws = self.store.update(owner, wid, data)
                return (200, _dump({"workspace": ws}), {}) if ws else (
                    404, '{"error":"Workspace not found."}', {})
            if method == "DELETE":
                ok = self.store.delete(owner, wid)
                return (200, '{"ok": true}', {}) if ok else (
                    404, '{"error":"Workspace not found."}', {})
            return 405, '{"error":"Method not allowed."}', {}

        section = rest[0]
        if section == "activate" and method == "POST":
            ws = self.store.activate(owner, wid)
            return (200, _dump({"workspace": ws}), {}) if ws else (
                404, '{"error":"Workspace not found."}', {})
        if section == "dashboard" and method == "GET":
            dash = self.store.dashboard(owner, wid)
            return (200, _dump(dash), {}) if dash else (
                404, '{"error":"Workspace not found."}', {})
        if section == "events" and method == "GET":
            events = self.store.recent_events(owner, wid)
            return (200, _dump({"events": events}), {}) if events is not None else (
                404, '{"error":"Workspace not found."}', {})
        if section == "products":
            return self._products(method, owner, wid, rest[1:], data)
        if section == "campaigns":
            return self._campaigns(method, owner, wid, rest[1:], data)
        if section == "settings":
            if method == "GET":
                doc = self.store.get_settings(owner, wid)
                return (200, _dump({"settings": doc}), {}) if doc else (
                    404, '{"error":"Workspace not found."}', {})
            if method == "PATCH":
                doc = self.store.update_settings(owner, wid, data)
                return (200, _dump({"settings": doc}), {}) if doc else (
                    404, '{"error":"Workspace not found."}', {})
            return 405, '{"error":"Method not allowed."}', {}
        return 404, '{"error":"Not found."}', {}

    def _products(self, method, owner, wid, rest, data):
        if not rest:
            if method == "GET":
                items = self.store.list_products(owner, wid)
                return (200, _dump({"products": items}), {}) if items is not None else (
                    404, '{"error":"Workspace not found."}', {})
            if method == "POST":
                doc = self.store.add_product(owner, wid, data)
                return (201, _dump({"product": doc}), {}) if doc else (
                    404, '{"error":"Workspace not found."}', {})
            return 405, '{"error":"Method not allowed."}', {}
        pid = rest[0]
        if method == "PATCH":
            doc = self.store.update_product(owner, wid, pid, data)
            return (200, _dump({"product": doc}), {}) if doc else (
                404, '{"error":"Product not found."}', {})
        if method == "DELETE":
            ok = self.store.delete_product(owner, wid, pid)
            return (200, '{"ok": true}', {}) if ok else (
                404, '{"error":"Product not found."}', {})
        return 405, '{"error":"Method not allowed."}', {}

    def _campaigns(self, method, owner, wid, rest, data):
        campaigns = CampaignStore(self.store)
        if not rest:
            if method == "GET":
                items = campaigns.list(owner, wid)
                return (200, _dump({"campaigns": items}), {}) if items is not None else (
                    404, '{"error":"Workspace not found."}', {})
            if method == "POST":
                doc = campaigns.create(owner, wid, data)
                return (201, _dump({"campaign": doc}), {})
            return 405, '{"error":"Method not allowed."}', {}
        cid = rest[0]
        act = rest[1] if len(rest) > 1 else ""
        if not act:
            if method == "GET":
                doc = campaigns.get(owner, wid, cid)
                return (200, _dump({"campaign": doc}), {}) if doc else (
                    404, '{"error":"Campaign not found."}', {})
            if method == "PATCH":
                doc = campaigns.update(owner, wid, cid, data)
                return (200, _dump({"campaign": doc}), {}) if doc else (
                    404, '{"error":"Campaign not found."}', {})
            if method == "DELETE":
                try:
                    ok = campaigns.delete(owner, wid, cid)
                except ValueError as exc:
                    return 400, _dump({"error": str(exc)[:200]}), {}
                return (200, '{"ok": true}', {}) if ok else (
                    404, '{"error":"Campaign not found."}', {})
            return 405, '{"error":"Method not allowed."}', {}
        orchestrator = Orchestrator(self.store)
        if act in ("brief", "plan") and method == "POST":
            doc = (orchestrator.run_strategy(owner, wid, cid) if act == "brief"
                   else orchestrator.run_planning(owner, wid, cid))
            return (200, _dump({"campaign": doc}), {}) if doc else (
                404, '{"error":"Campaign not found."}', {})
        if act in ("start", "pause", "resume", "stop") and method == "POST":
            try:
                doc = campaigns.transition(owner, wid, cid, act)
            except ValueError as exc:
                return 400, _dump({"error": str(exc)[:200]}), {}
            return (200, _dump({"campaign": doc}), {}) if doc else (
                404, '{"error":"Campaign not found."}', {})
        if act == "tasks" and method == "GET":
            tasks = campaigns.tasks(owner, wid, cid)
            return (200, _dump({"tasks": tasks}), {}) if tasks is not None else (
                404, '{"error":"Campaign not found."}', {})
        return 404, '{"error":"Not found."}', {}
