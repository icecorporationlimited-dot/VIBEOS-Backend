"""settings.py — ALL secrets and configuration live here, hardcoded, in ONE file.

Edit the values between the quotes. Nothing is read from .env or the
environment any more. NEVER upload/share/commit this file (it is in .gitignore).
"""

# ---------------- Telegram / Database / AI ----------------
BOT_TOKEN = "your_new_telegram_bot_token"
MONGODB_URI = "your_new_mongodb_uri"
GROQ_API_KEY = "your_new_groq_key"
VISION_MODEL = "qwen/qwen3.8-27b"

# ---------------- Instagram publishing (Phase 2) ----------------
# Meta developer app -> Instagram API with Instagram Login -> app ID / secret
META_APP_ID = "your_instagram_app_id"
META_APP_SECRET = "your_instagram_app_secret"

# Public HTTPS URL of this server (Railway/Render domain or ngrok). Must start with https://
PUBLIC_BASE_URL = "https://your-vibeos-host.example"

# Random string. Generate: python -c "import secrets;print(secrets.token_urlsafe(32))"
PUBLISHING_SIGNING_SECRET = "change_me"

# Fernet key. Generate: python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"
# Do not change it after users connect Instagram, or stored tokens can't be decrypted.
TOKEN_ENCRYPTION_KEY = "change_me"

# ---------------- Optional ----------------
PORT = 8080                 # set to your host's port if it requires a specific one
GRAPH_API_VERSION = "v26.0"
