# Web multi-user rollout checklist

This document tracks the security boundary required before enabling public multi-user publishing.

## Completed foundation

- [x] Escape OAuth callback HTML and add baseline response security headers.
- [x] Add namespaced `Principal` identities and ownership query helpers.
- [x] Add expiring server-side browser sessions and CSRF validation.
- [x] Add OIDC Authorization Code + PKCE login and claim validation.
- [x] Add browser login/logout routes.
- [x] Support deployment environment variables for OIDC credentials.
- [x] Add owner-scoped Instagram account storage methods while preserving Telegram entry points.

## Remaining before public multi-user publishing

- [ ] Add a browser Instagram OAuth flow bound to the authenticated web principal and a one-time browser state cookie.
- [ ] Make publishing jobs, assets/media, autopilot preferences, slots, retries, cancellation, and account lifecycle operations tenant-scoped end-to-end.
- [ ] Ensure workers resolve the owning account and asset only within the job's tenant; audit all database selectors and updates.
- [ ] Add rate limits and operational abuse controls to login and OAuth endpoints.
- [ ] Run all unit tests, Mongo integration tests, and Meta/Instagram provider sandbox tests.
- [ ] Perform a security review and deploy behind correctly configured HTTPS before inviting users.

Do not treat the browser login page as a multi-user publishing dashboard. The new account-storage methods are groundwork and are not yet wired to a browser Instagram OAuth route or publishing worker.
