"""Identity and tenant-scoping primitives for the web-platform migration.

These helpers deliberately do not authenticate a request. A web adapter must
construct a Principal only after validating its authentication mechanism, then
use owner_filter() for every tenant-owned database read/write. Telegram records
remain readable through the legacy fallback until migrated; once a document has
an owner_id, that value always takes precedence over legacy telegram_id data.
"""
from dataclasses import dataclass
import re


_TELEGRAM_ID = re.compile(r"^[0-9]{1,32}$")
_MAX_SUBJECT_LENGTH = 256


class IdentityError(ValueError):
    """Raised when an identity is malformed or cannot be safely scoped."""


@dataclass(frozen=True)
class Principal:
    """A verified identity represented independently of its login provider."""

    kind: str
    subject: str

    def __post_init__(self):
        if self.kind not in {"telegram", "web"}:
            raise IdentityError("Unsupported identity kind.")
        subject = str(self.subject).strip()
        if not subject or len(subject) > _MAX_SUBJECT_LENGTH:
            raise IdentityError("Invalid identity subject.")
        if any(ord(char) < 32 or ord(char) == 127 for char in subject):
            raise IdentityError("Invalid identity subject.")
        if self.kind == "telegram" and not _TELEGRAM_ID.fullmatch(subject):
            raise IdentityError("Telegram identity must be a numeric user ID.")
        object.__setattr__(self, "subject", subject)

    @classmethod
    def from_telegram(cls, telegram_id):
        return cls("telegram", str(telegram_id))

    @classmethod
    def from_web(cls, verified_subject):
        """Build only from a subject supplied by a validated web-auth adapter."""
        return cls("web", str(verified_subject))

    @property
    def owner_id(self):
        """Namespaced tenant key; Telegram and web subjects can never collide."""
        return f"{'tg' if self.kind == 'telegram' else 'web'}:{self.subject}"


def owner_filter(principal, owner_field="owner_id", legacy_telegram_field="telegram_id"):
    """Return a MongoDB filter that scopes access to exactly one principal.

    Web principals never fall back to Telegram fields. For Telegram, legacy
    records without owner_id remain visible during migration. A record with an
    owner_id is matched only by that owner_id, even if it also has a stale or
    conflicting telegram_id field.
    """
    if not isinstance(principal, Principal):
        raise IdentityError("A verified Principal is required.")
    if not owner_field or not legacy_telegram_field:
        raise IdentityError("Ownership field names must be non-empty.")
    if principal.kind == "web":
        return {owner_field: principal.owner_id}
    return {
        "$or": [
            {owner_field: principal.owner_id},
            {
                "$and": [
                    {owner_field: {"$exists": False}},
                    {legacy_telegram_field: principal.subject},
                ]
            },
        ]
    }


def owns_document(principal, document, owner_field="owner_id",
                  legacy_telegram_field="telegram_id"):
    """In-memory equivalent of owner_filter(), useful at service boundaries."""
    if not isinstance(principal, Principal) or not isinstance(document, dict):
        return False
    if owner_field in document:
        return document.get(owner_field) == principal.owner_id
    return (
        principal.kind == "telegram"
        and str(document.get(legacy_telegram_field, "")) == principal.subject
    )
