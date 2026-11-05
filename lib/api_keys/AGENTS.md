# Credential boundary guidance

This package mints credentials and adapts semantic storage identity operations.
Read `docs/IDENTITY.md` and `contracts/identity_v1.yaml` for authority and ownership.

- `transport` selects an untrusted candidate from supplied transport values.
  Preserve Bearer, x-api-key, cookie and optional query precedence; an explicit
  invalid Bearer never falls through to a lower-priority identity. No request
  globals, authentication, storage access or secret-bearing diagnostic repr.
- `_validate` reads current credential/account authority on every request.
  Its audit-touch schedule is not an authentication-result cache. `_crud`
  keeps owner/tenant identity explicit and never changes a credential's tier.
- HTTP middleware and WebSocket admission own their separate lifecycle and
  synthetic-admin rules. Selection does not grant identity or execution rights.

Verify credential transport, open-mode cross-site and push authentication,
request identity, credential lifecycle and current-read/audit-touch tests.
