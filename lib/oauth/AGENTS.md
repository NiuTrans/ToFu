# OAuth guidance

## Scope

This package owns OAuth provider-specific exchange, refresh, status, logout, and
safe upstream access. Read `docs/modules/auth_providers_billing.md` and
`docs/IDENTITY.md`.

Shared local/desktop Codex login is owned by `codex_shared_login.py` and the
stdlib-only `codex_local_login.py` / `codex_command_login.py`; source/refresh/opt-out rules live in
`docs/IDENTITY.md`. Verify with `tests/oauth/test_codex_shared_login.py` and
`tests/oauth/test_codex_command_login.py`.

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
- Bound reconstructible caches, network calls, retries and histories. Active
  identity locks retire with their holders/waiters; no ownerless background
  worker or permanently growing per-account lock map.
- Map upstream failures to the canonical typed/auth error vocabulary without
  leaking whether another owner's account exists.

## Verification

Run focused OAuth outbound, exchange error, refresh/logout, owner-isolation,
redaction, and earned-reset tests. Add identity and provider-dispatch tests when
the public account projection changes.

native_credentials owns production native Codex grant/source checks around the
existing outbound resolver. Verify native-credentials and outbound/refresh tests;
use the actual native archive refusal test. Keep per-invocation source identity,
owner/tenant/account binding and first-party destination fencing; no new vault.

`identity_flight.py` owns exact refresh/redemption exclusion across threads and
POSIX processes. Its persistent guard is short-lived; active files reclaim only
under that guard after a kernel lock probe. Verify process/fork/crash/error
cases in `test_identity_flight.py` and account redemption/refresh tests before
changing inode lifecycle or fallbacks.
