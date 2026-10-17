"""settings.py — reads everything from environment variables (Render/Docker friendly).

No secrets live in this file, so it is safe to commit. On your own PC you can
still hardcode values by creating `settings_local.py` (git-ignored) with the
same names, e.g.  BOT_TOKEN = "123:abc".
"""
import os


def _env(name, default=""):
    return os.environ.get(name, default)


# ---------------- Telegram / Database / AI ----------------
BOT_TOKEN = _env("BOT_TOKEN")
MONGODB_URI = _env("MONGODB_URI")
GROQ_API_KEY = _env("GROQ_API_KEY")
VISION_MODEL = _env("VISION_MODEL", "qwen/qwen3.8-27b")
OPENROUTER_API_KEY = _env("OPENROUTER_API_KEY")
OPENROUTER_MODEL = _env("OPENROUTER_MODEL", "openai/gpt-4o-mini")
# Trend Stories: themed + multi-step verified (see trends/verify.py)
TREND_SMART = True
TREND_ALLOW_SOLEMN = True   # condolence Stories for confirmed losses (>=3 publishers)

# Extra trend sources (all optional; empty = that source is skipped)
GNEWS_API_KEY = _env("GNEWS_API_KEY")
REDDIT_CLIENT_ID = _env("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = _env("REDDIT_CLIENT_SECRET")

# ---------------- Instagram publishing ----------------
META_APP_ID = _env("META_APP_ID")
META_APP_SECRET = _env("META_APP_SECRET")

# Render sets RENDER_EXTERNAL_URL automatically (https://yourapp.onrender.com)
PUBLIC_BASE_URL = _env("PUBLIC_BASE_URL") or _env("RENDER_EXTERNAL_URL")

PUBLISHING_SIGNING_SECRET = _env("PUBLISHING_SIGNING_SECRET")
# Fernet key. Never change it after users connect Instagram.
TOKEN_ENCRYPTION_KEY = _env("TOKEN_ENCRYPTION_KEY")

# Web dashboard publishing. OFF by default: set WEB_PUBLISHING=1 on Render only after
# you tested with your own account (see docs/WEB_PUBLISHING_PART_C.md).
WEB_PUBLISHING = _env("WEB_PUBLISHING")

# ---------------- Optional ----------------
# Render injects PORT (default 10000); the web server binds to it.
PORT = int(_env("PORT", "8000"))
GRAPH_API_VERSION = _env("GRAPH_API_VERSION", "v26.0")

# ---------------- Optional local override (not committed) ----------------
try:
    from settings_local import *  # noqa: F401,F403
except ImportError:
    pass
