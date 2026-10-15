# OIDC login integration boundary

This increment provides the OIDC Authorization Code + PKCE exchange and ID-token
validation service. It is not wired to HTTP routes and does not itself issue
VIBEOS browser sessions.

## Security behavior

- Configured issuer and callback must use HTTPS.
- Discovery issuer must exactly match the configured issuer; authorization,
  token, and JWKS endpoints must use HTTPS.
- Login uses cryptographically random state and nonce, PKCE S256, a 10-minute
  one-time Mongo transaction, and server-side code verifier storage.
- State is stored as a SHA-256 digest and consumed once before token exchange.
- ID tokens are verified against the provider JWKS, configured issuer and client
  audience, with a strict algorithm allowlist and required claims. Nonce is
  compared in constant time.
- Users are keyed by issuer-qualified subject, never email. Email is profile
  data only and its verification flag is recorded only when explicitly true.

## Required before public deployment

1. Configure a trusted OIDC provider, client credentials, and exact HTTPS callback.
2. Add HTTP routes that call begin/complete, apply rate limits, handle errors
   safely, and issue sessions with WebSessionStore only after complete succeeds.
3. Validate login CSRF/state in the callback and never accept a client-supplied
   subject as an authenticated identity.
4. Integrate account linking and tenant-scoped database operations; this does not
   yet connect Instagram accounts to web users.
5. Run provider integration tests and the full regression suite before deployment.
