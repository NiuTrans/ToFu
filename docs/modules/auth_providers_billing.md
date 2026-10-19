# Authentication, providers, and billing

## Tofu-DB wallet and ledger pre-authority

The seven wallet/ledger operations compile through Tofu-DB Transaction IR. Native continuation settles only fresh apiRounds.taskId credits while inherited quotes still cover cumulative tokens; verify native cost/settlement and v4 billed resume tests, with authority/evidence in [DEVELOPER_RUNTIME.md](../DEVELOPER_RUNTIME.md). Billing subjects remain explicit opaque IDs inside a narrowly enumerated tenant-global scope. Every applied amount atomically publishes the immutable ledger entry, exact global ID and nonempty reference claims, wallet balance, constant-time user sum/count, descending list index, active-reserve projection, receipt, and encrypted outbox record. Checked signed arithmetic rejects overflow, insufficient debit writes nothing, and filtered lists fail closed after 10,000 candidates or 8 MiB. Payment, redemption-code, and stale-reserve operations remain default-denied until their complete state machines land.

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
| Caller-controlled egress policy | `lib/byo/byo_egress.py` |
| Subscription OAuth | `lib/oauth/` |
| Codex account usage / earned resets | `lib/oauth/codex_usage.py` |
| Provider-specific outbound identity | `lib/oauth/outbound.py` |
| Pricing and cost math | `lib/pricing/`, `lib/cost.py` |
| Wallet and ledger | `lib/billing/wallet.py`, `ledger.py` |
| Reserve/settle choreography | `lib/billing/request_flow.py` |
| Rate limiting | `lib/rate_limit/rate_limit_api.py`, `rate_limit_store.py` |
| HTTP adapters | `routes/api_v1/auth.py`, `keys.py`, `providers.py`, `oauth.py`, `billing.py` |

## Identity boundary

Authentication produces immutable `AuthContext`; repositories use explicit
`owner_user_id`, never a bearer-key credential as owner. Handlers default deny
through `require_scope`, and storage services receive the authenticated owner.

API credentials have no requests-per-minute quota. New credentials default `rate_limit_rpm` to zero; the field remains accepted as deprecated metadata, and historical nonzero values do not constrain throughput. No request-rate headers or RPM backend mutations are emitted. Explicit `rate_limit_tpd` spending policy still governs model-token expenditure and its outage policy. Personal spending observations refill before their admission decision. Personal spending state retains one observation per configured credential for the process lifetime. Unrelated credentials never evict unspent allowance or negative token debt. Distributed spending authority and outage policy retain their existing lifecycle. Capacity/eviction gauges no longer imply a quota. Endpoint/IP frequency decorators and the `TOFU_RATE_LIMIT` switch are removed.

Open-mode grants have no per-peer request quota. `TOFU_OPEN_MODE_RPM` and
`TOFU_OPEN_MODE_EXEMPT_PATHS` are retired and ignored. The authentication
boundary still decides whether a remote peer can receive a synthetic grant;
private-mode credentials, scopes, revocation, and owner checks still apply.

No current-user global enters domain/repository code. Tests, desktop bridges,
and headless adapters cross the production auth or capability boundary.

## Secret storage

Credential and OAuth secrets use owner-scoped Sidecar operations. Model-routing secrets are authenticated ciphertext via `lib/secret_envelope.py`; only request route materialization decrypts them, and HTTP projections expose hints. Importing provider metadata or the secret-envelope contract does not initialize Fernet or touch the personal key file. Cryptography and the deployment key load only at an explicit seal/open/bound-payload operation; binding validation and failure behavior remain identical there.

ProviderAccess aggregates and credential references are owner/tenant keyed;
Provider IDs are locators, not authorization. Aggregate mutations use revision
CAS and secret writes use a separate encrypted operation, never plaintext JSON.

`sanitise_extra_headers` rejects injected authorization, cookies, host/proxy
selection, and framing headers.

Credential secrets are sealed as a `tofu.credential-secret/v1` envelope (`api_key`/`oauth` plus encrypted `extra_headers`); dispatch decodes it at route materialization and presentation surfaces must never render the raw envelope as the key. `key_hint` derives from the usable key via `credential_secret_display_hint` (`headers` marks header-only credentials). `split_header_secrecy` decides where each provider header lives: credential-named or transport-reserved names stay inside the encrypted envelope, and plain routing headers are inspectable Connection `extra_headers`. Bootstrap's `_repair_credential_secrets` converges early-v2 documents to that split idempotently — conflicted headers stay encrypted so the envelope-over-connection dispatch precedence is preserved.

## Egress boundary

Every outbound use of a caller-controlled `base_url` passes
`validate_egress_url` at use time. Registration-time validation improves UX but
cannot replace the use-time check because DNS may change.

The guard resolves every address and always denies metadata/link-local, multicast, reserved, and unspecified destinations. Private and loopback model servers are supported for self-hosted installs; multi-user deployments should enable `TOFU_BYO_BLOCK_PRIVATE` and `TOFU_BYO_BLOCK_LOOPBACK`. An allow-list is an explicit operator decision, not an automatic fallback. Exact HTTPS origins in `lib.proxy.SUBSCRIPTION_HOSTS` (port 443, no URL userinfo) may defer failed local DNS to the existing subscription proxy/owner desktop egress boundary. Resolved dangerous addresses still fail; arbitrary BYO hosts require local validation. Local DNS failure alone must not remove a subscription model.

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

Candidate initialization skips only typed missing/invalid credential secrets and invalid endpoints, retaining later candidates already admitted by the same routing policy. It does not expand provider pins or authorization. Storage failures and owner-boundary errors abort initialization and dispose any handles already minted. If no candidate survives, the first typed credential failure remains distinguishable from an invalid endpoint.

The native direct-stream relay follows the same steps without persisting a Turn: its server-minted request record owns billing idempotency while the shared `ExecutionSession` owns route disposal and the admission lease. Provider binding uses an execution `ContextVar`, so concurrent asyncio Tasks on one event-loop thread cannot see one another's request-scoped route group.

Subscription OAuth follows a separate token lifecycle but converges on the same LLM transport. `provider_ready` requires an eligible OAuth route from the owner-scoped candidate compiler; catalog counts, token validity and live health remain separate facts. `lib/oauth/outbound.py` is the sole owner of upstream identity headers; control-plane and request routes must not copy those rules. Codex refresh tokens rejected with a terminal OAuth code are cleared atomically and not retried; the access token is kept only until its recorded expiry, and later requests require a fresh project sign-in. The browser accepts a relayed `oauth_callback` only from the loopback relay origin of a pending flow echoing its state nonce; `exchange_code` requires an exact state match, the manual-paste path (`manual: true`, implied by `callback_url`) being the sole state-fallback exception.
Optional Deployment probes start tasks and requested cells on demand. Launch-probed worker values govern idle reuse, with no active call or matrix-size quota. Idle workers retire; unsaved terminal outcomes remain available as secret-free fallbacks. Results update a Deployment through its owner CAS aggregate, preserving authorization and enabled state.

Daily key health: HTTP 402 stops the provider account key; an ambiguous `insufficient_quota` 429 stops only the observing key/model pair so one vendor behind an aggregate gateway cannot poison siblings. Settings manual ON wins for attended dispatch. Optional background work may enter request-local strict admission where recorded key/model billing stops beat a stale manual ON and last-resort promotion; a wholly rejected pool raises `DispatchNoAdmissibleSlot` (projected retryable `no_slot`/503). Stops are day-scoped; explicit re-enable clears the old stop.


Daily credential attribution: `PermissionError_.is_credential_error` identifies 401 rejection (excluding missing-credential delivery errors) and explicit invalid/revoked/expired credentials in a 403. A model/region/policy 403, harness failure, unknown exception, network outage, 5xx or ordinary 429 cannot disable a key. `credential_failures` alone joins successes in the existing minimum attempt / health-rate gate; `failure` remains request telemetry. Historical failures without that evidence stay informational. Picker and Settings share one predicate; the displayed disable reason retains the credential rejection even after unrelated failures. Billing stops, day rollover, manual overrides and the last-resort guard retain their existing scopes.

Problem: aggregate request failure rate attributed harness/transport faults to credentials. Decision: require typed, positive credential evidence and persist its own counter/reason. Alternatives: adding exception-string exclusions still misclassifies future harness failures; treating every 403 as key-wide poisons models the credential can access. Verification: `tests/llm_dispatch/test_credential_health_attribution.py` and key-stats suites.

### Codex model catalogue lifecycle

`lib/oauth/codex_catalog.py` keeps a private, reconstructible last-good personal
catalogue: normalized rows, account fingerprint, and ETag, never a token. Login
or row changes reset to three minutes; stable 200/304 responses and errors back
off 6→12→24→48→60 minutes. Logout ends it; unauthenticated launch creates none. `codex_version.py` returns a pinned floor/last-good version without waiting for npm. One demand-driven owned worker coalesces refreshes, with a monotonic 60-second failure backoff and reset/shutdown publication fences; no executor or timer is added. Problem: cold/stale request preflight blocked on registry latency and concurrent calls duplicated work. Decision: background cache refresh; alternatives rejected: synchronous npm checks or one worker per request. Spec: `test_codex_version.py` (24 concurrent callers, one fetch).
Measurement (synthetic, Python 3.12.13): with a 250 ms fetch delay, 30 cold resolver calls had p95 250.841 ms before and 0.218 ms after; imports/token resolution/model inference are excluded. Across 20 batches of 24 readers, all returned before fetch release with one fetch per batch; live workers/retained handles were 1/1 while blocked, 0/1 after completion, and 0/0 after joined reset. Cache I/O used warm local temporary storage, not deployment FUSE; this is not an end-to-end Codex speed claim.

## Codex account usage and earned resets

Quota-window rollover and manually redeemable reset credits are distinct.
Their owner-scoped cache, refresh, Push, logout, and notification lifecycle is
specified in [`../CODEX_ACCOUNT_USAGE.md`](../runbooks/CODEX_ACCOUNT_USAGE.md).

## Billing flow
`lib.cost.compute_cost` is the rate engine used by charging and user-visible
cost projection. Model rates come from `lib/pricing/`; do not add a second live
rate table under billing. Static pricing import does not initialize the shared
HTTP stack; only an explicit online refresh owns that lazy dependency.

Model-id lookups must go through `lib.pricing.pricing_for_model` (or `lookup_pricing`), never a raw `MODEL_PRICING.get`: the seam folds one trailing dated-checkpoint stamp (`-MMDD` / `-YYYYMMDD`) so a pinned snapshot (`deepseek-v4-pro-0813`) inherits its rolling alias's row. A dated pin therefore needs no pricing row of its own — add the alias it folds to instead.

For billed requests, `lib/billing/request_flow.py` owns the full sequence:

1. estimate and reserve before dispatch;
2. settle the reservation against terminal usage;
3. append an idempotent ledger entry and update the wallet in one transaction;
4. reclaim abandoned reservations through one durably claimed scheduler task.

Direct paid relays use `request_settlement.py` and the shared `reservation_admission.py` wallet barrier. The [direct request settlement contract](../contracts/HEADLESS_API.md#direct-request-settlement) owns complete physical-attempt invoices, unknown holds, cold recovery and limits.

`wallet_janitor.py` alone queries bounded stale reserves, skips known-running requests, and uses settlement's idempotent release; `janitor.py` is thread-free compatibility. Its durable five-minute task runs only for multi-user billing, so open/private/BYO-only modes poll zero times. `TOFU_BILLING_JANITOR=0` and the legacy TTL alias remain. Distributed scheduling invents no owner; `TODO(enterprise)` requires owner enumeration and durable in-flight leases.

Native billed roots with positive holds acknowledge one zero-value `native_admission` ledger entry before model dispatch. Registered children resolve that exact live root through the shared execution account; they do not invent a second wallet. `lib/billing/native_admission.py` owns this barrier and its root-local ACK cache. The ledger kind and guard vocabulary are generated from `contracts/tofudb_ir_v1.json`; the existing wallet transaction and immutable ledger remain the authority on both SQL and Tofu-DB.

Problem: a Flow child may incur paid fees before its root has a terminal result or a native ROOT checkpoint. Best-effort task-result metadata can be absent or replaced, causing a cold janitor to classify the hold as legacy and refund it. Decision: wallet admission and every standalone release serialize on the same wallet authority. Admission requires the original positive, unreleased reservation and no terminal debit. A protected hold can only be released by exact settlement; generic ledger append cannot bypass the barrier or manufacture an admission. Missing native invoice evidence stays unresolved even if task/conversation records are deleted. The admission entry never claims a price, usage or completed result.

Each admission attempt uses a fresh command delivery identity and the same stable ledger identity. Reusing an earlier command receipt could skip the live hold check after settlement; identical live retries instead revalidate and return the existing ledger entry. Lost ACKs cannot grant a cached provider permit. The joined model callback rechecks cancellation after ACK, and failed admission remains provably unsent. Subsequent requests on the same live root reuse its exact acknowledged owner/wallet/task/amount tuple. Unbilled or zero-hold execution creates no entry.

Alternatives rejected: persist only an in-memory flag or task metadata (crash, checkpoint replacement and deletion lose it); query a marker then refund outside the transaction (a concurrent admission can win between the read and release); or freeze an incomplete root invoice while children are still running (missing quotes become a false zero-debt settlement). One bounded immutable ledger entry per billed root is financial evidence and follows ledger retention, not cache GC. It introduces no poller, thread, extra balance charge or per-round durable write.

A private SQLite measurement over 64 roots and 640 acknowledged retries recorded 64 admission commands and zero cached commands: median warm admission 1.248 ms, cached validation 5.463 µs, and 272 JSON bytes per ledger entry. The root cache tuple plus lock occupied 128 shallow bytes, excluding referenced values. Total physical file growth was 2,776,880 bytes, including ordinary receipts, indexes, logs and allocation; this is not per-entry storage cost. These measurements do not establish native-engine or end-to-end model/Goal recovery latency.

The append-only ledger is authoritative; wallet balance is a recomputable cache. Reserve and settle must share one enablement decision per request; if billing is disabled after a reservation was created, settlement releases that durable hold rather than stranding it behind the disabled path.

## Codex subscription recovery

The existing bounded catalog worker doubles stable-poll intervals up to one hour, but wakes at least five minutes before a stored Codex access token's expiry. A near-expiry idle poll uses the same cross-process OAuth singleflight refresh as requests, even when the model catalog is still fresh; transient catalog/provisioning errors retry within 15 minutes. An explicitly revoked or absent refresh token is never replayed and cannot be renewed without the user's authorization. An explicit logout stops the catalog worker and forgets staged rows only after credential deletion succeeds. Last-admitted catalog rows remain available during transient network/auth outages, whereas freshly fetched rows are not published until model-routing admission succeeds. The repair cannot override deliberately disabled provider access or an explicit capability/credential restriction; see `docs/MODEL_REGISTRATION.md`. Browser/code login commits share the refresh/logout transaction boundary, so an old refresh cannot overwrite a completed new login. Failed refresh fallback re-reads current credentials and current time; expiry crossed during a wait, logout, and replacement login never return the pre-refresh bearer. Problem: slow network operations outlive credential snapshots. Decision: reuse the existing transaction lock and revalidate fallback authority; alternatives rejected: a second credential store, recursively locking generic `save_token`, or trusting stale expiry. Specs: `test_oauth_token_store.py` login/refresh and expiry-boundary regressions.

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
| New provider network call | `lib/byo/byo_egress.py` | use-time guard tests and failure redaction |
| OAuth wire behavior / Codex earned-reset signal | focused `lib/oauth/` module, `codex_usage.py` | `outbound.py`, provider integration tests, OAuth status projection, logout lifecycle, typed notification tests |
| Daily key health/admission | `lib/key_stats/` | dispatcher picker and optional-work policy tests |
| Model rate / wallet/ledger mutation | `lib/pricing/`, `lib/billing/` | `lib/cost.py` tests, billing settlement, idempotency, rollback, janitor tests |
| Rate policy | `lib/rate_limit/rate_limit_api.py` | shared store and response headers |

## Test map

```bash
pytest -q tests/identity/test_identity_contract.py tests/api/test_api_keys.py tests/model_routing/test_model_routing_contract.py tests/byo_egress/test_byo_egress.py tests/startup/test_secret_envelope_startup_boundary.py
pytest -m unit -q tests/oauth/test_oauth_outbound.py tests/oauth/test_oauth_exchange_errors.py tests/oauth/test_codex_usage_reset.py tests/oauth/test_codex_usage_reset_notice.py tests/llm_dispatch/test_key_stats_model_exhaustion.py tests/llm_dispatch/test_key_stats_no_429_auto_disable.py
pytest -q tests/cost/test_cost.py tests/billing/test_billing.py tests/storage_authority_sql/test_rate_limit_store.py
```

Use `rg --files tests | rg '(identity|api_key|model_routing|oauth|billing|cost|rate_limit)'`
to locate renamed focused suites before broadening the gate.

## Native resolved request cost

Problem: native execution lacks the canonical per-component fee arithmetic. Decision: tofu_runtime::pricing consumes normalized counters, their existing cache split and one resolved price bound to the exact total prompt count. It preserves lib/cost.py operation order and ties-to-even rounding for display and 9-digit USD billing components, thinking-only output and CNY conversion. Invalid prices, nonfinite results, invalid splits and stale prompt bindings refuse before a quote. It owns no wallet, prices, currency refresh, tier lookup or source/peak metadata; those remain the explicit existing owner-resolved snapshot. Zero usage returns no quote. Alternatives rejected: another model price table drifts; reselecting tiers per component changes the bill; ordinary Rust round differs from Python at half ties; quoting nonfinite values hides missing price evidence. The current Python oracle captures 1,710 synthetic resolved cases, including full cache, mixed vendor keys, CNY, free prices, wide counters and half ties. CI checks the unchanged Python bodies against the retained fixture before Rust parity. This native kernel is not yet selected by the production fee/wallet path; route-owned price binding and full entry migration remain required before a default cutover.

Raw pricing usage now enters pricing_usage::normalize as a borrowed Value. The generated lib/cost.py aliases preserve first-truthy selection, zero-string nested fallback, null versus present cache fields, mixed native residual input and integer coercion. Python int/Unicode-derived read-only tables preserve supported decimal text and whitespace without depending on Rust Unicode versions. Selected malformed, negative, wide or unsupported counters refuse; they never become a free quote. This explicit native refusal is stricter than legacy display-only zero substitution. Normalization is not a complete financial observation: usage.rs and the existing shared account retain unknown-usage settlement authority. Alternatives: key-presence alone mistakes null for residual semantics; treating zero strings as absent changes alias ordering; copying the JSON or calling Python per request adds context-sized work; inferring finance from display counters refunds unknown dispatched usage. The same oracle now checks raw normalization, cache split and rounded fee components, plus all generated decimal/space entries.

Native pricing_tiers::Card borrows one owner-supplied normalized card, validates finite rates/FX/multiplier and stable inclusive threshold order once, then selects one tier from actual totalInputTokens with a binary search. Equal thresholds retain first-match semantics; above the final threshold uses the final entry. Quote retains a borrowed tier reference, scalar resolved rates and the existing Cost. Explicit already-resolved peak policy scales input/output once; all cache components inherit that same selection. The Python normalizer/selector plus full compute_cost oracle checks 168 flat/tiered, boundary, duplicate, peak and currency cases. Alternatives: estimating from uncached input chooses the wrong tier; sorting/copying every quote adds repeated work; independent component selection changes invoices; adopting another provider table splits price authority. Card compilation and clock/date/route authorization remain existing host owners; no production settlement/wallet/default switch follows from this kernel alone.

Native billing_credits::convert consumes canonical nine-decimal USD components and an explicit resolved nonnegative margin. It rounds input/output/cache-read/ cache-write individually with ties-to-even, sums integer micro-credits and truncates base*margin before checked total addition. The micro-per-USD constant is generated from lib/billing/cost.py, preserving existing wallet units. The unchanged Python conversion/margin bodies provide 8,565 supported comparisons, including half ties and the sub-cent 450-micro case; the same tests cover raw usage through native cost into credits. Invalid money/margins and component, sum, margin or total overflow refuse, never wrap or saturate. Alternatives: converting the four-decimal display total loses fees; summing USD first changes component rounding; resolving margin globally hides ownership; updating wallet units without migration corrupts balances. This pure adapter neither claims complete usage nor writes wallets/ledgers. Production settlement binding and default entry remain pending.

## Native priced settlement transport

Problem: native fee arithmetic could not reach the application model/round settlement ports. Decision: explicit model_control.pricing permits one bounded ModelPricing snapshot only in the existing settle ACK, after the owned tree account settles actual usage. The caller supplies the authorized resolver; its frozen numeric shape derives from the stdio contract. Known usage is required; unknown usage skips preparation and stays incomplete. Rust independently checks known usage and exact prompt binding, computes canonical components/credits, and attaches one boxed cost to the existing StreamResult/round_settled evidence. Known zero usage carries null amounts and zero credits; missing evidence never becomes a zero bill. NativeContextPolicy forwards its owned consumer declaration and exact settlement. SDK requires accepts_native_cost and native CLI requires round control before any model reservation. Resolver/quote/consumer faults preserve paid usage, join existing callbacks and fail without provider replay. Alternatives: a separate price RPC adds a round trip; pricing before budget settlement can strand financial leases; trusting provider cost fields grants invoice authority; a new fee ledger duplicates durability. Six pinned-process cases cover exact fees, unpriced defaults, prompt mismatch, delivery fault, unknown and known-zero usage. All use one HTTP request and reserve/dispatch/settle exactly once. The consumer still owns durable publication; this port does not debit wallets or select a production default resolver/entry.

NativeRoutePricing supplies the production resolver for an explicit priced native route. Problem: global model/provider lookup ignored the owned offering price, so configured custom models could fail settlement or use an unrelated same-name rate. Decision: compile_native_route carries the selected offering’s actual_pricing in its detached owner/tenant/revision-bound metadata; NativeModelAccountPolicy.bind_pricing freezes that snapshot beside existing credential scopes and clears callbacks after the joined run. The default resolver prefers that flat per-million USD/CNY declaration through lib.pricing.declared_pricing. Explicit absolute cache rates convert to the existing multipliers; free rates stay zero, while positive cache rates over zero base input refuse at binding before paid I/O. An absent offering price retains lookup_pricing’s logical-model/provider tier/peak policy; explicitly supplied owner-aware lookup ports keep their existing contract. Actual settled input, exchange-rate authority and resolved margin remain unchanged. Owner/tenant mismatch and missing real price evidence refuse; display-only estimates never become invoices. Alternatives rejected: global/wire-alias prices cannot distinguish owned offerings; rereading mutable task/catalog state after dispatch changes the bill; a new price registry duplicates authority. Conflicting global prices, independent owners, detached snapshots and the installed native settlement exercise exact prices and credits. The full production HTTP test also covers completed-Turn durability across service SIGKILL/restart and pre-provider refusal of an unrepresentable price; it uses a synthetic provider and does not qualify active-task recovery or a two-hour real-model run.

NativeTaskCostPolicy wraps the existing round port and binds issued logical model/ provider metadata. It validates known native amounts/credits, freezes the current settlement digest, and appends one record to existing task.apiRounds before joined consumer delivery. Callback failure retains that paid projection; identical retries repeat delivery only, changed/older candidates refuse, and pending cost debt blocks later admission. No second receipt history or financial account is created. Existing usage accumulation and frozen-cost aggregation populate task fields; the private native projection marker makes live/Turn terminal folds use those exact costs instead of looking up later prices. The ordinary terminal writer persists the authoritative Turn, retaining apiRounds/nativePrice/ nativeCredits through its existing sanitizer. SQLite scratch authority tests exercise real pinned native model/tool/model execution in sync/streaming modes and read back exact credits/aggregate from Turn. Alternatives: repricing changes history after table updates; recording after callbacks loses paid evidence on errors; another cost store splits durability; retry append double-counts bills.
### Native credits into existing wallet settlement

Problem: Python terminal repricing changes settled Rust fees, and root-only quotes omit child consumption. Decision: native_task_cost folds original per-round integer components/margin into the unchanged atomic wallet settlement. Missing account, unknown/uncovered usage, invalid/duplicate rows and signed overflow refuse; known unsent zero consumption can release its hold. Pricing is marked before dispatch. transfer_native_child_fees admits same-model descendants; tree journals require checkpoint_tree_usage; ordinary Continue validates the owned source invoice. It validates captured attachment ancestry and settled actor usage before terminal ACK. Original quotes retain taskId/billableTaskId/sourceRound; immutable grants hash each row. Foreign, altered or duplicate rows refuse; detached retries verify without appending. Recovered actor completion preserves each historical `billableTaskId` instead of retagging the entire result to the new root. Problem: a child can contain several executor generations; selecting retry rows only by the current child ID loses its historical rows and replacing their payer assigns old debt to new work. Decision: the existing grant binds both the original child rows and their exact transferred producer/source-round/payer identities. An inherited prefix requires an acknowledged shared-account rebind with exactly matching original token consumption. Missing payer identity, changed prefixes and failed frontier ACKs refuse before parent projection. All rows cover cumulative consumption; only rows whose witnessed payer is the current root enter its wallet capsule. Alternatives rejected: omitting historical rows breaks full token coverage; retagging them charges prior work again; trusting a prefix without account rebinding creates an unproved billing exclusion. These witnesses remain local to the existing root lifetime and add no durable record or worker. They do not authorize actor execution: atomic native actor takeover and durable restoration of mixed-origin actor fees must still be connected before automatic Goal recovery. Verify `test_recovered_actor_fee_transfer.py` for multiple generations and altered proofs, and `test_recovered_actor_fee_wallet.py` for original/successor settlement and lost ACKs on SQLite, native daemon and embedded authorities. For 2/32/256 original-plus-fresh rounds, a seven-sample local measurement recorded median host transfer times of 252/2,177/16,359 µs and newly allocated immutable grant retention of 4,005/11,473/69,409 bytes. These cover fee validation/copying and both row-hash indexes; they exclude model execution, native handoff, storage and parent result serialization. Grant lifetime remains the root lifetime. Account locking precedes shared content locking and performs no I/O. Actor completion never certifies tree quiescence. Descendants transfer to the owned root with executor/source ordinal intact; intermediates acquire no wallet/Turn/recovery authority. No pool, ledger or wallet schema is added. Edges detach with actors; dictionary capacity/grants last with the root (512 live edges: 13,056 bytes on Python 3.12). Tree restore validates root/tree coverage precedes retained witnesses; capsules include transferred fees. Bare Continue validates source owner/accepted lineage, exact quotes and all capsule components before increasing spent budget once. Same-executor recovery still funds its unsettled rows; only a preceding foreign-task prefix stays paid elsewhere. Wallet retries/recovery consume the same capsule without replay. Alternatives: repricing loses paid rates; separate child wallets split ownership; ignored child usage undercharges. Tests: test_native_child_fee_transfer, test_native_task_cost, test_relay_billing_gate, test_native_priced_child_wallet_transfer and native_cost cases in test_native_task_terminal, test_native_priced_tree_checkpoint_handoff and test_native_invoice_prefix_restore. Default remains unqualified.
### Native terminal fee recovery

**Uncertain usage and terminal outcomes.** Problem: refusing an invoice for unknown native usage also prevented an already failed Turn from being persisted. Decision: `native_settlement.terminal_metadata` permits error, interruption and abort outcomes to retain `nativePricingRequired=true` and `usageComplete=false` without a settlement capsule. The ordinary task-result/Turn writer still owns terminal publication; the existing wallet recovery owner continues to refuse settlement or refund until exact native evidence is available. Successful outcomes, missing financial accounts, invalid known quotes and mutation of an already frozen invoice retain their original validation. Alternatives rejected: a zero invoice would refund uncertain debt; withholding the failure record keeps the conversation running after execution has stopped. Verify `test_native_terminal_projection` and `test_goal_native_source_sigkill`, including committed whole-set handoff with a lost response, no subsequent provider call or effect admission, one durable consumed receipt, and repeated wallet recovery. The genuine crash fixture's target remains an explicit port composition; automatic pending dispatch and recovery after another process loss remain fenced.

Problem: the reserve sweeper refunded a hold after process loss even when Rust fees were confirmed and terminal delivery had not acknowledged the wallet. Decision: reserve admission binds the opaque wallet subject explicitly. Native terminal projection freezes one closed-schema nativeBillingSettlement capsule before its outcome digest and the existing owned task-result CAS; metadata also marks native-required work before terminal. The capsule contains owner/task/wallet identity, original reservation, model and fixed integer component totals, not usage/history or a new ledger. NATIVE_BILLING_SETTLEMENT_SCHEMA in lib/billing/native_settlement.py is its machine-readable shape authority. The existing sweeper maps wallet subject through tenant.user.get, reads the owned terminal result, validates the capsule and calls the unchanged idempotent wallet settlement. Missing native terminal evidence, changed identity/reservation, invalid amounts and uncertain reads retain the hold as an error. Legacy orphan handling remains at its existing owner. Live delivery and recovery use the same frozen model/amount/note with schema-ordered components despite storage JSON key reordering; later caller aliases or price changes cannot change the command. Terminal retries additionally fence usage, round fees, reservation and capsule mutations. Alternatives: TTL refund loses paid work; replaying execution repeats effects; a second financial database splits authority; parsing an opaque wallet ID as a numeric repository owner bypasses the identity mapping. Tests clear live task/account state and run a fresh interpreter over the actual SQLite authority after terminal metadata publication; normal and lost-ACK cases retain exactly one debit/release. Real native sync/streaming tests verify capsule publication through canonical persistence. This covers committed terminal fee handoff, not a kill before terminal CAS, in-flight model takeover, missing initial checkpoint evidence, no-reserve recovery scanning, or production-default cutover.
