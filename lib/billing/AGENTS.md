# Billing guidance

## Scope

This package owns wallet, reservation/settlement, ledger, and cleanup policy.
Canonical model rates live in `lib/pricing/`. Read
`docs/modules/auth_providers_billing.md`.

## Editing rules

- Every operation is owner/account scoped and uses stable idempotency keys.
  Unknown foreign resources fail without revealing their existence.
- Reservation, request outcome, ledger append, wallet update, refund/release,
  and emitted event obey one atomic state transition vocabulary.
- Use canonical decimal/rate and usage calculations. Do not copy price tables or
  infer billable usage from UI/provider text.
- Lost acknowledgement, retry, timeout, cancellation, partial provider usage,
  and janitor recovery must not double-charge or mint balance.
- Keep ledger durability distinct from bounded derived summaries/caches. Never
  delete financial evidence as reconstructible cleanup.
- Redact credentials and sensitive provider data while retaining auditable
  request/usage/idempotency attribution.

## Verification

Run focused billing, cost, pricing, idempotency, rollback, settlement, and
janitor tests, plus the Sidecar transaction cases for changed ledger operations.
