# Web multi-user rollout: identity and tenant boundaries

## This increment

- Adds a provider-neutral `Principal` model with namespaced tenant keys.
- Adds MongoDB ownership filters and an in-memory ownership check.
- Keeps Telegram identities compatible with existing records that only have
  `telegram_id`, while ensuring an explicit `owner_id` always takes priority.
- Adds regression tests for cross-tenant access and identity collisions.

## Important limitation

This is a migration foundation, **not a complete web login or dashboard**.
The helpers do not authenticate requests, create sessions, or authorize a route
by themselves. A web handler must validate its identity provider/session before
constructing a web principal and must use ownership filters on every tenant-owned
query and mutation. Do not expose a public dashboard until that integration,
CSRF/session protections, migration, and end-to-end isolation tests are complete.

## Safe integration sequence

1. Choose and configure a real authentication provider; validate issuer, audience,
   signature, expiry, and state/nonce as applicable.
2. Create the principal only from verified claims, never from a request parameter.
3. Apply `owner_filter(principal)` to every media, account, job, schedule, analytics,
   and preference read/write. For updates/deletes, combine it with the document ID
   in the same database predicate.
4. Migrate Telegram-owned documents to `owner_id = "tg:<telegram_id>"` with a
   backfill and reconciliation report before removing legacy fallback.
5. Link Instagram OAuth state to the authenticated principal and persist the
   connected account under that owner; never accept a client-supplied owner ID.
6. Run cross-user regression tests against the real repository/service layer
   before deploying public routes.
7. Preserve current Telegram command handlers and their existing data access
   semantics while migration proceeds.
