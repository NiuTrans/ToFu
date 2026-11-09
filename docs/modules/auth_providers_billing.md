# Authentication, providers, and billing

## Tofu-DB wallet and ledger authority

The seven wallet/ledger operations compile through Tofu-DB Transaction IR. Native continuation settles only fresh apiRounds.taskId credits while inherited quotes still cover cumulative tokens; verify native cost/settlement and v4 billed resume tests, with authority/evidence in [DEVELOPER_RUNTIME.md](../DEVELOPER_RUNTIME.md). Billing subjects remain explicit opaque IDs inside a narrowly enumerated tenant-global scope. Every applied amount atomically publishes the immutable ledger entry, exact global ID and nonempty reference claims, wallet balance, constant-time user sum/count, descending list index, active-reserve projection, receipt, and encrypted outbox record. Checked signed arithmetic rejects overflow, insufficient debit writes nothing, and filtered lists fail closed after 10,000 candidates or 8 MiB. Additional billing operations follow the admitted storage operation catalog. Payment record/find/list/settle run on this authority as well as the SQL Sidecar; the application parity tests below exercise both native transports.

This security/money boundary owns principals, credentials, OAuth, owner-scoped
ProviderAccess resources, safe egress, charging, and rate limits. Identity lives in
[`../IDENTITY.md`](../IDENTITY.md); HTTP rules in [`../API_CONTRACT.md`](../contracts/API_CONTRACT.md).

## Ownership

| Concern | Owner |
|---|---|
| Request authentication boundary | `routes/api_v1/auth.py`, `lib/auth_mode.py` |
| Principal and ownership types | `lib/identity.py` |
| API credential verification/CRUD | `lib/api_keys/` |
| HTTP/WebSocket credential selection | `lib/api_keys/transport.py` |
| Model/provider authority | `lib/model_routing/repository.py`, `domain.py` |
| Request route resolution/lifecycle | `lib/model_routing/routing.py`, `dispatch_adapter.py` |
| Storage-free run authority for child invocations | `lib/model_routing/request_authority.py` |
| Caller-controlled egress policy | `lib/byo/byo_egress.py` |
| Subscription OAuth | `lib/oauth/` |
| Kimi Code device sign-in and refresh | `lib/oauth/kimi.py` |
| Device-code login loop (Codex, Kimi) | `lib/oauth/manager/_device.py` |
| Coding-plan key templates (`subscription` category) | `lib/model_info/data/*.json`, `lib/provider/provider_template_recipes.py` |
| Codex account usage / earned resets | `lib/oauth/codex_usage.py` |
| Provider-specific outbound identity | `lib/oauth/outbound.py` |
| Pricing and cost math | `lib/pricing/`, `lib/cost.py` |
| Dynamic charge conversion / shared charge result | `lib/billing/cost.py`, `cost_types.py` |
| Frozen native invoice and child fee witnesses | `lib/billing/native_cost.py` |
| Wallet and ledger | `lib/billing/wallet.py`, `ledger.py` |
| Reserve/settle choreography | `lib/billing/request_flow.py` |
| Task-backed HTTP billing preparation | `routes/api_v1/_task_billing.py` |
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

Problem: every page the local user visits can reach a loopback server, so the
synthetic grant let any website write as the owner (CSRF) and read the push
stream or call control RPCs over `/api/push` (browsers apply no CORS to
sockets). Decision: a browser request that Fetch Metadata marks as not
`same-origin`/`none` (or, without Fetch Metadata, whose `Origin` differs from
`Host`) receives no synthetic grant for unsafe HTTP methods or the push socket;
it must present a credential like private mode. Safe cross-site reads, whose
responses CORS already withholds, and headless clients are unchanged.
Alternatives rejected: a `Host` allowlist (would also stop DNS rebinding, but
breaks forwarded-port and proxy hosts without new operator configuration) and
blocking every cross-site request (breaks links and navigations into the UI). Tests: `tests/api_keys/test_open_mode_cross_site.py`,
`tests/api_keys/test_push_ws_auth.py`.

No current-user global enters domain/repository code. Tests, desktop bridges,
and headless adapters cross the production auth or capability boundary.

## Secret storage

Problem: Settings, OAuth and local endpoint removal can drift when each owns a
Provider dependency cascade. Decision: `lib/model_routing/managed_provider.py`
owns the shared candidate transformation. It removes the Provider, accesses,
connections, credentials, offerings and referencing deployments; independent
Creator/Model catalog entries remain. Callers retain their owner boundary,
registration admission, revision CAS, retries and post-commit secret cleanup.
The in-place entry point avoids another full copy of an already detached
candidate; the copying entry point retains its detached result and sorted unique
cleanup references. Alternatives rejected: duplicated cascade implementations
or one generic transaction wrapper that changes HTTP conflicts and OAuth retries.

The same domain owner selects unreferenced secret cleanup candidates from the
committed aggregate. Provider deletion, bundle replacement and credential rotation
retain any reference still used by another credential. HTTP cleanup must explicitly
receive the committed document; only rollback of newly created, uncommitted secrets
uses no document. This prevents one provider mutation from invalidating another
provider's shared credential while preserving last-reference reclamation.

Problem: a later aggregate commit can reference a cleanup candidate after the
application selects it, and a secret refresh can race an expiry scan. Decision:
the storage transaction rechecks current owner/tenant credential references,
including disabled credentials, before deletion. Caller prune references add
retention pins; they cannot remove the current aggregate's protection. SQL
cleanup takes the existing aggregate fence before reading references, then the
candidate's secret fence and rechecks its timestamp in the conditional deletion.
Secret puts retain their independent per-key fence. Native cleanup uses its
existing authority transaction; the in-memory repository uses its existing lock.
Malformed reference metadata refuses cleanup with an integrity error. Removing
the last reference still permits reclamation. Delete receipt replay preserves its
original result; prune remains a repeatable scan without receipts. Alternatives
rejected: snapshot-only filtering leaves the race;
locking every secret put on the aggregate would serialize independent writes;
a separate reference index adds a second consistency obligation. This guard
does not change aggregate admission or authorize future references to missing
secrets. Verification: `tests/model_routing/test_secret_lifecycle.py` and
`tests/storage_authority_sql/test_model_routing_secret_fences.py`.

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

The guard resolves every address and always denies metadata/link-local, multicast, reserved, and unspecified destinations, including the metadata endpoints outside link-local space (Alibaba Cloud `100.100.100.200`, AWS IPv6 `fd00:ec2::254`). Private and loopback model servers are supported for self-hosted installs; multi-user deployments should enable `TOFU_BYO_BLOCK_PRIVATE` (every non-global address, including RFC 6598 shared space) and `TOFU_BYO_BLOCK_LOOPBACK`. The public-only `lib/safe_fetch.py` policy accepts only globally routable addresses. An allow-list is an explicit operator decision, not an automatic fallback. Exact HTTPS origins in `lib.proxy.SUBSCRIPTION_HOSTS` (port 443, no URL userinfo) may defer failed local DNS to the existing subscription proxy/owner desktop egress boundary. Resolved dangerous addresses still fail; arbitrary BYO hosts require local validation. Local DNS failure alone must not remove a subscription model.

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
   egress once per distinct Connection base URL, retains every eligible ranked candidate
   ([MODEL_REGISTRATION.md](../MODEL_REGISTRATION.md)) and publishes the group's request-scoped
   slots in one dispatcher-lock critical section ([llm_io.md](llm_io.md), "Route group publication"); the shared LLM
   transport records factual health against Deployment/Connection/Credential. Ranking scans eligible candidates lazily with stable top-K selection; a full primary prefix skips unused model-fallback scans, and fallback excludes the resolved primary identity.
4. The route group is disposed at terminal settlement and a redacted bounded
   RouteSnapshot is persisted with the turn.

Candidate initialization skips only typed missing/invalid credential secrets and invalid endpoints, retaining later candidates already admitted by the same routing policy. It does not expand provider pins or authorization. Storage failures and owner-boundary errors abort initialization and dispose any handles already minted. If no candidate survives, the first typed credential failure remains distinguishable from an invalid endpoint.

The native direct-stream relay follows the same steps without persisting a Turn: its server-minted request record owns billing idempotency while the shared `ExecutionSession` owns route disposal and the admission lease. Provider binding uses an execution `ContextVar`, so concurrent asyncio Tasks on one event-loop thread cannot see one another's request-scoped route group.

Subscription OAuth follows a separate token lifecycle but converges on the same LLM transport. `provider_ready` requires an eligible OAuth route from the owner-scoped candidate compiler; catalog counts, token validity and live health remain separate facts. `lib/oauth/outbound.py` is the sole owner of upstream identity headers; control-plane and request routes must not copy those rules. Codex refresh tokens rejected with a terminal OAuth code are cleared atomically and not retried; the access token is kept only until its recorded expiry, and later requests require a fresh project sign-in. The browser accepts a relayed `oauth_callback` only from the loopback relay origin of a pending flow echoing its state nonce; `exchange_code` requires an exact state match, the manual-paste path (`manual: true`, implied by `callback_url`) being the sole state-fallback exception.
Problem: the two OAuth status cards separately read, normalize and index the
same owner routing aggregate. Decision: `managed_oauth_provider_statuses` shares
one request-owned document and compiler per provider group, for both status
responses and reconciliation readback. Each new call still reads current owner
authority, and each token status retains its live credential checks. Alternatives
rejected: a global or TTL readiness cache can hide revocation; skipping route compilation would turn
catalog presence into authorization. The 216-model isolated ABBA medians were
23.8/23.5 ms before and 11.7/12.1 ms after, with repository reads and compilers
both reduced from two to one per status response. These exclude storage/network
I/O. `test_oauth_status_snapshot.py` verifies parity, fresh revocation, owner
isolation and recovery after a failed snapshot.

Problem: one status response resolved the borrowed Codex login four times
(catalog account fingerprint, card, two reset-offer reads) and ten times when the
throttled reconciliation ran, and that pass read the routing authority once per
provider step plus its readback before the route read it again. Each resolution
revalidates native launcher metadata and reads the credential files with about
60 GIL-releasing filesystem calls, so on a busy server a status read cost
seconds (live SLOW log: up to 19.9 s). Decision: `token_store.token_snapshot()`
scopes one response, and each reconciliation pass, to one resolution per
provider; `save_token`, `delete_token` and a refreshing shared read drop that
provider, and nothing outlives the block. Reconciliation reads one authority
revision for all provider steps, re-reads only after its own commit (the CAS
still rejects a moved aggregate) and returns its readback, which the route
reuses. Read-path permission hardening stats instead of re-chmodding an
already private store, a verified Codex home is resolved once per read, and a
non-symlink launcher stamp reuses its lstat. Alternatives rejected: a
cross-request or TTL credential cache would hide logout, renewal and account
switches; dropping reconciliation from the status read would lose shared-login
discovery, expired-login renewal and desktop snapshot refresh, which no other
owner performs for this surface. In-process A/B with the live 606-model
aggregate and a borrowed local login on the FUSE mount (8 requests per leg):
steady responses fell from 341 to 62 filesystem calls and 4 to 1 resolutions,
160 to 138 ms idle and 5.4 to 1.5 s with three CPU-bound threads; reconciling
responses fell from 942 to 209 calls, 10 to 2 resolutions and 4 to 1 authority
reads, 653 to 336 ms idle and 15.8 to 6.0 s under contention, with identical
bodies. The remaining reconciling cost is dominated by the hot provider-template
reload in `_upsert_oauth_provider` while the Codex catalog lists models the
directory lacks; it has no exact cheaper witness yet.
`test_oauth_status_credential_reads.py` counts resolutions and authority reads
and proves a snapshot never outlives its response.

Optional Deployment probes start tasks and requested cells on demand. The fixed `TOOL_MAX_PARALLEL_WORKERS` hint governs idle reuse, with no active call or matrix-size quota. Idle workers retire; unsaved terminal outcomes remain available as secret-free fallbacks. Results update a Deployment through its owner CAS aggregate, preserving authorization and enabled state.

Daily key health: HTTP 402 stops the provider account key; an ambiguous `insufficient_quota` 429 stops only the observing key/model pair so one vendor behind an aggregate gateway cannot poison siblings. Settings manual ON wins for attended dispatch. A recorded key/model billing stop is never last-resort promoted, under either admission policy; credential-health (success-rate) disables keep that promotion. Optional background work may enter request-local strict admission where recorded key/model billing stops beat a stale manual ON; a wholly rejected pool raises `DispatchNoAdmissibleSlot` (projected retryable `no_slot`/503). A planner choosing credentials for such work reads the same refusal before minting a slot through `is_key_refused_for_optional_work` (owner-wide auxiliary summaries: `docs/CONTEXT_BUDGET_POLICY.md`, recorded billing stops); ordinary dispatch reads it with `honor_override=True`, so a manual ON restores capacity exactly as `is_key_enabled` does. `LLMDispatcher.has_capable_slots` treats a recorded billing stop as durable disqualifying evidence under ordinary admission too, never as healable cooldown, so the no-slot branch cannot poll a pool only an owner or a provider reset can unblock. Stops are day-scoped; explicit re-enable clears the old stop.

Problem (2026-11-06 edit benchmark): most DeepSeek-V4-Pro credentials answered HTTP 402 "Credit exhausted" and the rest were rate-limited, yet every later request spent one more live 402 on an already-stopped credential. Two mechanisms compounded. The provider last-resort guard promoted one billing-stopped key back to enabled whenever every sibling in the owner + Provider namespace was raw-disabled, and each request mints a fresh request-scoped route group, so the slot-local one-hour quota cooldown died with the request while the durable stop was the only surviving evidence. Separately, `has_capable_slots` consulted key health only inside strict optional-work admission, so once the picker refused every stopped slot the no-slot branch still saw healable capacity and polled it indefinitely without sending a request.

Decision: a billing stop is the provider's own statement that this credential cannot pay for another request, so it is durable disqualifying evidence for selection AND for capacity, not a transient condition a retry or a last-resort reprieve can cure. It clears only on provider-defined evidence — the subscription reset time, a new day, or an owner re-enable (which also clears the recorded stops). No cooldown constant, retry cap or other knob was added.

Alternatives rejected: a longer slot-local quota cooldown (does not survive the next request's fresh route group, and is a tuning knob); keeping last-resort promotion for a sole billing-dead key (reproduces the measured one-402-per-request burn); enabling strict billing-stop admission for all attended dispatch (would also drop the retained credential-health last-resort reprieve and let a stale manual ON outrank a fresh stop); leaving capacity key-health-blind outside strict admission (the indefinite poll).

Verification: `tests/llm_dispatch/test_billing_stop_no_last_resort.py` (failing first on the previous lib: re-pick of a stopped credential, healable capacity, endless no-slot polling, one more provider call), `test_key_stats_model_exhaustion.py` (promotion expectations rewritten with the incident, manual-ON and strict-admission precedence), `test_key_stats_no_429_auto_disable.py`, `test_credential_health_attribution.py`, `tests/key_stats/` and `test_gateway_errors_accounting.py`.


Daily credential attribution: `PermissionError_.is_credential_error` identifies 401 rejection (excluding missing-credential delivery errors) and explicit invalid/revoked/expired credentials in a 403. A model/region/policy 403, harness failure, unknown exception, network outage, 5xx or ordinary 429 cannot disable a key. `credential_failures` alone joins successes in the existing minimum attempt / health-rate gate; `failure` remains request telemetry. Historical failures without that evidence stay informational. Picker and Settings share one predicate; the displayed disable reason retains the credential rejection even after unrelated failures. Billing stops, day rollover, manual overrides and the last-resort guard retain their existing scopes.

Problem: aggregate request failure rate attributed harness/transport faults to credentials. Decision: require typed, positive credential evidence and persist its own counter/reason. Alternatives: adding exception-string exclusions still misclassifies future harness failures; treating every 403 as key-wide poisons models the credential can access. Verification: `tests/llm_dispatch/test_credential_health_attribution.py` and key-stats suites.

### Key-health persistence

`data/config/key_stats.json` holds two classes of data. Health telemetry (success, failure and credential-failure counts, 429 and gateway counts, the 429 streak and the last errors) is reconstructible: the next requests re-observe it. Manual overrides and billing stops are admission decisions: an override is durable user state, and a lost stop costs a live 402 per model to rediscover.

Problem: every successful dispatch called `Slot.record_success` → `record_outcome`, which re-read, rewrote and fsynced the store and re-read the server config for the namespace fold, all while holding the process-wide key-stats `_lock` and the store's sidecar flock. The picker calls `is_key_enabled` under the dispatcher lock, so one success's disk transaction serialized every concurrent pick and success in the process, and another Tofu process holding the flock stalled them too. The dispatch-pick investigation measured about 40–50 ms per success on the deployment FUSE volume and up to about 0.5 s on the first dispatch.

Decision: recording applies telemetry to the in-memory snapshot at once and coalesces it per pair key and day; the request thread holds `_lock` only for that update and never touches disk. A demand-driven `tofu-key-stats-flush` worker, which retires when idle, merges it into the latest document once per `KEY_STATS_FLUSH_INTERVAL_SECONDS` (2 s, resource class `fixed`: a hardware-independent bound on how long telemetry stays unwritten, never sizing or delaying work). `shutdown_key_stats` waits out the worker's commit and flushes the rest after production shutdown stops dispatch producers, and again at interpreter exit for other processes ("Key-health exit flush" below). `set_key_override`, `clear_key_override` and `mark_key_exhausted` still commit before returning, after earlier telemetry in the same transaction, so a re-enable resets the 429 streak at its own position. Each write remains one transaction against the latest document under the sidecar flock, so processes sharing the config directory merge rather than overwrite; the flock covers only that read-modify-write, and the namespace-fold map is resolved before it. No disk I/O happens under `_lock`. Reads load the opened inode without the writer lock, using `fstat` for its exact fingerprint, and never write. A rollover or namespace fold is persisted by the next flush. A past day's unwritten telemetry is dropped at rollover, as if it had been flushed before midnight. A failed commit keeps its mutations applied in memory and pending and leaves the file untouched. Retries coalesce, so a long outage keeps the queue bounded by keys and decisions. A forked child never replays its parent's queue.

Consequences: a crash (SIGKILL/OOM) loses at most one interval of telemetry, never a committed decision. Other processes see counters up to one interval late, so their credential auto-disable can lag by that much. When processes interleave 429s and successes on one key, the cross-process 429 streak is ordered per batch. A billing stop still costs its caller one commit, without holding `_lock`; on an async dispatch loop that commit runs on the loop thread, as every outcome did before.

Alternatives rejected:
- Per-success writes without fsync still rewrite the store and take the flock on the request path, serialized under `_lock`.
- A background thread per write creates unbounded threads and reorders writes.
- An append-only event journal adds a file format and a compaction/recovery owner for reconstructible data.
- Deferring overrides and stops too would let a crash or a second process lose or miss decisions.
- A launch knob for the interval: it is a fixed bound, not tuning.
- Deriving the interval from hardware is forbidden (`docs/RESOURCE_CONTROL.md`).

Measurement: a synthetic microbenchmark on local xfs on the shared host, with interleaved before/after runs with and without 40 ms of emulated fsync latency, 200 sequential successes, 8 threads × 25 success+pick, and a 174,769-byte synthetic server config. Per-success latency fell from a 72.8–108 ms mean to 5.4–9.3 µs, and `_lock` hold per success from 72.8–108 ms to 2.0–3.6 µs. A pick right after a success fell from 2.0–2.2 ms to 6.4–11.5 µs, because our own commit no longer forces a reload. The concurrent mean fell from 470–1,212 ms to 0.25–0.31 ms, and wall time for 200 calls from 12.1–30.6 s to 7–9 ms. The first dispatch of a cold process fell from 31–115 ms to 6.4–9.1 ms. One background commit took 13–208 ms, dominated by fsync while the disk was shared with concurrent test runs. An earlier, quieter run measured 7.4 ms per success before the change (51.8 ms with 40 ms of emulated FUSE fsync latency). Every run persisted exactly 605 of 605 successes. FUSE writes were not measured; the per-read freshness `stat` costs 2.4 µs p50 there. Verification: `tests/key_stats/test_key_stats_persistence.py`, whose failing-first spec is `test_success_recording_never_waits_for_store_io`, and the production shutdown order in `tests/server/test_server_shutdown_lifecycle.py`.

### Key-health exit flush

Problem: interpreter exit ran `shutdown_key_stats` with a 5 s deadline that no contract recorded (production shutdown has its own process backstop, `TOFU_SHUTDOWN_HARD_SECS` in `lib/server/server_shutdown.py`). Processes that record and exit together queue their exit commits on the store's flock, and every commit also read the server config, and on a first commit imported the face module, while holding it. Once that queue outlasted the deadline the exiting thread stopped joining the flush worker, could not take `_persist_lock` from the worker's commit in progress, returned False, and the process died with the worker's batch and everything recorded after it. `test_cross_process_counters_and_overrides_do_not_lose_updates` flaked about once in 20 runs on a loaded host.

Decision: delete the deadline. Exit never abandons a commit it can still complete ("Joined producers" in [DEFENSIVE_PATTERNS.md](../DEFENSIVE_PATTERNS.md)): `shutdown_key_stats` joins the worker however long its commit waits for the flock, then commits what was recorded after that commit took its batch. The flock covers only reading, applying, folding and the atomic fsync'd write: `_namespace_fold_map` (the config read and import) runs before it. A failed commit still returns False after one attempt and keeps its mutations pending; production shutdown stays bounded by its hard deadline.

Alternatives rejected:
- Keeping a fixed deadline and handing later mutations to the in-flight commit: the in-flight batch itself is still dropped whenever the queue outlasts the deadline.
- A longer deadline: another arbitrary knob, and it still loses once the processes' queued commits outlast it.
- Per-process journal files merged by a later commit: a new file format and recovery owner for reconstructible telemetry.
- Skipping fsync for telemetry commits: the same document carries manual overrides, which are durable user state.

Measurement (shared host, `/tmp` on local md RAID, 20 rounds per tree alternating base/fix, every round a fresh process set released by one barrier and exiting without a flush): with the flaky spec's shape (8 recording plus 5 override processes) neither tree lost an update in these quieter rounds; the slowest round took 5.39 s from release to the last exit before and 4.05 s after (means 1.57 s and 1.36 s). With a slow volume emulated by holding the flock 0.75 s longer per commit (8 processes, 3 successes and one 429 each), the old exit path lost counters in 20 of 20 rounds, 144 of 480 successes, two or three processes' counters entirely per round; the new one lost none in 20 of 20 rounds, at 6.3–10.7 s per round. Verification: `test_processes_that_record_and_exit_at_once_lose_no_counters` (the slow-volume case is `slow`-marked), `test_shutdown_waits_out_an_in_flight_commit_and_persists_what_follows` and `test_store_commit_runs_no_import_or_config_read_under_the_flock` in `tests/key_stats/test_key_stats_persistence.py`.

### Key-health admission reads

Problem: a dispatcher pick reads one `is_key_enabled` verdict per candidate slot, and every read stat'ed the store for cross-process freshness while holding `_lock`. Each `stat` releases the GIL; under GIL pressure the thread then waits a whole scheduling round to get it back while still holding `_lock`, so every other reader, including the dispatcher-lock holder, queued behind it. With about 1,600 live request-scoped slots one pick paid about 1,600 such handoffs inside the global dispatcher lock. Separately, the flush worker was started while holding `_lock`: `Thread.start` waits until the new thread runs, so a concurrent admission read (and the dispatcher-lock holder making it) waited for a thread start; a production stall dump showed exactly that chain.

Decision: an admission read never stats the store while holding `_lock`. It fingerprints the store first (`_observe_store`) and compares under the lock, reloading only when the store changed. `key_admission_batch()` lets the reads of one selection share one freshness check: entering the block checks once, before the caller takes its own locks, and the reads inside use the in-memory snapshot, which already holds this process's decisions and telemetry, so a decision recorded inside the block is still seen and a commit by another process is seen by the next batch. The batch is a context variable, local to its execution context. The flush worker is claimed under `_lock` and started after the lock is released; shutdown tolerates a claimed worker that has not started yet, because its own final flush persists everything that worker would have written. The dispatcher's use of the batch is described in [llm_io.md](llm_io.md) ("Slot pool scopes").

Alternatives rejected: a time-based freshness cache adds a staleness window to tune; an inotify watcher adds a thread and a platform dependency; starting the worker at import keeps an idle thread in every process. Verification: `tests/key_stats/test_key_admission_batch.py`.

### Key-health siblings

Problem: the last-resort guard needs a key's siblings, the live credentials of its owner + Provider namespace, on every admission read. `_list_siblings` rebuilt that map for every namespace by walking every live dispatcher slot, once per 30 s (`_SIBLINGS_TTL_SEC`) and again for any namespace missing from the cache, inside the pick under the global dispatcher lock: O(all live slots), about 1,600. Between rebuilds it served lists up to 30 s stale, so a withdrawn credential could still win last-resort promotion while the live keys stayed disabled, and a newly minted one was not counted. Settings reads of namespaces without live slots rebuilt it on every call.

Decision: `SlotPool` keeps an exact index of live credentials per key-health namespace (`Slot.key_stats_provider_id()` → `Slot.key_stats_key_name()` → number of live slots), updated in the same dispatcher-lock critical section as route-group publication and withdrawal, and rebuilt with the pool on configuration reload or generic list edits. A credential stays listed while any live slot uses it. `_list_siblings` reads one published sorted tuple (`SlotPool.key_stats_key_names`), so no admission read walks the pool and the result is exact the moment a group is minted or withdrawn. Each slot is uncounted under the identity it was counted with, so editing a pooled slot cannot unbalance the counts. The TTL and its cache are deleted.

Alternatives rejected: a shorter TTL (a staleness knob, still a periodic full walk under the dispatcher lock); invalidating the cache on publication and withdrawal (a full rebuild per route-group mint); a credential set without counts (withdrawing one of several slots sharing a credential would drop a live credential).

Measurement (scratch harness, shared host, fresh processes alternating base/fix, 1,600 live slots in 30 route groups of 53 plus 10 configured, real key admission on an isolated store, 8 CPU-bound threads, one thread doing pinned picks): steady-state picks stayed within noise, p50 0.37–0.39 ms before and 0.39–0.40 ms after (3,000 picks per run, three runs each; p99 about 5.6 ms, one GIL switch interval, in five of the six runs). The pick that paid the 30 s rebuild before took p50 1.8–6.7 ms, mean 17–24 ms and up to 0.4–0.9 s; it no longer exists. `benchmarks/dispatch_contention.py` (four pinned and one unpinned picker, a group minter, 8 CPU-bound threads, 65 s, four alternating runs per tree) cannot resolve the change: its pinned p50 is bimodal on both trees, 0.25–0.29 ms or 0.9–1.2 s (two of four runs slow before, one of four after), and its unpinned picks and group mints take 0.2–1.5 s at p50 on both. Verification: `test_pinned_pick_never_walks_the_live_pool`, `test_key_health_siblings_follow_route_group_mint_and_withdrawal` and `test_key_health_siblings_follow_reload_and_list_edits` in `tests/llm_dispatch/test_slot_pool_scopes.py`.

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

A resolved context tier carries only the rates its source declares; every
other rate is inherited from the model's declared row, then the documented
defaults (cache read 0.10, cache write 1.25 of input). Problem: the legacy CNY
tier table declares only input/output per context, and its translator wrote
`cacheReadMul` 0.0 over the model row, so declared cache hits (hy4-preview's
¥0.3, every Qwen row's 10%) were billed as free. Decision: tiers inherit, and
the CNY table may declare its own cache rates per row (the two rows without a
model row keep the 1.0 write rate the translator always applied).
Alternative rejected: copying cache rates into every tier row, a second
declaration of the same fact that drifts. Specification:
`tests/pricing/test_declared_cache_rates.py`.

FRIDAY (the YourProvider AIGC gateway, `your-llm-gateway.example.com`) bills Claude cache
tokens by its own multipliers of the model's base input. Problem: every
consumer priced FRIDAY Claude rounds with the creator's row, so Opus 5.5 and
Sonnet 5.5 cache reads (Anthropic: 0.05x) were displayed and debited at half
of FRIDAY's documented read charge. Evidence: 学城 2762588777 《Friday 大模型平台
Claude 模型接入推荐实践》 §1.3 (modified 2026-05-15, read 2026-11-08), verbatim
`| 5m（默认） | × 1.25 base | $3.75 |`, `| 1h | × 2.0 base | $6.00 |`,
`| 命中读取（任意 TTL） | × 0.1 base | $0.30 |`. Decision: `lib/pricing`
declares those multipliers once (`FRIDAY_CLAUDE_CACHE_MULTIPLIERS`) and
installs, at import, one provider row per Claude-family `MODEL_PRICING` row
for each FRIDAY provider (`FRIDAY_PROVIDER_IDS`: `yourprovider`, which a fresh
install of the YourProvider template creates, and `example-corp`, the deployment's
provider imported by the v1→v2 cutover). The row keeps the creator's base
input/output, currency, tiers and their evidence and replaces only the cache
multipliers, so `lookup_pricing(model, provider_id)` returns it to every
consumer: the per-round and Turn cost stamps the conversation shows, the My
Day calendar, the message-cost endpoints, billing receipts, native settlement
without an Offering price, and compaction summary pricing and cache economics.
Any other provider, such as a direct Anthropic key, keeps the creator's row.
Gaps: FRIDAY publishes no base price for any Claude model, and the page
predates Opus 5.5 (GA 2026-09-22) and Fable 5.1, whose official reads are
0.05x and 0.025x; nothing else ties 0.1x to them, and the gateway's responses
carry token counts only. A FRIDAY provider installed under another id keeps
the creator's row until it is added there. Stored costs are not rewritten:
round and Turn stamps settled before the change keep their recorded rates,
while surfaces that price from usage at read time (today's My Day total,
unstamped rounds, past days not yet in the daily cost cache) use the new row.
Alternatives rejected: changing the creator rows (would misprice direct
Anthropic routes); Offering `actual_pricing` from template recipes (needs a
base price FRIDAY does not publish, template convergence never rewrites a
stored price, and display pricing does not read it); installing every
Offering price into the registry (reprices 62 unrelated live offerings); a
resolver-only overlay (registry snapshot readers such as compaction's verified
lookup would not see it); binding provider ids from the routing authority at
route time (processes that price before reading it would disagree).
Specification: `tests/pricing/test_friday_claude_cache_billing.py`.

For billed requests, `lib/billing/request_flow.py` owns the full sequence:

1. estimate and reserve before dispatch;
2. settle the reservation against terminal usage;
3. append an idempotent ledger entry and update the wallet in one transaction;
4. reclaim abandoned reservations through one durably claimed scheduler task.

Problem: task-backed chat and agent-run routes duplicated reservation, HTTP
refusal and lifecycle binding. Decision: `routes/api_v1/_task_billing.py` owns
that concrete HTTP preparation over `request_flow` and `ExecutionSession`, with
explicit account, session, task, conversation, final messages, config, completion
hint and model lookup. Empty accounts skip estimation, conversion, reservation
and binding; nonempty accounts bind even a zero reservation. Completion integer
conversion stays inside the reservation failure guard. Chat's settlement model
lookup remains late-bound to its config; agent-run retains its resolved model.
Refusal/error causes, the canonical 402 projection and original exception
propagation are unchanged. The session still owns rollback, pre-dispatch refund
and terminal settlement, including deferred cleanup debt.

Alternatives rejected: a generic billing/dependency framework obscures the
existing owners; copying the block permits financial error paths to drift;
folding in the direct relay changes its invoice and thread-placement contract.
`tests/api/test_task_billing_preparation.py` exercises both handlers without
storage or services. Empty accounts retain zero estimate/reserve/bind/wallet
calls; billed preparation retains one prompt estimate, one reserve and one bind,
including zero holds, with no extra wallet read. Positive holds still make one
wallet reserve; disabled/free preparation makes none. Routing alone is already
offloaded; preparation stays on the request loop and settlement on its owning
terminal caller. This consolidation reduces runtime duplication, not ledger
operations, and establishes no whole-service latency improvement.

Direct paid relays use `request_settlement.py` and the shared `reservation_admission.py` wallet barrier. The [direct request settlement contract](../contracts/HEADLESS_API.md#direct-request-settlement) owns complete physical-attempt invoices, unknown holds, cold recovery and limits.

`wallet_janitor.py` alone queries bounded stale reserves, skips known-running requests, and uses settlement's idempotent release; `janitor.py` is thread-free compatibility. Its durable five-minute task runs only for multi-user billing, so open/private/BYO-only modes poll zero times. `TOFU_BILLING_JANITOR=0` and the legacy TTL alias remain. Distributed scheduling invents no owner; `TODO(enterprise)` requires owner enumeration and durable in-flight leases.

Native billed roots with positive holds acknowledge one zero-value `native_admission` ledger entry before model dispatch. Registered children resolve that exact live root through the shared execution account; they do not invent a second wallet. `lib/billing/native_admission.py` owns this barrier and its root-local ACK cache. The ledger kind and guard vocabulary are generated from `contracts/tofudb_ir_v1.json`; the existing wallet transaction and immutable ledger remain the authority on both SQL and Tofu-DB.

Problem: a Flow child may incur paid fees before its root has a terminal result or a native ROOT checkpoint. Best-effort task-result metadata can be absent or replaced, causing a cold janitor to classify the hold as legacy and refund it. Decision: wallet admission and every standalone release serialize on the same wallet authority. Admission requires the original positive, unreleased reservation and no terminal debit. A protected hold can only be released by exact settlement; generic ledger append cannot bypass the barrier or manufacture an admission. Missing native invoice evidence stays unresolved even if task/conversation records are deleted. The admission entry never claims a price, usage or completed result.

Each admission attempt uses a fresh command delivery identity and the same stable ledger identity. Reusing an earlier command receipt could skip the live hold check after settlement; identical live retries instead revalidate and return the existing ledger entry. Lost ACKs cannot grant a cached provider permit. The joined model callback rechecks cancellation after ACK, and failed admission remains provably unsent. Subsequent requests on the same live root reuse its exact acknowledged owner/wallet/task/amount tuple. Unbilled or zero-hold execution creates no entry.

Alternatives rejected: persist only an in-memory flag or task metadata (crash, checkpoint replacement and deletion lose it); query a marker then refund outside the transaction (a concurrent admission can win between the read and release); or freeze an incomplete root invoice while children are still running (missing quotes become a false zero-debt settlement). One bounded immutable ledger entry per billed root is financial evidence and follows ledger retention, not cache GC. It introduces no poller, thread, extra balance charge or per-round durable write.

A private SQLite measurement over 64 roots and 640 acknowledged retries recorded 64 admission commands and zero cached commands: median warm admission 1.248 ms, cached validation 5.463 µs, and 272 JSON bytes per ledger entry. The root cache tuple plus lock occupied 128 shallow bytes, excluding referenced values. Total physical file growth was 2,776,880 bytes, including ordinary receipts, indexes, logs and allocation; this is not per-entry storage cost. These measurements do not establish native-engine or end-to-end model/Goal recovery latency.

The append-only ledger is authoritative; wallet balance is a recomputable cache. Reserve and settle must share one enablement decision per request; if billing is disabled after a reservation was created, settlement releases that durable hold rather than stranding it behind the disabled path.

### Dynamic and frozen charge ownership

Problem: the dynamic rate adapter also owned native child fee witnesses,
account coverage and frozen credit folding. Checking each transferred row
scanned every grant and hashed unrelated rows, multiplying work as actors grew.

Decision: `cost.py` retains canonical dynamic pricing and unit conversion;
`cost_types.py` owns their shared integer result. `native_cost.py` keeps frozen
credit folding and its mutually dependent live/recovered witnesses together.
Terminal capsules and wallet recovery remain with `native_settlement.py`.
Internal callers import the native owner directly; the public
`cost.CostBreakdown` export remains the same shared type.

Each projection builds a temporary index from original producer/round to all
candidate grants, only when transferred rows need validation. A candidate still
passes its existing exact digest and owner checks. The index preserves distinct
payers, repeated local round numbers and multiple candidate witnesses; it never
caches authorization across projections. Duplicate transferred rows, modified
evidence, unknown usage and uncovered account spend still refuse settlement.

Alternatives rejected: a generic pricing strategy would obscure the prohibition
on repricing frozen fees; separating mutually validating witnesses and folding
would introduce circular dependencies; a persistent permission cache would need
invalidation and could authorize changed evidence. Limiting actors or invoice
rows would change accepted work. The optimization removes unrelated checks
without changing ledger operations, fees, retained evidence or admission policy.

Verify the native fee lookup, child transfer, recovered actor, prefix restore,
terminal settlement and wallet lost-ACK specifications, plus the canonical
generated pricing oracle. Performance comparisons must use identical original
quotes and account coverage, and report invoice-validation cost separately from
end-to-end execution.

### Payment amount conversion

**Problem:** binary floating-point conversion lost integer credit units, while
invalid or zero configured rates silently selected the default. **Decision:**
`payments/amounts.py` converts exact decimal rates using the existing credit unit
and half-even rounding, independent of the caller's decimal context. Only an
absent `credit_per_minor_unit` selects 1.0. Nonfinite/nonpositive rates, noninteger
minor amounts, zero rounded credits and signed 64-bit overflow refuse before
storage. The accepted payment row freezes its credit amount for settlement.
**Alternatives rejected:** rounding through floats retains precision loss;
substituting a default for invalid configuration silently changes the purchase.
Verify `tests/billing/test_payment_amounts.py` and payment concurrency tests.

### Stripe webhook admission

**Problem:** checkout completion can precede payment, while separate event
handlers could credit the same transfer under a session ID and a PaymentIntent
ID. Signature dictionaries also discarded valid signatures during key rotation.
**Decision:** `lib/billing/payments/stripe_events.py` authenticates the unchanged body
against every v1 signature with one unambiguous timestamp and the existing
five-minute tolerance. It admits received nonnegative integer amounts and
explicit currency/owner facts. Checkout events require one-time payment mode
and `payment_status=paid`; delayed success uses the same path. Zero received
funds, unpaid sessions and unrelated events acknowledge without storage writes.
Malformed signed facts return 400. A requested amount never substitutes for
an absent or zero received amount.

PaymentIntent identity is the sole Stripe settlement key, including expanded
intent objects. `payments/stripe.py` joins authenticated facts to a durable local
quote and binds its provider identity before settlement. Storage errors propagate
for retry; transactional wallet/ledger idempotency and rollback remain authoritative.
**Alternatives rejected:** crediting completion mints unpaid balance; a session
fallback splits deduplication; accepting only the last signature breaks rotation;
a second receipt store duplicates the existing financial authority.
This follows Stripe's [payment event guidance](https://github.com/stripe/ai/blob/main/skills/stripe-best-practices/references/payments.md#webhooks-and-fulfillment)
and [secret rotation contract](https://docs.stripe.com/events/manage-webhook-endpoints#roll-endpoint-signing-secrets-periodically).
Verify `test_stripe_webhook_admission`, `test_stripe_payment_settlement` and the
Sidecar settlement rollback specification. Payment record/bind/find/list/settle
run on SQLite, PostgreSQL and both native transports.

### Stripe checkout quote and provider binding

SQL ownership is explicit: `operation_domains/billing.py` declares operations;
`operations_pkg/_billing_wallets.py` owns wallet/ledger/reservation transactions,
`_billing_redeem.py` owns redemption and `_billing_payments.py` owns quotes,
binding and settlement. Both credit producers call the same wallet owner;
`_billing_values.py` holds only shared scalar/JSON validation. Native checkpoint
settlement imports the wallet owner directly. Keeping all implementations in the
catalog or duplicating wallet application in each producer was rejected because
it obscures transaction ownership and allows financial invariants to drift.

**Problem:** creating the remote Session before a nonfatal local write could
expose a payment link without a durable quote. A later PaymentIntent callback
created another row using the current credit rate, leaving the original Session
pending and changing an already quoted purchase.

**Decision:** `payments/_common.prepare_payment` records one pending quote with
an independent local ID and empty provider ID before provider IO. Creation requires
both the API secret and callback signing secret. Stripe receives the local ID in
both Session metadata and `payment_intent_data.metadata`, and as the request's
idempotency key. Owner, amount, currency and credit are frozen in that row.
`billing.payment.bind` atomically verifies those facts and claims one nonempty
provider identity. Competing payments cannot share a claim; a different identity
cannot replace an accepted binding. Failed/unbound rows cannot settle. Binding
and settlement retain independent command receipts so retry after either commit
acknowledgement is lost can finish without repricing or credit duplication.

Legacy canonical PaymentIntent rows retain their original quote. A pending
legacy Session row can migrate only when an authenticated Checkout event names
that Session and it matches the row's saved `raw.checkout_session`; the same
transaction replaces the provider claim. The old Session identity remains in
settlement evidence. Callbacks cannot create a missing quote from user metadata;
unmatched historical payments require reconciliation. Historical duplicate rows
are not deleted or repriced. Offline native migration preserves independent
pending quotes without inventing empty provider claims.

**Alternatives rejected:** callback-created quotes permit repricing; keeping both
Session and Intent rows duplicates purchase state; a new order table or receipt
store duplicates existing authority. Existing SQL partial unique indexes and
native claim indexes already express optional provider identity.

**Checkout error taxonomy. Problem:** `/api/v1/billing/checkout` answered every
`RuntimeError`/`ValueError` from order creation with 400 and its raw text, so a
malformed merchant key, a missing signing dependency, a network failure or a
Stripe outage read as the customer's mistake. **Decision:** order creation raises
`BadRequest` only for caller-attributable refusals: an unconfigured provider
(`field=provider`), an invalid currency (`field=currency`) and a Stripe HTTP 400
parameter rejection (for example an amount below the provider minimum). The
route keeps those 400s with their `error_kind` (`not_configured`,
`stripe_checkout_failed`); any other failure is a logged, redacted 500 and a
storage failure keeps the storage taxonomy. **Alternatives rejected:** treating
every Stripe 4xx as the customer's fault would also blame them for an invalid
secret key (401) or provider throttling (429).

Provider references: [Stripe metadata propagation](https://docs.stripe.com/metadata)
and [idempotent requests](https://docs.stripe.com/api/idempotent_requests).
Verify `test_stripe_checkout_admission`, `test_stripe_checkout_lifecycle`,
`test_payment_binding_contract` and `test_payment_quote_migration`; SQL rollback,
legacy migration, concurrent claims, cold restart and lost acknowledgements are
part of the contract. Growth evidence and retention live in
[storage operations](../STORAGE_OPERATIONS.md#payment-quote-retention).

### Alipay local-order authority

**Problem:** valid provider signatures alone did not bind a callback to this
merchant's order, amount or wallet. Order IDs could collide within one millisecond,
and request signing incorrectly used the notification exclusion rules.
**Decision:** checkout signs page-pay fields including `sign_type`, assigns a
random order ID, and durably records the pending payment before returning the
redirect. The existing row freezes wallet, CNY cents, credit micro-units, app ID
and seller ID; no second order store is introduced. Configure
`payments.json:alipay.{app_id,seller_id,private_key_pem,alipay_public_key_pem}`.
The requested seller is also present in `biz_content`.

`payments/alipay_protocol.py` owns signing and authenticated immutable payment
facts without configuration or storage IO; `payments/alipay.py` owns checkout
and the local-order join. Both providers share `payments/amounts.py` conversion
and `_common.py` persistence/settlement instead of duplicating money state.

Notify requires RSA2, one scalar value per form field, a valid signature, and the
configured app/seller. Paid notifications must also match the local order's
merchant binding, currency and exact amount. Only `TRADE_SUCCESS` and
`TRADE_FINISHED` settle; other authenticated states do not credit. Neither
`passback_params` nor order-name parsing selects a wallet. Plain `success` follows
the atomic settlement acknowledgement; storage errors retain retry behavior.
Replays and concurrent notifications use the existing payment/ledger idempotency.
Changing a rate after checkout does not reprice an accepted order.

**Alternatives rejected:** reconstructing orders from callbacks trusts remote
metadata as financial authority; timestamp IDs collide; separate receipt/order
stores duplicate existing durability; applying current rates on settlement
changes a purchase already quoted. Legacy outstanding orders without a locally
recorded merchant binding fail closed and require merchant reconciliation; do not
backfill ownership or balances from callback metadata. These durable pending
orders are financial evidence, not TTL-evictable cache entries.

A synthetic 2026-10-28 retention measurement created 16 orders and delivered 64
success notifications on each of SQLite, native daemon and native embedded.
Every backend retained 16 payment rows and 16 ledger rows: callbacks created no
additional payment rows. Compact UTF-8 JSON measured 346 bytes per pending row,
917 per settled row and 276–278 per ledger entry. Observed total file growth after
pending creation was 428,480 / 89,791 / 94,901 bytes, respectively; after callbacks
it was 2,537,920 / 422,619 / 416,214 bytes. Physical totals include receipts, logs,
indexes and allocation, so they are neither per-row costs nor throughput claims.
Each new checkout retains one pending record even if the buyer abandons it;
cleanup must not treat that evidence as a reconstructible transport cache.

Protocol references: [Alipay notification validation](https://developer.alibaba.com/docs/doc.htm?articleId=105902&docType=1&source=search&treeId=193)
and [official page-pay request signing](https://github.com/alipay/alipay-sdk-python-all/blob/master/alipay/aop/api/DefaultAlipayClient.py).
Verify `test_alipay_admission` and `test_alipay_payment_settlement`: independent
RSA checks, bad merchant/amount/no-order refusals, opaque wallets, same-millisecond
orders, storage restart, concurrent delivery, committed lost ACKs and public HTTP.

## Codex subscription recovery

The existing bounded catalog worker doubles stable-poll intervals up to one hour, but wakes at least five minutes before a stored Codex access token's expiry. A near-expiry idle poll uses the same cross-process OAuth singleflight refresh as requests, even when the model catalog is still fresh; transient catalog/provisioning errors retry within 15 minutes. An explicitly revoked or absent refresh token is never replayed and cannot be renewed without the user's authorization. An explicit logout stops the catalog worker and forgets staged rows only after credential deletion succeeds. Last-admitted catalog rows remain available during transient network/auth outages, whereas freshly fetched rows are not published until model-routing admission succeeds. The repair cannot override deliberately disabled provider access or an explicit capability/credential restriction; see `docs/MODEL_REGISTRATION.md`. Browser/code login commits share the refresh/logout transaction boundary, so an old refresh cannot overwrite a completed new login. Failed refresh fallback re-reads current credentials and current time; expiry crossed during a wait, logout, and replacement login never return the pre-refresh bearer. Problem: slow network operations outlive credential snapshots. Decision: reuse the existing transaction lock and revalidate fallback authority; alternatives rejected: a second credential store, recursively locking generic `save_token`, or trusting stale expiry. Specs: `test_oauth_token_store.py` login/refresh and expiry-boundary regressions.

## Domestic coding subscriptions

Settings → 订阅登录 also connects domestic coding plans. Mechanisms and terms
were checked against each vendor's official documentation on 2026-11-06.

**Kimi Code membership sign-in.** `lib/oauth/kimi.py` runs the RFC 8628 device
grant of the open-source Kimi Code CLI (MIT, design note KLIP-14) against
`auth.kimi.com` with that CLI's public client; there is no client secret. The
manager's one device loop (`lib/oauth/manager/_device.py`) runs each provider's
protocol, so Kimi shares the Codex status, logout and reconciliation path. The
login lives in the common token store. Refresh is a cross-process singleflight
once under five minutes remain, a 401 forces one refresh for that request, and a
rejected refresh token is cleared and never replayed; an expired login raises
the typed `oauth_reauth` failure. Provider `oauth_kimi`
(`https://api.kimi.com/coding/v1`) takes its roster from the `kimi_code`
template, so sign-in and a pasted Kimi Code console key expose the same supply.
Requests carry Tofu's own client identity, never the CLI's
([IDENTITY.md](../IDENTITY.md#kimi-code-sign-in)). Kimi's 5-hour, weekly and
monthly usage-limit 429s enter the existing subscription-quota channel; an
exhausted credit balance is billing quota.

**Plan-key templates.** A template of category `subscription` is a vendor's own
plan-bound coding endpoint reached with a plan API key
([recipes](../PROVIDER_TEMPLATE_RECIPES.md#coding-plan-subscriptions)). Settings →
订阅登录 renders one card per such template (`subscription-plans.ts`): key-console
and plan-details links, a password input, then status and Disconnect. Connect
compiles the template and stages it with the key through the same Provider add
command as the 服务商 picker, which lists these templates under 订阅套餐;
Disconnect is the ordinary Provider removal. The browser keeps no plan list or
credential.

| Plan | Template | Terms for a tool such as Tofu |
|---|---|---|
| Kimi Code (console key) | `kimi_code` | third-party tools allowed; the client identity must stay honest |
| Alibaba Bailian Coding Plan | `bailian_coding_plan` | interactive use inside coding tools only: no automation scripts, application backends or non-interactive batches; inputs and outputs may be used to improve models |
| Tencent Cloud Coding Plan | `tencent_coding_plan` | interactive use inside coding tools only, same exclusions |
| Volcengine Ark Coding Plan | `ark_coding_plan` | personal development; plan quota applies only inside AI coding tools, not to API calls |
| MiniMax M Plan | `minimax_mplan` | any OpenAI- or Anthropic-compatible tool; interactive personal use |

Never make Bailian, Tencent or Ark models the default for background jobs such
as titles, translation or evaluations. Plan Offerings carry no price tier, so
the `cheap` pools that auto titles and language correction request skip them.
Tofu has no interactive-only Offering policy yet, though: unpinned background
dispatch is otherwise pool-wide (a capability request with no matching slot
falls back to any text slot, and owner-wide auxiliary summaries consider every
enabled text Offering), so a connected plan can still be selected for such work.

Not integrated: Zhipu's GLM Coding Plan, whose terms name the permitted tools
(Tofu is not one; outside them the plan quota does not apply and the account
balance is charged); Qwen OAuth (free tier discontinued 2026-04-15) and iFlow
(closed 2026-04-17); Baidu Qianfan's Coding Plan (model updates stopped; Baidu
points to its Token Plan); CodeBuddy, Trae and Lingma (closed backends without a
third-party API); web-chat reversers (cookie scraping, against vendor terms).

Owner steps:

- Kimi Code (membership Plus or above): Settings → 订阅登录 → Kimi Code →
  设备码登录; open the page shown on any device, sign in and confirm the code;
  the card then shows the connected model count.
- Bailian, Tencent, Ark, MiniMax, or a Kimi Code console key: open the card's
  获取 Key link and create the plan key; paste it into the card and press 连接;
  pick a plan model in the composer for interactive work.

Specifications: `tests/oauth/test_kimi_oauth.py` (device grant, refresh and
expiry against a fake `auth.kimi.com`), `tests/oauth/test_kimi_subscription.py`
(status, logout, routing, identity headers, 401 refresh, quota),
`tests/providers/test_subscription_plan_templates.py`,
`tests/providers/test_subscription_settings_cards.py` and the Kimi device
scenarios of `tests/frontend/test_settings_import_oauth_lifecycle.py`.

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
| Daily key health/admission | `lib/key_stats/` | dispatcher picker and optional-work policy tests, key-stats persistence specs |
| Model rate / wallet/ledger mutation | `lib/pricing/`, `lib/billing/` | `lib/cost.py` tests, billing settlement, idempotency, rollback, janitor tests |
| Rate policy | `lib/rate_limit/rate_limit_api.py` | shared store and response headers |

## Test map

```bash
pytest -q tests/identity/test_identity_contract.py tests/api/test_api_keys.py tests/model_routing/test_model_routing_contract.py tests/byo_egress/test_byo_egress.py tests/startup/test_secret_envelope_startup_boundary.py
pytest -m unit -q tests/oauth/test_oauth_outbound.py tests/oauth/test_oauth_exchange_errors.py tests/oauth/test_codex_usage_reset.py tests/oauth/test_codex_usage_reset_notice.py tests/llm_dispatch/test_key_stats_model_exhaustion.py tests/llm_dispatch/test_key_stats_no_429_auto_disable.py tests/key_stats/test_key_stats_persistence.py
pytest -q tests/cost/test_cost.py tests/billing/test_billing.py tests/storage_authority_sql/test_rate_limit_store.py
pytest -m unit -q tests/oauth/test_kimi_oauth.py tests/oauth/test_kimi_subscription.py tests/providers/test_subscription_plan_templates.py tests/providers/test_subscription_settings_cards.py
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
The shared child-round stamper observes the same frozen-cost marker before any
Python price lookup. **Problem:** the final fold respected native snapshots while
the earlier stamper could fill an unpriced native row from today's catalog.
**Decision:** preserve absent, null and empty native costs exactly; ordinary
Python requests retain their request-bound pricing policy. **Alternative
rejected:** filling the gap with a current price invents a historical quote and
can change subsequent native aggregation. Verify
`tests/orchestration/test_flow_child_accounting.py` and native cost projection.

### Native credits into existing wallet settlement

Problem: Python terminal repricing changes settled Rust fees, and root-only quotes omit child consumption. Decision: native_task_cost folds original per-round integer components/margin into the unchanged atomic wallet settlement. Missing account, unknown/uncovered usage, invalid/duplicate rows and signed overflow refuse; known unsent zero consumption can release its hold. Pricing is marked before dispatch. transfer_native_child_fees admits same-model descendants; tree journals require checkpoint_tree_usage; ordinary Continue validates the owned source invoice. It validates captured attachment ancestry and settled actor usage before terminal ACK. Original quotes retain taskId/billableTaskId/sourceRound; immutable grants hash each row. Foreign, altered or duplicate rows refuse; detached retries verify without appending. Recovered actor completion preserves each historical `billableTaskId` instead of retagging the entire result to the new root. Problem: a child can contain several executor generations; selecting retry rows only by the current child ID loses its historical rows and replacing their payer assigns old debt to new work. Decision: the existing grant binds both the original child rows and their exact transferred producer/source-round/payer identities. An inherited prefix requires an acknowledged shared-account rebind with exactly matching original token consumption. Missing payer identity, changed prefixes and failed frontier ACKs refuse before parent projection. All rows cover cumulative consumption; only rows whose witnessed payer is the current root enter its wallet capsule. Alternatives rejected: omitting historical rows breaks full token coverage; retagging them charges prior work again; trusting a prefix without account rebinding creates an unproved billing exclusion. These witnesses remain local to the existing root lifetime and add no durable record or worker. They do not authorize actor execution: atomic native actor takeover and durable restoration of mixed-origin actor fees must still be connected before automatic Goal recovery. Verify `test_recovered_actor_fee_transfer.py` for multiple generations and altered proofs, and `test_recovered_actor_fee_wallet.py` for original/successor settlement and lost ACKs on SQLite, native daemon and embedded authorities. For 2/32/256 original-plus-fresh rounds, a seven-sample local measurement recorded median host transfer times of 252/2,177/16,359 µs and newly allocated immutable grant retention of 4,005/11,473/69,409 bytes. These cover fee validation/copying and both row-hash indexes; they exclude model execution, native handoff, storage and parent result serialization. Grant lifetime remains the root lifetime. Account locking precedes shared content locking and performs no I/O. Actor completion never certifies tree quiescence. Descendants transfer to the owned root with executor/source ordinal intact; intermediates acquire no wallet/Turn/recovery authority. No pool, ledger or wallet schema is added. Edges detach with actors; dictionary capacity/grants last with the root (512 live edges: 13,056 bytes on Python 3.12). Tree restore validates root/tree coverage precedes retained witnesses; capsules include transferred fees. Bare Continue validates source owner/accepted lineage, exact quotes and all capsule components before increasing spent budget once. Same-executor recovery still funds its unsettled rows; only a preceding foreign-task prefix stays paid elsewhere. Wallet retries/recovery consume the same capsule without replay. Alternatives: repricing loses paid rates; separate child wallets split ownership; ignored child usage undercharges. Tests: test_native_child_fee_transfer, test_native_task_cost, test_relay_billing_gate, test_native_priced_child_wallet_transfer and native_cost cases in test_native_task_terminal, test_native_priced_tree_checkpoint_handoff and test_native_invoice_prefix_restore. Default remains unqualified.
### Native terminal fee recovery

**Uncertain usage and terminal outcomes.** Problem: refusing an invoice for unknown native usage also prevented an already failed Turn from being persisted. Decision: `native_settlement.terminal_metadata` permits error, interruption and abort outcomes to retain `nativePricingRequired=true` and `usageComplete=false` without a settlement capsule. The ordinary task-result/Turn writer still owns terminal publication; the existing wallet recovery owner continues to refuse settlement or refund until exact native evidence is available. Successful outcomes, missing financial accounts, invalid known quotes and mutation of an already frozen invoice retain their original validation. Alternatives rejected: a zero invoice would refund uncertain debt; withholding the failure record keeps the conversation running after execution has stopped. Verify `test_native_terminal_projection` and `test_goal_native_source_sigkill`, including committed whole-set handoff with a lost response, no subsequent provider call or effect admission, one durable consumed receipt, and repeated wallet recovery. The genuine crash fixture's target remains an explicit port composition; automatic pending dispatch and recovery after another process loss remain fenced.

Problem: the reserve sweeper refunded a hold after process loss even when Rust fees were confirmed and terminal delivery had not acknowledged the wallet. Decision: reserve admission binds the opaque wallet subject explicitly. Native terminal projection freezes one closed-schema nativeBillingSettlement capsule before its outcome digest and the existing owned task-result CAS; metadata also marks native-required work before terminal. The capsule contains owner/task/wallet identity, original reservation, model and fixed integer component totals, not usage/history or a new ledger. NATIVE_BILLING_SETTLEMENT_SCHEMA in lib/billing/native_settlement.py is its machine-readable shape authority. The existing sweeper maps wallet subject through tenant.user.get, reads the owned terminal result, validates the capsule and calls the unchanged idempotent wallet settlement. Missing native terminal evidence, changed identity/reservation, invalid amounts and uncertain reads retain the hold as an error. Legacy orphan handling remains at its existing owner. Live delivery and recovery use the same frozen model/amount/note with schema-ordered components despite storage JSON key reordering; later caller aliases or price changes cannot change the command. Terminal retries additionally fence usage, round fees, reservation and capsule mutations. Alternatives: TTL refund loses paid work; replaying execution repeats effects; a second financial database splits authority; parsing an opaque wallet ID as a numeric repository owner bypasses the identity mapping. Tests clear live task/account state and run a fresh interpreter over the actual SQLite authority after terminal metadata publication; normal and lost-ACK cases retain exactly one debit/release. Real native sync/streaming tests verify capsule publication through canonical persistence. This covers committed terminal fee handoff, not a kill before terminal CAS, in-flight model takeover, missing initial checkpoint evidence, no-reserve recovery scanning, or production-default cutover.
