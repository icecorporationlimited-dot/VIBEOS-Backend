# Final deploy checklist (public multi-user Telegram bot)

Every Telegram user connects and manages only their own Instagram account.
All data is keyed by the user's Telegram ID; button taps re-check ownership.

## Before going public
1. Set these environment variables on Render (never commit them):
   BOT_TOKEN, MONGODB_URI, GROQ_API_KEY, META_APP_ID, META_APP_SECRET,
   TOKEN_ENCRYPTION_KEY (Fernet key, never change after users connect),
   PUBLISHING_SIGNING_SECRET (32+ random characters; Render can generate it).
2. If real keys were ever committed (old `settings.py` versions), rotate them.
3. In the Meta app: add `https://<your-host>/oauth/callback` as the redirect URI.
   Public users need Meta App Review (instagram_business_basic and
   instagram_business_content_publish) and the app in Live mode; in Development
   mode only added testers can connect.
4. Use the Render `starter` plan or higher so the worker stays awake.
5. Run `pip install -r requirements-dev.txt` and the tests once on your machine.
6. Test the whole flow with a private/test Instagram account first.

## Built-in protections
- Per-user rate limits (docs/RATE_LIMITING.md).
- Per-user rolling 24h upload cap: USER_DAILY_UPLOADS (default 300).
- Encrypted tokens, signed expiring media URLs (1h), HTTPS-only base URL.
- Container runs as a non-root user.

## Not in scope / known limits
- Web login pages exist in the code but the product is the Telegram bot;
  web publishing is intentionally disabled (worker skips web-owned jobs).
- Rate-limit counters are in memory (single bot instance). For multiple
  instances, move them to MongoDB/Redis.
