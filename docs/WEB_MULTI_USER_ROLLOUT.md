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

## Tenant-scoped media access groundwork

- [x] Add `ContentBuffer.get_media_for(principal, asset_id, variant=None)`, which scopes asset lookup through the verified principal's owner filter and returns a generic not-found result for inaccessible IDs.
- [x] Add regression tests for cross-web-user denial, web-vs-Telegram separation, legacy Telegram compatibility, and requiring a verified `Principal`.

This is only a safe access primitive. Existing internal `get_media(asset_id)` remains for trusted legacy callers, and the browser media route has not been migrated to this helper in this change. Do not enable public web publishing based on this step alone.

## Remaining before public multi-user publishing

- [x] Add a browser Instagram OAuth flow bound to the authenticated web principal and a one-time browser state cookie.
- [ ] Make publishing jobs, assets/media, autopilot preferences, slots, retries, cancellation, and account lifecycle operations tenant-scoped end-to-end.
- [ ] Ensure workers resolve the owning account and asset only within the job's tenant; audit all database selectors and updates.
- [ ] Add rate limits and operational abuse controls to login and OAuth endpoints.
- [ ] Run all unit tests, Mongo integration tests, and Meta/Instagram provider sandbox tests.
- [ ] Perform a security review and deploy behind correctly configured HTTPS before inviting users.

Do not treat the browser login page as a multi-user publishing dashboard. Browser Instagram account linking is now wired to owner-scoped account storage, but web publishing is not enabled: jobs, assets/media, preferences, scheduling, and worker operations still require end-to-end tenant scoping and audit.
