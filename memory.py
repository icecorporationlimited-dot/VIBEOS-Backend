
# memory.py
from datetime import datetime, timezone

import settings
from pymongo import MongoClient

MONGODB_URI = settings.MONGODB_URI

_client = MongoClient(
    MONGODB_URI,
    serverSelectionTimeoutMS=15000,
    connectTimeoutMS=15000,
)

_db = _client["vibeOS"]
_users = _db["users"]

# Shared handles so the rest of the app uses ONE Mongo connection.
client = _client
db = _db

VALID_STYLES = {"natural", "y2k", "moody", "film", "clean", "golden", "mono",
                # legacy names still accepted (editor resolves them)
                "baddie", "soft"}
DEFAULT_STYLE = "baddie"


def ensure_indexes():
    """Create indexes and verify that MongoDB is reachable."""
    _client.admin.command("ping")
    _users.create_index("telegram_id", unique=True)


def get_preferences(telegram_id) -> dict:
    """Return saved preferences, or safe defaults."""
    user = _users.find_one(
        {"telegram_id": str(telegram_id)},
        {"_id": 0, "vibe": 1, "timezone": 1},
    ) or {}

    vibe = user.get("vibe", DEFAULT_STYLE)
    if vibe not in VALID_STYLES:
        vibe = DEFAULT_STYLE

    return {
        "vibe": vibe,
        "timezone": user.get("timezone", "Asia/Kolkata"),
    }


def save_vibe(telegram_id, vibe: str) -> dict:
    """Save the user's selected content style."""
    vibe = vibe.strip().lower()
    if vibe not in VALID_STYLES:
        raise ValueError(
            f"Invalid vibe. Choose one of: {', '.join(sorted(VALID_STYLES))}"
        )

    now = datetime.now(timezone.utc)

    _users.update_one(
        {"telegram_id": str(telegram_id)},
        {
            "$set": {
                "vibe": vibe,
                "updated_at": now,
            },
            "$setOnInsert": {
                "telegram_id": str(telegram_id),
                "created_at": now,
                "timezone": "Asia/Kolkata",
            },
        },
        upsert=True,
    )

    return get_preferences(telegram_id)


def save_timezone(telegram_id, timezone_name: str) -> dict:
    """Save a timezone after validating the IANA timezone name."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Invalid timezone name.") from None

    now = datetime.now(timezone.utc)

    _users.update_one(
        {"telegram_id": str(telegram_id)},
        {
            "$set": {
                "timezone": timezone_name,
                "updated_at": now,
            },
            "$setOnInsert": {
                "telegram_id": str(telegram_id),
                "vibe": DEFAULT_STYLE,
                "created_at": now,
            },
        },
        upsert=True,
    )

    return get_preferences(telegram_id)


def close():
    """Close the MongoDB client during application shutdown."""
    _client.close()
