# OAuth guidance

## Scope

This package owns OAuth provider-specific exchange, refresh, status, logout, and
safe upstream access. Read `docs/modules/auth_providers_billing.md` and
`docs/IDENTITY.md`.

## Editing rules

- Keep account identity, repository owner identity, credential identity, and
  provider subject distinct and explicit.
- `outbound.py` is the OAuth network boundary. Validate destinations through the
  canonical egress guard and never follow a redirect into a newly unauthorized
  destination.
- Store refresh/access tokens only through the credential vault/Sidecar identity
  operations. Never log or return tokens, verifier secrets, raw headers, or
  upstream bodies.
- Exchange, refresh, logout, and earned-reset/status behavior are idempotent
  where applicable and scoped to the authenticated owner/account.
- Bound caches, lock stripes, network calls, retries, histories, and proactive
  refresh. No ownerless background worker or unbounded per-account lock map.
- Map upstream failures to the canonical typed/auth error vocabulary without
  leaking whether another owner's account exists.

## Verification

Run focused OAuth outbound, exchange error, refresh/logout, owner-isolation,
redaction, and earned-reset tests. Add identity and provider-dispatch tests when
the public account projection changes.
