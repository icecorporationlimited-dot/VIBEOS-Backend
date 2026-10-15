"""
publishing/accounts.py — connected Instagram accounts + token lifecycle.

Tokens are stored encrypted. A long-lived token lasts ~60 days and can be
refreshed once it is >= 24h old and before it expires; get_token() refreshes
automatically when it is within `refresh_before_days` of expiry. If Instagram
says the token is dead we mark the account `needs_reauth` so jobs stop and the
user is told to /connect again (Phase 3 turns this into an alert).
"""
import logging
from datetime import datetime, timedelta, timezone

from .graph import GraphError

logger = logging.getLogger(__name__)

ACTIVE, NEEDS_REAUTH, DISCONNECTED = "active", "needs_reauth", "disconnected"


class NotConnected(Exception):
    pass


class ReauthRequired(Exception):
    pass


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class AccountStore:
    def __init__(self, collection, cipher, oauth, graph, refresh_before_days=10,
                 clock=_now):
        self.col, self.cipher, self.oauth, self.graph = collection, cipher, oauth, graph
        self.refresh_before = timedelta(days=refresh_before_days)
        self.clock = clock

    def ensure_indexes(self):
        self.col.create_index("telegram_id", unique=True)

    def connect(self, telegram_id, token_info) -> dict:
        """Verify the token against /me, then upsert the account."""
        token = token_info["access_token"]
        me = self.graph.me(token)
        ig_id = str(me.get("user_id") or me.get("id"))
        now = self.clock()
        doc = {
            "ig_user_id": ig_id,
            "username": me.get("username"),
            "account_type": me.get("account_type"),
            "token_enc": self.cipher.encrypt(token),
            "token_expires_at": now + timedelta(seconds=token_info["expires_in"]),
            "scopes": token_info.get("scopes", []),
            "status": ACTIVE,
            "connected_at": now,
            "last_refresh_at": now,
            "updated_at": now,
        }
        self.col.update_one({"telegram_id": str(telegram_id)},
                            {"$set": doc,
                             "$setOnInsert": {"telegram_id": str(telegram_id)}},
                            upsert=True)
        return self.get(telegram_id)

    def get(self, telegram_id):
        return self.col.find_one({"telegram_id": str(telegram_id)})

    def disconnect(self, telegram_id):
        self.col.update_one(
            {"telegram_id": str(telegram_id)},
            {"$set": {"status": DISCONNECTED, "updated_at": self.clock()},
             "$unset": {"token_enc": ""}})

    def mark_needs_reauth(self, telegram_id, reason=""):
        self.col.update_one(
            {"telegram_id": str(telegram_id)},
            {"$set": {"status": NEEDS_REAUTH, "reauth_reason": reason,
                      "updated_at": self.clock()}})

    def get_token(self, telegram_id):
        """Return (ig_user_id, plaintext_token), refreshing if due."""
        acct = self.get(telegram_id)
        if not acct or acct.get("status") == DISCONNECTED or not acct.get("token_enc"):
            raise NotConnected("No Instagram account connected. Use /connect.")
        if acct["status"] == NEEDS_REAUTH:
            raise ReauthRequired("Instagram login expired. Use /connect again.")

        now = self.clock()
        expires = _aware(acct["token_expires_at"])
        if expires <= now:
            self.mark_needs_reauth(telegram_id, "token_expired")
            raise ReauthRequired("Instagram login expired. Use /connect again.")

        token = self.cipher.decrypt(acct["token_enc"])
        if expires - now <= self.refresh_before:
            token = self._refresh(telegram_id, acct, token)
        return acct["ig_user_id"], token

    def _refresh(self, telegram_id, acct, token):
        try:
            fresh = self.oauth.refresh(token)
        except GraphError as exc:
            if exc.auth:
                self.mark_needs_reauth(telegram_id, "refresh_rejected")
                raise ReauthRequired("Instagram login expired. Use /connect again.") from None
            logger.warning("Token refresh failed (will retry later): %s", exc)
            return token  # still valid for now; try again next call
        now = self.clock()
        self.col.update_one(
            {"telegram_id": str(telegram_id)},
            {"$set": {"token_enc": self.cipher.encrypt(fresh["access_token"]),
                      "token_expires_at": now + timedelta(seconds=fresh["expires_in"]),
                      "last_refresh_at": now, "updated_at": now}})
        return fresh["access_token"]

    def expiring_soon(self, within_days=7):
        """Active accounts whose token expires soon (for the Phase 3 alerter)."""
        limit = self.clock() + timedelta(days=within_days)
        return [a for a in self.col.find({"status": ACTIVE})
                if _aware(a["token_expires_at"]) <= limit]

    def needing_reauth(self):
        """Accounts whose login is dead (for the Phase 3 alerter)."""
        return list(self.col.find({"status": NEEDS_REAUTH}))

    def mark_alerted(self, telegram_id, kind, marker):
        """Remember that the user was told about `marker` (so we alert once, not every tick)."""
        self.col.update_one({"telegram_id": str(telegram_id)},
                            {"$set": {f"alert_{kind}_for": marker}})
