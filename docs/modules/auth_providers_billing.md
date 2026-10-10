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
[`../IDENTITY.md`](../IDENTITY.md); HTTP rules in [`../API_CONTRACT.md`](../contracts/API_CONTRACT.md).

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

Credential secrets are sealed as a `tofu.credential-secret/v1` envelope
(`api_key`/`oauth` plus encrypted `extra_headers`); dispatch decodes it at
route materialization and presentation surfaces must never render the raw
envelope as the key. `key_hint` derives from the usable key via
`credential_secret_display_hint` (`headers` marks header-only credentials).
`split_header_secrecy` decides where each provider header lives:
credential-named or transport-reserved names stay inside the encrypted
envelope, and plain routing headers are inspectable Connection
`extra_headers`. Bootstrap's `_repair_credential_secrets` converges early-v2
documents to that split idempotently — conflicted headers stay encrypted so
the envelope-over-connection dispatch precedence is preserved.

## Egress boundary

Every outbound use of a caller-controlled `base_url` passes
`validate_egress_url` at use time. Registration-time validation improves UX but
cannot replace the use-time check because DNS may change.

The guard resolves every address and always denies metadata/link-local,
multicast, reserved, and unspecified destinations. Private and loopback model
servers are supported for self-hosted installs; multi-user deployments should
enable `TOFU_BYO_BLOCK_PRIVATE` and `TOFU_BYO_BLOCK_LOOPBACK`. An allow-list is
an explicit operator decision, not an automatic fallback. Exact HTTPS origins
in `lib.proxy.SUBSCRIPTION_HOSTS` (port 443, no URL userinfo) may defer failed
local DNS to the existing subscription proxy/owner desktop egress boundary.
Resolved dangerous addresses still fail; arbitrary BYO hosts require local
validation. Local DNS failure alone must not remove a subscription model.

Provider discovery, balance probes, ephemeral dispatch slots, and custom-tool
webhooks must reuse this policy. Adding a new network path without the guard is
a security defect.

## Provider dispatch flow

1. The route resolves the authenticated owner/tenant boundary.
2. `lib.model_routing` parses a structured ModelRef or explicit Provider+Offering
   pending identity (`model@provider` rejected); the candidate compiler
   hard-filters authorization, capability, context, health, and price budget
   before ranking ProviderAccess routes. Explicit priority 0 is the highest rank, not a missing-value default.
3. `dispatch_adapter` decrypts only selected Credential references, validates
   egress, retains at most 64 ranked candidates before minting request-scoped slots; the shared LLM
   transport records factual health against Deployment/Connection/Credential. Ranking scans eligible candidates lazily with stable top-K selection; a full primary prefix skips unused model-fallback scans, and fallback excludes the resolved primary identity.
4. The route group is disposed at terminal settlement and a redacted bounded
   RouteSnapshot is persisted with the turn.

Candidate initialization skips only typed missing/invalid credential secrets
and invalid endpoints, retaining later candidates already admitted by the
same routing policy. It does not expand provider pins or authorization.
Storage failures and owner-boundary errors abort initialization and dispose
any handles already minted. If no candidate survives, the first typed
credential failure remains distinguishable from an invalid endpoint.

The native direct-stream relay follows the same steps without persisting a Turn:
its server-minted request record owns billing idempotency while the shared
`ExecutionSession` owns route disposal and the admission lease. Provider binding
uses an execution `ContextVar`, so concurrent asyncio Tasks on one event-loop
thread cannot see one another's request-scoped route group.

Subscription OAuth follows a separate token lifecycle but converges on the same LLM transport. `provider_ready` requires an eligible OAuth route from the owner-scoped candidate compiler; catalog counts, token validity and live health remain separate facts. `lib/oauth/outbound.py` is the sole owner of upstream identity headers; control-plane and request routes must not copy those rules. Codex refresh tokens rejected with a terminal OAuth code are cleared atomically and not retried; the access token is kept only until its recorded expiry, and later requests require a fresh project sign-in. The browser accepts a relayed `oauth_callback` only from the loopback relay origin of a pending flow echoing its state nonce; `exchange_code` requires an exact state match, the manual-paste path (`manual: true`, implied by `callback_url`) being the sole state-fallback exception.
Optional Deployment probes start tasks and requested cells on demand. Launch-probed worker values govern idle reuse, with no active call or matrix-size quota. Idle workers retire; unsaved terminal outcomes remain available as secret-free fallbacks. Results update a Deployment through its owner CAS aggregate, preserving authorization and enabled state.

Daily key health: HTTP 402 stops the provider account key; an ambiguous `insufficient_quota` 429 stops only the observing key/model pair so one vendor behind an aggregate gateway cannot poison siblings. Settings manual ON wins for attended dispatch. Optional background work may enter request-local strict admission where recorded key/model billing stops beat a stale manual ON and last-resort promotion; a wholly rejected pool raises `DispatchNoAdmissibleSlot` (projected retryable `no_slot`/503). Stops are day-scoped; explicit re-enable clears the old stop.

### Codex model catalogue lifecycle

`lib/oauth/codex_catalog.py` keeps a private, reconstructible last-good personal
catalogue: normalized rows, account fingerprint, and ETag, never a token. Login
or row changes reset to three minutes; stable 200/304 responses and errors back
off 6→12→24→48→60 minutes. Logout ends it; unauthenticated launch creates none.
## Codex account usage and earned resets

Quota-window rollover and manually redeemable reset credits are distinct.
Their owner-scoped cache, refresh, Push, logout, and notification lifecycle is
specified in [`../CODEX_ACCOUNT_USAGE.md`](../runbooks/CODEX_ACCOUNT_USAGE.md).

## Billing flow
`lib.cost.compute_cost` is the rate engine used by charging and user-visible
cost projection. Model rates come from `lib/pricing/`; do not add a second live
rate table under billing. Static pricing import does not initialize the shared
HTTP stack; only an explicit online refresh owns that lazy dependency.

Model-id lookups must go through `lib.pricing.pricing_for_model` (or
`lookup_pricing`), never a raw `MODEL_PRICING.get`: the seam folds one trailing
dated-checkpoint stamp (`-MMDD` / `-YYYYMMDD`) so a pinned snapshot
(`deepseek-v4-pro-0813`) inherits its rolling alias's row. A dated pin
therefore needs no pricing row of its own — add the alias it folds to instead.

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
cache. Reserve and settle must share one enablement decision per request; if
billing is disabled after a reservation was created, settlement releases that
durable hold rather than stranding it behind the disabled path.

## Codex subscription recovery

The existing bounded catalog worker doubles stable-poll intervals up to one
hour, but wakes at least five minutes before a stored Codex access token's
expiry. A near-expiry idle poll uses the same cross-process OAuth
singleflight refresh as requests, even when the model catalog is still fresh;
transient catalog/provisioning errors retry within 15 minutes. An explicitly
revoked or absent refresh token is never replayed and cannot be renewed
without the user's authorization. An explicit logout stops the catalog worker
and forgets staged rows only after credential deletion succeeds. Last-admitted
catalog rows remain available during transient network/auth outages, whereas
freshly fetched rows are not published until model-routing admission succeeds.
The repair cannot override deliberately disabled provider access or an explicit
capability/credential restriction; see `docs/MODEL_REGISTRATION.md`.

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
| OAuth wire behavior / Codex earned-reset signal | focused `lib/oauth/` module, `codex_usage.py` | `outbound.py`, provider integration tests, OAuth status projection, logout lifecycle, typed notification tests |
| Daily key health/admission | `lib/key_stats/` | dispatcher picker and optional-work policy tests |
| Model rate / wallet/ledger mutation | `lib/pricing/`, `lib/billing/` | `lib/cost.py` tests, billing settlement, idempotency, rollback, janitor tests |
| Rate policy | `lib/rate_limit_api.py` | shared store and response headers |

## Test map

```bash
pytest -q tests/identity/test_identity_contract.py tests/api/test_api_keys.py tests/model_routing/test_model_routing_contract.py tests/byo_egress/test_byo_egress.py tests/startup/test_secret_envelope_startup_boundary.py
pytest -m unit -q tests/oauth/test_oauth_outbound.py tests/oauth/test_oauth_exchange_errors.py tests/oauth/test_codex_usage_reset.py tests/oauth/test_codex_usage_reset_notice.py tests/llm_dispatch/test_key_stats_model_exhaustion.py tests/llm_dispatch/test_key_stats_no_429_auto_disable.py
pytest -q tests/cost/test_cost.py tests/billing/test_billing.py tests/storage_sidecar/test_rate_limit_store.py
```

Use `rg --files tests | rg '(identity|api_key|model_routing|oauth|billing|cost|rate_limit)'`
to locate renamed focused suites before broadening the gate.
