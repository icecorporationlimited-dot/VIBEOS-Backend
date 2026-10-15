"""
Connected Instagram accounts and encrypted token lifecycle.

Legacy Telegram methods remain compatible. The owner-scoped methods accept a
verified Principal so web accounts can be stored under namespaced owner_id
values without pretending a browser subject is a Telegram user ID.
"""
import logging
from datetime import datetime, timedelta, timezone

from .graph import GraphError
from .identity import Principal, owner_filter

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
        # The old non-sparse unique Telegram index treats missing telegram_id
        # values as null, which would permit only one web-owned account. Replace
        # that index once with a sparse unique index while preserving Telegram
        # uniqueness for records that actually have a Telegram ID.
        if hasattr(self.col, "list_indexes"):
            for index in list(self.col.list_indexes()):
                key = dict(index.get("key", {}))
                if (key == {"telegram_id": 1} and index.get("unique") and
                        not index.get("sparse")):
                    self.col.drop_index(index["name"])
        self.col.create_index("telegram_id", unique=True, sparse=True)
        self.col.create_index("owner_id", unique=True, sparse=True)

    @staticmethod
    def _filter_for(principal):
        return owner_filter(principal)

    def connect(self, telegram_id, token_info) -> dict:
        """Backward-compatible Telegram account connect entry point."""
        return self.connect_for(Principal.from_telegram(telegram_id), token_info)

    def connect_for(self, principal, token_info) -> dict:
        """Verify token ownership with Instagram and store it for this principal."""
        if not isinstance(principal, Principal):
            raise TypeError("A verified Principal is required.")
        token = token_info["access_token"]
        me = self.graph.me(token)
        ig_id = str(me.get("user_id") or me.get("id"))
        now = self.clock()
        doc = {
            "owner_id": principal.owner_id,
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
        if principal.kind == "telegram":
            # Preserve old consumers and indexes during the gradual migration.
            doc["telegram_id"] = principal.subject
        existing = self.get_for(principal)
        if existing:
            self.col.update_one({"_id": existing["_id"]}, {"$set": doc})
        else:
            self.col.insert_one(doc)
        return self.get_for(principal)

    def get(self, telegram_id):
        """Backward-compatible Telegram account lookup."""
        return self.get_for(Principal.from_telegram(telegram_id))

    def get_for(self, principal):
        if not isinstance(principal, Principal):
            raise TypeError("A verified Principal is required.")
        return self.col.find_one(self._filter_for(principal))

    def disconnect(self, telegram_id):
        return self.disconnect_for(Principal.from_telegram(telegram_id))

    def disconnect_for(self, principal):
        self.col.update_one(
            self._filter_for(principal),
            {"$set": {"status": DISCONNECTED, "updated_at": self.clock()},
             "$unset": {"token_enc": ""}})

    def mark_needs_reauth(self, telegram_id, reason=""):
        return self.mark_needs_reauth_for(Principal.from_telegram(telegram_id), reason)

    def mark_needs_reauth_for(self, principal, reason=""):
        self.col.update_one(
            self._filter_for(principal),
            {"$set": {"status": NEEDS_REAUTH, "reauth_reason": reason,
                      "updated_at": self.clock()}})

    def get_token(self, telegram_id):
        """Backward-compatible Telegram token lookup."""
        return self.get_token_for(Principal.from_telegram(telegram_id))

    def get_token_for(self, principal):
        """Return (Instagram user ID, plaintext token) for this principal only."""
        acct = self.get_for(principal)
        if not acct or acct.get("status") == DISCONNECTED or not acct.get("token_enc"):
            raise NotConnected("No Instagram account connected. Use /connect.")
        if acct["status"] == NEEDS_REAUTH:
            raise ReauthRequired("Instagram login expired. Use /connect again.")

        now = self.clock()
        expires = _aware(acct["token_expires_at"])
        if expires <= now:
            self.mark_needs_reauth_for(principal, "token_expired")
            raise ReauthRequired("Instagram login expired. Use /connect again.")

        token = self.cipher.decrypt(acct["token_enc"])
        if expires - now <= self.refresh_before:
            token = self._refresh(principal, acct, token)
        return acct["ig_user_id"], token

    def _refresh(self, principal, acct, token):
        try:
            fresh = self.oauth.refresh(token)
        except GraphError as exc:
            if exc.auth:
                self.mark_needs_reauth_for(principal, "refresh_rejected")
                raise ReauthRequired("Instagram login expired. Use /connect again.") from None
            logger.warning("Token refresh failed (will retry later): %s", exc)
            return token  # still valid for now; try again next call
        now = self.clock()
        # Re-check ownership atomically at write time as well as at read time;
        # an account ID alone is not an authorization boundary.
        selector = {"$and": [
            {"_id": acct["_id"]},
            self._filter_for(principal),
        ]} if "_id" in acct else self._filter_for(principal)
        self.col.update_one(
            selector,
            {"$set": {"token_enc": self.cipher.encrypt(fresh["access_token"]),
                      "token_expires_at": now + timedelta(seconds=fresh["expires_in"]),
                      "last_refresh_at": now, "updated_at": now}})
        return fresh["access_token"]

    def expiring_soon(self, within_days=7):
        """Legacy Telegram accounts needing expiry alerts."""
        limit = self.clock() + timedelta(days=within_days)
        return [a for a in self.col.find({"status": ACTIVE, "telegram_id": {"$exists": True}})
                if _aware(a["token_expires_at"]) <= limit]

    def needing_reauth(self):
        """Legacy Telegram accounts needing reauth alerts."""
        return list(self.col.find({"status": NEEDS_REAUTH, "telegram_id": {"$exists": True}}))

    def mark_alerted(self, telegram_id, kind, marker):
        """Remember that a Telegram user was told about marker only once."""
        self.col.update_one({"telegram_id": str(telegram_id)},
                            {"$set": {f"alert_{kind}_for": marker}})
