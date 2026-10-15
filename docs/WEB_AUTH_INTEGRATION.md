# Web authentication integration boundary

This increment adds server-side session storage, not a login route or a complete
authentication provider.

## Session guarantees

- Random opaque bearer tokens are returned once; only SHA-256 token digests are
  stored in MongoDB.
- Sessions are revocable, expire after a bounded lifetime (8 hours by default,
  never more than 24 hours), and are invalidated when a linked user is disabled.
- CSRF tokens are random, stored as digests, and validated against the same live
  session with constant-time comparison.
- Cookie helpers set Secure, HttpOnly, SameSite=Lax, and Path=/ using the
  __Host-vibeos_session cookie name.
- MongoDB TTL index support is provided for expired-session cleanup; application
  code also checks expiry itself.

## Required before public deployment

1. Integrate a real OIDC/OAuth identity provider and validate signature, issuer,
   audience, expiry, nonce/state, and redirect URI. Never trust unverified claims.
2. Link verified provider subjects to user records after the provider login flow;
   this module refuses to issue sessions for unknown or disabled users.
3. Integrate cookies and CSRF validation into web request handlers; enforce HTTPS.
4. Add login throttling, account recovery, verified email policy, security logging,
   and session rotation after authentication/privilege changes.
5. Scope every tenant-owned database operation and Instagram OAuth state to the
   authenticated user. Do not accept owner IDs from clients.
6. Run full regression tests against MongoDB and the existing Telegram workflows.

No public web login/dashboard routes are exposed by this increment.
