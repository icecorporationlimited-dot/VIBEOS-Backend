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
- `GET /web`: signed-in page with an Instagram connection status and sign-out form.
- `POST /web/instagram/connect`: requires same-origin Origin and session-bound CSRF; starts Instagram OAuth with a short-lived, HttpOnly, browser-bound state cookie.
- `GET /web/instagram/callback`: validates and consumes one-time state, verifies the initiating browser still has the same authenticated session, exchanges the code using the exact callback URI, then stores the encrypted token under that session's verified web principal.
- `POST /logout`: requires same-origin Origin, the session-bound CSRF token, and the CSRF cookie; revokes the session and clears cookies.

Mongo collections used: `web_users`, `oidc_login_transactions`, `web_sessions`, `web_ig_oauth_transactions`, and the existing `ig_accounts`. Indexes are created at runtime.

## Important limits

This adds browser Instagram account linking only; it does not enable web publishing. Publishing jobs, preferences, media/assets, scheduler, retries, and worker operations are not yet consistently tenant-scoped for web principals. The account disconnect UI is not implemented yet. Do not advertise or deploy web-user publishing until those operations are tenant-scoped end-to-end and audited.

Deploy only behind a correctly configured HTTPS reverse proxy. Configure the provider redirect URI exactly, store client secrets in deployment secrets, and run the full test suite plus provider integration tests before public launch.
