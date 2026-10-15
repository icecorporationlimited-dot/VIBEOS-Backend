# Phase 2 — Instagram setup (one time)

All secrets are hardcoded in ONE file: `settings.py` (no .env, no environment variables).
Never upload/share/commit it (it is in .gitignore). `settings.example.py` is the blank template.

1. Instagram account -> Settings -> Account type -> switch to **Professional** (Business or Creator).
   Use a TEST account first. Stories publishing is documented for Business accounts.
2. developers.facebook.com -> Create app (type **Business**) -> add product
   **Instagram API with Instagram Login**.
3. Copy **Instagram app ID / secret** into `settings.py` (META_APP_ID / META_APP_SECRET).
4. Add your account as an **Instagram Tester** (App roles -> Roles) and accept the invite in
   Instagram (Settings -> Apps and websites -> Tester invites).
5. Set **Valid OAuth redirect URI** to `<PUBLIC_BASE_URL>/oauth/callback`.
6. Fill the rest of `settings.py` (PUBLIC_BASE_URL, PUBLISHING_SIGNING_SECRET, TOKEN_ENCRYPTION_KEY,
   BOT_TOKEN, MONGODB_URI, GROQ_API_KEY), deploy, make sure the port is reachable over HTTPS.
7. In Telegram: `/connect` -> approve -> `/post` -> tap Post.

Going live for other people's accounts needs Meta **App Review** for
`instagram_business_content_publish` (+ Business Verification). Not needed for your own test account.
