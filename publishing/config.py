"""publishing/config.py — reads the hardcoded values from settings.py."""
from dataclasses import dataclass


@dataclass(frozen=True)
class PublishingConfig:
    app_id: str
    app_secret: str
    public_base_url: str        # https://your-host (callback + media URLs)
    signing_secret: str         # HMAC key for OAuth state + media URLs
    encryption_key: str         # Fernet key for tokens at rest
    graph_version: str = "v26.0"
    scopes: tuple = ("instagram_business_basic",
                     "instagram_business_content_publish")
    http_port: int = 8080
    media_url_ttl: int = 3600   # seconds a signed media URL stays valid
    poll_interval: float = 5.0
    poll_attempts: int = 24     # ~2 minutes of container polling per run
    refresh_before_days: int = 10

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/oauth/callback"


REQUIRED = ("META_APP_ID", "META_APP_SECRET", "PUBLIC_BASE_URL",
            "PUBLISHING_SIGNING_SECRET", "TOKEN_ENCRYPTION_KEY")


def _from_settings():
    import settings
    keys = REQUIRED + ("PORT", "GRAPH_API_VERSION")
    out = {}
    for k in keys:
        v = getattr(settings, k, None)
        # untouched placeholders count as "not configured"
        if v in (None, "") or str(v).startswith("your_") or str(v) == "change_me" \
                or "your-vibeos-host" in str(v):
            continue
        out[k] = str(v)
    return out


def load_config(env=None):
    """Return PublishingConfig, or None if Instagram isn't filled in settings.py yet.

    The bot still runs without these (Phase 0/1 features keep working); the
    Instagram commands just explain what is missing.
    """
    env = _from_settings() if env is None else env
    missing = [k for k in REQUIRED if not env.get(k)]
    if missing:
        return None
    base = env["PUBLIC_BASE_URL"].strip()
    if not base.startswith("https://"):
        # Meta requires HTTPS redirect URIs and Instagram fetches media by URL.
        raise ValueError("PUBLIC_BASE_URL must start with https://")
    return PublishingConfig(
        app_id=env["META_APP_ID"],
        app_secret=env["META_APP_SECRET"],
        public_base_url=base,
        signing_secret=env["PUBLISHING_SIGNING_SECRET"],
        encryption_key=env["TOKEN_ENCRYPTION_KEY"],
        graph_version=env.get("GRAPH_API_VERSION", "v26.0"),
        http_port=int(env.get("PORT", "8080")),
    )


def missing_settings(env=None):
    env = _from_settings() if env is None else env
    return [k for k in REQUIRED if not env.get(k)]
