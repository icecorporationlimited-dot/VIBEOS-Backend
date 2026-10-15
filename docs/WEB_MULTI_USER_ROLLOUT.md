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

The authenticated browser media route now resolves the current web session and calls `get_media_for()`; inaccessible, missing, or unsupported media returns a generic not-found response. The legacy signed `/media/<token>` endpoint remains for Instagram's server-to-server fetches and is a separate bearer-URL boundary that still needs review. Do not enable public web publishing based on this step alone.

## Publishing job ownership groundwork

- [x] Stamp new Telegram publishing jobs with a namespaced owner ID.
- [x] Scope queue, prefix lookup, retry, cancel, release, and job-owned asset reads through the verified Telegram principal's owner filter, with a legacy-job fallback.
- [x] Add an owner index and focused selector tests.

This remains Telegram-worker groundwork, not end-to-end web publishing support. Worker account/token resolution remains Telegram-specific, and worker state transitions and account lifecycle paths still need a full tenant-boundary audit before public multi-user publishing.

## Remaining before public multi-user publishing

- [x] Add a browser Instagram OAuth flow bound to the authenticated web principal and a one-time browser state cookie.
- [ ] Make publishing jobs, assets/media, autopilot preferences, slots, retries, cancellation, and account lifecycle operations tenant-scoped end-to-end.
- [ ] Ensure workers resolve the owning account and asset only within the job's tenant; audit all database selectors and updates.
- [ ] Add rate limits and operational abuse controls to login and OAuth endpoints.
- [ ] Run all unit tests, Mongo integration tests, and Meta/Instagram provider sandbox tests.
- [ ] Perform a security review and deploy behind correctly configured HTTPS before inviting users.

Do not treat the browser login page as a multi-user publishing dashboard. Browser Instagram account linking is now wired to owner-scoped account storage, but web publishing is not enabled: jobs, assets/media, preferences, scheduling, and worker operations still require end-to-end tenant scoping and audit.
