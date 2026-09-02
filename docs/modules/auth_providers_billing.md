# Authentication, providers, and billing

This security/money boundary owns principals, credentials, OAuth, owner-scoped
BYO providers, safe egress, charging, and rate limits. Identity lives in
[`../IDENTITY.md`](../IDENTITY.md); HTTP rules in [`../API_CONTRACT.md`](../API_CONTRACT.md).

## Ownership

| Concern | Owner |
|---|---|
| Request authentication boundary | `routes/api_v1/auth.py`, `lib/auth_mode.py` |
| Principal and ownership types | `lib/identity.py` |
| API credential verification/CRUD | `lib/api_keys/` |
| BYO provider repository | `lib/byo_providers.py` |
| BYO model resolution/lifecycle | `lib/byo_resolve.py`, `lib/llm_dispatch/ephemeral.py` |
| Caller-controlled egress policy | `lib/byo_egress.py` |
| Subscription OAuth | `lib/oauth/` |
| Codex account usage / earned resets | `lib/oauth/codex_usage.py` |
| Provider-specific outbound identity | `lib/oauth/outbound.py` |
| Pricing and cost math | `lib/pricing/`, `lib/cost.py` |
| Wallet and ledger | `lib/billing/wallet.py`, `ledger.py` |
| Reserve/settle choreography | `lib/billing/request_flow.py` |
| Rate limiting | `lib/rate_limit_api.py`, `rate_limit_store.py` |
| HTTP adapters | `routes/api_v1/auth.py`, `keys.py`, `providers.py`, `oauth.py`, `billing.py` |

## Identity boundary

Authentication produces immutable `AuthContext`; repositories use explicit
`owner_user_id`, never a bearer-key credential as owner. Handlers default deny
through `require_scope`, and storage services receive the authenticated owner.

No current-user global enters domain/repository code. Tests, desktop bridges,
and headless adapters cross the production auth or capability boundary.

## Secret storage

Credentials, OAuth tokens, and BYO keys use owner-scoped Sidecar operations.
BYO keys are authenticated ciphertext via `lib/secret_envelope.py`; only
internal outbound lookup decrypts them, and HTTP projections expose hints.

Provider rows are owner/tenant keyed; public `prov_*` IDs are locators, not
authorization. Mutations are atomic Sidecar commands, never plaintext JSON.

`sanitise_extra_headers` rejects injected authorization, cookies, host/proxy
selection, and framing headers.

## Egress boundary

Every outbound use of a caller-controlled `base_url` passes
`validate_egress_url` at use time. Registration-time validation improves UX but
cannot replace the use-time check because DNS may change.

The guard resolves every address and always denies metadata/link-local,
multicast, reserved, and unspecified destinations. Private and loopback model
servers are supported for self-hosted installs; multi-user deployments should
enable `TOFU_BYO_BLOCK_PRIVATE` and `TOFU_BYO_BLOCK_LOOPBACK`. An allow-list is
an explicit operator decision, not an automatic fallback.

Provider discovery, balance probes, ephemeral dispatch slots, and custom-tool
webhooks must reuse this policy. Adding a new network path without the guard is
a security defect.

## Provider dispatch flow

1. The route resolves the authenticated repository owner.
2. `byo_providers.resolve_model_string` interprets `<model>@<provider>` within
   that owner boundary.
3. Plain global models return without initializing the dispatcher.
4. Inline or registered BYO loads/decrypts its provider, validates egress, and activates a request-scoped `llm_dispatch.ephemeral` slot.
5. The shared LLM transport performs the request.
6. The ephemeral slot is disposed at request completion.

Subscription OAuth follows a separate token lifecycle but converges on the
same LLM transport. `lib/oauth/outbound.py` remains the sole owner of upstream
identity headers; control-plane and request routes must not copy those rules.

### Codex model catalogue lifecycle

`lib/oauth/codex_catalog.py` keeps a private, reconstructible last-good personal
catalogue: normalized rows, account fingerprint, and ETag, never a token. Login
or real row change resets to three minutes; stable 200/304 responses and errors
back off 6→12→24→48→60 minutes, matching cache freshness. Logout ends the
worker; unauthenticated launch creates none.

Distributed startup never attaches the legacy global token to a tenant.
`TODO(enterprise)`: enumerate eligible owners through an owner-scoped catalogue
repository; until then request-time resolution retains static/last-good fallback.

## Codex account usage and earned resets

Codex exposes two different reset concepts and they must never be conflated:

- quota-window `resets_at` / `reset_at` means scheduled rolling-window
  rollover;
- `rate_limit_reset_credits.available_count` from the authenticated Codex
  account-usage response means the account owns an earned, manually redeemable
  reset credit.

`lib/subscription_quota.py` continues to project coarse primary/secondary
window percentages observed on successful model responses.
`lib/oauth/codex_usage.py` separately reads the structured account entitlement
from `GET /backend-api/wham/usage`; a positive count may be enriched from the
bounded details response. The underlying `/wham/*` routes are private upstream
APIs, so their paths and parser live in one module and fixture tests pin the
currently verified shape. UI code never scrapes Codex TUI strings.

`GET /api/v1/oauth/status` is non-blocking. For an authenticated Codex account
its `reset_offer` projection has an explicit `state` of `available`, `none`, or
`unknown`, plus `available_count`, freshness, and a stable opaque
`notification_key` when available. Missing fields, HTTP failures, decoding
failures, and account-switch races are `unknown`, never zero. A stale read
starts one daemon refresh scoped by the authenticated `owner_user_id` and a
hash of the ChatGPT account ID; the request returns the last projection
immediately.

The reconstructible cache is mode 0600, contains no token or raw account ID,
keeps at most 16 owner/account rows, uses a 30-minute success TTL, permits at
most two process-local refresh threads, and applies a bounded failure retry.
Network I/O is singleflight per owner/account on 16 bounded identity-hashed
lock stripes, but never holds the shared cache write lock; unrelated owners can
refresh concurrently and logout is not blocked by an upstream timeout, while
historical account churn cannot create unbounded lock sidecars. Logout in `lib/oauth/manager/_exchange.py` is
the lifecycle authority: only after credential deletion succeeds does it clear
passive quota and the identified account's reset-credit projection; an absent
account identity never broad-clears the cache.

There is no ownerless periodic server worker. The proactive notice performs one
lazy startup check, a 30-minute visible-page check, and at most six short
re-polls while a refresh is running. The Settings panel separately permits at
most eight two-second re-polls while it remains open, covering the bounded
usage-plus-details reads without creating a permanent timer.

A fresh positive offer appears persistently in Settings and once as a global
notification. Browser deduplication retains at most 16 opaque notification
keys, so reloads, tabs, and account switches do not turn one credit into
repeated noise. Tofu never redeems a reset automatically; consuming a one-time
entitlement requires a separate explicitly confirmed, idempotent command.

## Billing flow

`lib.cost.compute_cost` is the rate engine used by charging and user-visible
cost projection. Model rates come from `lib/pricing/`; do not add a second live
rate table under billing. Static pricing import does not initialize the shared
HTTP stack; only an explicit online refresh owns that lazy dependency.

For billed requests, `lib/billing/request_flow.py` owns the full sequence:

1. estimate and reserve before dispatch;
2. settle the reservation against terminal usage;
3. append an idempotent ledger entry and update the wallet in one transaction;
4. reclaim abandoned reservations through one durably claimed scheduler task.

`wallet_janitor.py` alone queries bounded stale reserves, skips known-running
requests, and uses settlement's idempotent release; `janitor.py` is thread-free
compatibility. Its durable five-minute task runs only for multi-user billing,
so open/private/BYO-only modes poll zero times. `TOFU_BILLING_JANITOR=0` and the
legacy TTL alias remain. Distributed scheduling invents no owner;
`TODO(enterprise)` requires owner enumeration and durable in-flight leases.

The append-only ledger is authoritative; wallet balance is a recomputable
cache. Refunds are positive ledger entries, not history rewrites. Billing may
be disabled for private/BYO-only deployments, but reserve and settle must use
the same enablement decision for a request.

## Failure semantics

- Missing/invalid credentials: `401`.
- Authenticated principal without required scope: `403`.
- Unknown owner-scoped provider: `404`, without revealing another owner's row.
- Invalid provider input or rejected egress: `400`.
- Insufficient wallet reservation: `402`.
- Rate-limit rejection: `429` with the published limit headers.
- Upstream/provider failure: typed dependency error; secrets never enter the
  response or normal logs.
- Codex reset-credit detection failure: cached state is stale or `unknown`;
  never manufacture `available_count: 0` and never block OAuth status reads.

## Invariants

- Authentication is resolved once at middleware; authorization defaults deny.
- Owner and tenant are explicit at every repository call.
- Credential IDs never substitute for ownership identity.
- Secrets are encrypted at rest and redacted at every public projection.
- Caller-controlled egress has one use-time SSRF guard.
- Cost math has one engine; ledger append and wallet update are atomic.
- Reservation and settlement are idempotent and crash-recoverable.
- Rate limiting is shared-store capable; process-local counters are not an
  authority for multi-worker deployments.
- Routes only parse/project; storage and business decisions live below them.
- Quota reset timestamps never imply an earned reset credit.
- Earned reset checks and prompt histories are owner/account scoped and bounded.
- Detection may be automatic; one-time entitlement consumption never is.

## Change routing

| Change | Start here | Required neighbors |
|---|---|---|
| Principal/scope semantics | `lib/identity.py`, auth middleware | `contracts/identity_v1.yaml`, API contract |
| API-key lifecycle | `lib/api_keys/` | Sidecar identity operations, redaction tests |
| BYO provider field | `lib/byo_providers.py` | Sidecar provider schema/operations, route schema |
| New provider network call | `lib/byo_egress.py` | use-time guard tests and failure redaction |
| OAuth wire behavior | focused `lib/oauth/` module | `outbound.py`, provider integration tests |
| Codex earned-reset signal | `lib/oauth/codex_usage.py` | OAuth status projection, logout lifecycle, typed notification tests |
| Model rate | `lib/pricing/` | `lib/cost.py` tests and billing settlement |
| Wallet/ledger mutation | `lib/billing/` | idempotency, rollback, janitor tests |
| Rate policy | `lib/rate_limit_api.py` | shared store and response headers |

## Test map

```bash
pytest -q tests/test_identity_contract.py tests/test_api_keys.py
pytest -q tests/test_byo_providers.py tests/test_byo_egress.py
pytest -m unit -q tests/test_oauth_outbound.py tests/test_oauth_exchange_errors.py
pytest -m unit -q tests/test_codex_usage_reset.py tests/test_codex_usage_reset_notice.py
pytest -q tests/test_cost.py tests/test_cost_estimator.py
pytest -q tests/test_billing.py tests/test_rate_limit_store.py
```

Use `rg --files tests | rg '(identity|api_key|byo|oauth|billing|cost|rate_limit)'`
to locate renamed focused suites before broadening the gate.
