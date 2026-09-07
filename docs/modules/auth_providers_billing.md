# Authentication, providers, and billing

## Tofu-DB wallet and ledger pre-authority

The seven wallet/ledger operations compile through Tofu-DB Transaction IR.
Billing subjects remain explicit opaque IDs inside a narrowly enumerated
tenant-global scope. Every applied amount atomically publishes the immutable
ledger entry, exact global ID and nonempty reference claims, wallet balance,
constant-time user sum/count, descending list index, active-reserve projection,
receipt, and encrypted outbox record. Checked signed arithmetic rejects
overflow, insufficient debit writes nothing, and filtered lists fail closed
after 10,000 candidates or 8 MiB. Payment, redemption-code, and stale-reserve
operations remain default-denied until their complete state machines land.

This security/money boundary owns principals, credentials, OAuth, owner-scoped
ProviderAccess resources, safe egress, charging, and rate limits. Identity lives in
[`../IDENTITY.md`](../IDENTITY.md); HTTP rules in [`../API_CONTRACT.md`](../API_CONTRACT.md).

## Ownership

| Concern | Owner |
|---|---|
| Request authentication boundary | `routes/api_v1/auth.py`, `lib/auth_mode.py` |
| Principal and ownership types | `lib/identity.py` |
| API credential verification/CRUD | `lib/api_keys/` |
| Model/provider authority | `lib/model_routing/repository.py`, `domain.py` |
| Request route resolution/lifecycle | `lib/model_routing/routing.py`, `dispatch_adapter.py` |
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

Credential and OAuth secrets use owner-scoped Sidecar operations. Model-routing
secrets are authenticated ciphertext via `lib/secret_envelope.py`; only
request route materialization decrypts them, and HTTP projections expose hints.
Importing provider metadata or the secret-envelope contract does not initialize
Fernet or touch the personal key file. Cryptography and the deployment key load
only at an explicit seal/open/bound-payload operation; binding validation and
failure behavior remain identical there.

ProviderAccess aggregates and credential references are owner/tenant keyed;
Provider IDs are locators, not authorization. Aggregate mutations use revision
CAS and secret writes use a separate encrypted operation, never plaintext JSON.

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

1. The route resolves the authenticated owner/tenant boundary.
2. `lib.model_routing` parses a structured official ModelRef or an explicit
   Provider+Offering pending identity; `model@provider` is rejected.
3. The candidate compiler hard-filters authorization, capability, context,
   health, and price budget before ranking ProviderAccess routes.
4. `dispatch_adapter` decrypts only selected Credential references, validates
   egress, and mints at most 64 request-scoped ephemeral slots.
5. The shared LLM transport performs the request and records factual health
   against Deployment, Connection, Credential, or Credential×Deployment.
6. The route group is disposed at terminal settlement and a redacted bounded
   RouteSnapshot is persisted with the turn.

The native direct-stream relay follows the same steps without persisting a
Turn: its server-minted request record owns billing idempotency and terminal
usage, while the shared `ExecutionSession` owns route disposal and an exact
admission lease. Task-backed API/compat routes bind the same resources to the
private session carried by `TaskRuntime`. Provider binding uses an execution
`ContextVar`, so concurrent asyncio Tasks on one event-loop thread cannot see
one another's request-scoped route group.

Subscription OAuth follows a separate token lifecycle but converges on the same LLM transport. `lib/oauth/outbound.py` remains the sole owner of upstream identity headers; control-plane and request routes must not copy those rules.
Codex refresh tokens rejected with a terminal OAuth code are cleared atomically while the current access token is retained only until its recorded expiry. The rejection is not retried; later requests require a fresh project sign-in instead of replaying a revoked refresh token or sending an expired access token.
The browser accepts a relayed `oauth_callback` only from the loopback relay origin of a pending flow echoing that flow's state nonce (login records the marker; a mid-flow reload re-arms it from the status projection). `exchange_code` requires an exact state match whenever the flow recorded one — a stateless non-manual callback is rejected without touching the pending flow; the manual-paste path (`manual: true`, implied by `callback_url`) is the one state-fallback exception, and a pasted state still must match.
Optional Deployment probes use one launch-probed, provider-fair finite lane;
aggregate cell calls stay inside the shared read-only tool budget (hard maximum
eight), queued closures are finite, idle workers retire, and construction is
bounded at 4,096 cells. Probe results are diagnostic evidence used to update a
Deployment through its owner CAS aggregate; they never recreate alias pools or
mutate configured enablement from transient health.

Daily key health distinguishes temporary 429 backpressure from a recorded billing stop. HTTP 402 stops the provider account key; ambiguous `insufficient_quota` 429 stops only the observing key/model pair so one vendor behind an aggregate gateway cannot poison its siblings. Settings manual ON continues to win for attended interactive dispatch. Optional background work may instead enter a request-local strict admission context: recorded key/model billing stops then win over a stale manual ON and over last-resort promotion, while healthy sibling models and providers remain eligible. A pool rejected entirely by that policy raises `DispatchNoAdmissibleSlot` before transport and without cooldown polling or direct-LLM fallback; translation projects it as retryable `no_slot`/503. Stops remain day-scoped; explicit re-enable clears the old stop, and the next fresh stop is still recorded for both UI diagnosis and strict optional-work admission.

### Codex model catalogue lifecycle

`lib/oauth/codex_catalog.py` keeps a private, reconstructible last-good personal
catalogue: normalized rows, account fingerprint, and ETag, never a token. Login
or row changes reset to three minutes; stable 200/304 responses and errors back
off 6→12→24→48→60 minutes. Logout ends it; unauthenticated launch creates none.
Distributed startup never attaches the legacy global token to a tenant;
`TODO(enterprise)`: enumerate eligible owners via an owner-scoped repository.

## Codex account usage and earned resets

Quota-window rollover and manually redeemable reset credits are distinct.
Their owner-scoped cache, refresh, Push, logout, and notification lifecycle is
specified in [`../CODEX_ACCOUNT_USAGE.md`](../CODEX_ACCOUNT_USAGE.md).

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
the same enablement decision for a request. If billing is disabled after a
reservation was created, settlement releases that durable hold rather than
stranding it behind the disabled path.

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
- Rate limiting is shared-store capable; process-local counters are not an authority for multi-worker deployments. The memory backend keys dynamic paths by route template, retains only launch-probed finite LRU buckets, API-key token pairs, and exact timestamps, and cleans each bucket against its own window; capacity eviction preserves fail-open availability. Sidecar events carry exact expiries and prune an age-indexed bounded batch on every check, including across one-shot identities.
- Routes only parse/project; storage and business decisions live below them.
- Quota reset timestamps never imply an earned reset credit.
- Earned reset checks and prompt histories are owner/account scoped and bounded.
- Detection may be automatic; one-time entitlement consumption never is.

## Change routing

| Change | Start here | Required neighbors |
|---|---|---|
| Principal/scope semantics | `lib/identity.py`, auth middleware | `contracts/identity_v1.yaml`, API contract |
| API-key lifecycle | `lib/api_keys/` | Sidecar identity operations, redaction tests |
| Model-routing entity/policy | `lib/model_routing/` | v2 contract, Sidecar operation, API and migration tests |
| New provider network call | `lib/byo_egress.py` | use-time guard tests and failure redaction |
| OAuth wire behavior | focused `lib/oauth/` module | `outbound.py`, provider integration tests |
| Codex earned-reset signal | `lib/oauth/codex_usage.py` | OAuth status projection, logout lifecycle, typed notification tests |
| Daily key health/admission | `lib/key_stats/` | dispatcher picker and optional-work policy tests |
| Model rate | `lib/pricing/` | `lib/cost.py` tests and billing settlement |
| Wallet/ledger mutation | `lib/billing/` | idempotency, rollback, janitor tests |
| Rate policy | `lib/rate_limit_api.py` | shared store and response headers |

## Test map

```bash
pytest -q tests/test_identity_contract.py tests/test_api_keys.py
pytest -q tests/test_model_routing_contract.py tests/test_byo_egress.py \
  tests/test_secret_envelope_startup_boundary.py
pytest -m unit -q tests/test_oauth_outbound.py tests/test_oauth_exchange_errors.py
pytest -m unit -q tests/test_codex_usage_reset.py tests/test_codex_usage_reset_notice.py
pytest -m unit -q tests/test_key_stats_model_exhaustion.py tests/test_key_stats_no_429_auto_disable.py
pytest -q tests/test_cost.py
pytest -q tests/test_billing.py tests/test_rate_limit_store.py
```

Use `rg --files tests | rg '(identity|api_key|model_routing|oauth|billing|cost|rate_limit)'`
to locate renamed focused suites before broadening the gate.
