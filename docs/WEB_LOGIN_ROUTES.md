# Browser login route integration

This increment exposes a minimal browser login flow through the existing publishing HTTP server when OIDC settings are configured.

## Configure

For production, set these deployment environment variables using your hosting platform's secret/configuration settings:

- `OIDC_ISSUER`: exact HTTPS issuer URL
- `OIDC_CLIENT_ID`: registered client ID
- `OIDC_CLIENT_SECRET`: client secret (keep it in deployment secrets)

Environment variables take precedence over matching values in `settings.py`. For local development, the three values may still be set in `settings.py`. Configure all three together; a partial configuration fails closed.

Register `https://YOUR_PUBLIC_HOST/oidc/callback` as the exact redirect URI. The existing publishing server still requires its Meta publishing configuration and HTTPS public base URL to start.

## Routes

- `GET /login`: begins OIDC Authorization Code + PKCE and sets a short-lived, HttpOnly browser state cookie.
- `GET /oidc/callback`: checks the callback state against the initiating browser cookie, validates OIDC claims, provisions/updates the provider-subject user, then issues a web session.
- `GET /web`: minimal signed-in page with a sign-out form.
- `POST /logout`: requires same-origin Origin, the session-bound CSRF token, and the CSRF cookie; revokes the session and clears cookies.

Mongo collections used: `web_users`, `oidc_login_transactions`, and `web_sessions`. Indexes are created at runtime.

## Important limits

This is a login/session foundation, not a complete multi-user publishing dashboard. Instagram OAuth remains Telegram-bound, and the publishing accounts/jobs/preferences/media operations are not yet consistently tenant-scoped for web principals. Do not advertise or deploy web-user Instagram publishing until those operations and account-linking flow are completed and audited.

Deploy only behind a correctly configured HTTPS reverse proxy. Configure the provider redirect URI exactly, store client secrets in deployment secrets, and run the full test suite plus provider integration tests before public launch.
