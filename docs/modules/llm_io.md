# LLM I/O and dispatch

This domain compiles owner-authorized v2 routes, emits canonical requests, and
normalizes upstream results. Entity/selection rules live in
[`../MODEL_REGISTRATION.md`](../MODEL_REGISTRATION.md); `lib.llm` and
`lib.llm_dispatch` are lazy execution facades, not configuration authorities.

## Ownership

| Concern | Owner |
|---|---|
| Provider-independent chat/stream API | `lib/llm/chat.py`, `astream.py`, `stream.py` |
| Canonical request-body construction | `lib/llm/body/` |
| Shared transport, connection and timeout policy | `lib/llm/_transport.py` |
| Responses / Anthropic translation | `lib/llm/{responses,anthropic}_outbound/` |
| SSE byte framing | `lib/llm/_sse_framer.py` |
| Provider-payload normalization | `lib/llm/_sse_core.py`, provider `_sse.py` modules |
| Model/provider/access authority, migration, candidate compilation, scoped health, snapshots | `lib/model_routing/` |
| Request-owned chat/non-chat slot execution and affinity | `lib/model_routing/{dispatch_adapter,capability_adapter}.py`, `lib/llm_dispatch/{dispatcher,slot,conv_affinity}.py` |
| Cross-surface execution lifecycle, direct relay, bounded non-task model services | `lib/agent_core/{execution_session,direct_stream}.py`, `lib/log_compression.py`, `lib/model_routing/embedding_execution.py` |
| Retry/caching and transport-attempt policy | dispatch health modules, `lib/llm/cache.py` |
| Dispatch operation surface (chat/stream/multi-key/budget/contention/hygiene) | `lib/llm_dispatch/api.py` (re-export facade) + `_api_{chat,stream,stream_state,multi,budget,contention,hygiene,errors}.py` shards |
| Managed local deployment (model path → running local provider) | `lib/local_serve/` (+ agent surface `lib/tasks_pkg/handlers/local_serve.py`, `lib/local_serve/tool_defs.py`) |

## Request flow

1. Parse a structured native ModelRef or resolve one compatible string with explicit Creator/Provider hints; ambiguity is an error.
2. Read the owner-scoped v2 aggregate, hard-filter authorization, capability, context, protocol, budget, probe, stale/pending, and health state, then bind the bounded ordered candidates as request-owned slots.
3. Build the provider-neutral request body once; root rounds retain a validated positive full-prompt admission count as an internal-only sidecar, so same-model slot retries and same-conversation cache-settle classification reuse it while invalid, missing, or cross-model evidence recomputes locally and every provider wire strips it. The first exact wire-schema digest is reused only while the model and every ordered schema object remain identical. Per-slot adaptation deep-clones message history only for families that mutate it (cache-marker/Claude or Gemini); read-only OpenAI/Responses projection reuses the canonical list, while caller-byte immutability remains the enforced boundary. A VLM-to-text fallback projects only that derived copy: one bounded marker replaces all images in each image-bearing message at their first image position, adjacent captions/references/tool results/prior assistant descriptions remain, pixels are declared unseen, and the durable multimodal history is unchanged. Final transport diagnostics derive semantic, whole-message-byte, and field-byte fingerprints in one shared history traversal; canonical field normalization emits the temporary tool alignment key in the same message scan, takes standard strings/text blocks directly while routing images and unknown shapes through the generic normalizer, and already-required field serialization proves whether an encoded `cache_control` key exists, so only marker-bearing or malformed messages allocate a marker-free projection. Each top-level value is serialized once for both raw views and remains an independent reference until one final whole-message join; primitive fields use byte-identical direct encoding, complex fields share a stateless configured stdlib encoder, and tool arguments use sorted orjson canonicalization. Message-sized values use process-local keyed integers, canonical rows retain only their alignment key plus field map, and stable-format hoisted/static/routing digests remain separate while standalone diagnostic APIs retain identical outputs.
4. Translate only at the provider adapter boundary.
5. Execute with bounded connect policy and a 300s default rolling stream-idle window; every SSE/WS event renews it, so it is not a total request wall clock.
6. Normalize stream events and terminal usage into the internal vocabulary.
7. Settle health on the factual Deployment, Connection, Credential, or Credential×Deployment scope; record cost/usage, cache observations, retry outcome, and the final redacted RouteSnapshot.

Non-chat surfaces enter at step 2 through the capability adapter. Listings emit only enabled, authorized, probe-passed model/provider pairs. Execution pins and disposes a request group; strict logical matching includes later-injected slots. Missing routes fail closed without legacy provider/global-key fallback.

Continuation rounds retain Provider preference and candidate ordering unless typed failure policy declares failover. Failover never rewrites conversation preference; metadata stores structured requested and actual v2 identities, not display labels.

## Provider adapters

Provider adapters translate wire vocabulary; they do not own task policy, context compaction, billing, or tool execution.
OpenAI-compatible, Responses-based, Anthropic, subscription, and owner ProviderAccess paths converge before task code consumes deltas.

A translator preserves text/reasoning/tool work, finish/truncation meaning, cache/reasoning usage, ordered tool-call occurrences, and typed errors without leaking credentials. Streaming tool assembly continues an index-less active slot only while ID/name evidence remains compatible.
A different ID opens a new slot even when its name arrives later; same-name/no-ID ambiguity or an invalid index makes the stream malformed instead of merging executable calls. Exact complete-frame retransmission at the same stable slot is ignored, but equal payloads at different positions remain distinct.

Responses adapters use `output_index` as the primary occurrence identity and use an item ID only while it is unambiguous. Recycled item IDs at distinct positions remain separate; a later delta carrying only an ambiguous ID fails closed.
A terminal `response.output` array is the ordered authority and replaces provisional opaque items position-for-position, preserving even byte-identical entries. The WebSocket incremental-input ledger counts occurrences rather than set membership, and advances state only after a verified terminal response; malformed or interrupted streams retire the socket.
Unknown provider payloads fail explicitly: do not add `.get()` chains that turn a malformed response into an empty successful answer.

## SSE framing and stream-activity watchdog

All byte-stream transports use `lib/llm/_sse_framer.py`. It incrementally decodes strict UTF-8 and frames SSE events across arbitrary byte boundaries,
including CR/LF/CRLF, comments, a leading BOM, repeated `data:` fields, multiple
events in one read and a split `[DONE]`. One event is capped at 1 MiB. Invalid
UTF-8, invalid JSON, an oversized event, or an unterminated EOF frame closes as
`malformed_stream` with bounded, credential-free diagnostics. The Responses
WebSocket path submits an already-decoded provider payload directly; it never
manufactures a `data:` line.

`TOFU_LLM_IDLE_STREAM_TIMEOUT_S` is one attempt's continuous transport-idle window, not its maximum wall time: default 300 seconds, `0` disables it, and positive values below 30 seconds clamp to 30. This matches native Codex's rolling stream behavior; sync SSE, async SSE, and Responses WebSocket paths share these rules. The pinned `@openai/codex` `rust-v0.149.1` [provider default](https://github.com/openai/codex/blob/rust-v0.149.1/codex-rs/model-provider-info/src/lib.rs#L26-L27) is 300,000 ms, and its [Responses SSE loop](https://github.com/openai/codex/blob/rust-v0.149.1/codex-rs/codex-api/src/sse/responses.rs#L552-L575) wraps each next stream event in that rolling timeout.

- the live response boundary initializes the activity anchor;
- every received byte chunk or WebSocket message renews the anchor, including
  SSE comments/keep-alives, empty data, signatures, usage, finish metadata, and
  protocol-only events such as `response.in_progress`;
- reasoning/content/tool deltas remain semantic diagnostics, but their absence
  is never a termination condition;
- reaching the window with no transport activity closes the attempt as a
  transport interruption (`premature_close` / `midstream_close`), so the normal
  bounded stream retry and route-health path applies.

`TOFU_LLM_SEMANTIC_IDLE_TIMEOUT_S` and `TOFU_LLM_NO_ACTIONABLE_TIMEOUT_S` are
deprecated lower-priority aliases for the same transport-idle duration. They
log an import-time warning because keep-alives now renew the value. New attempts
never emit `semantic_progress_timeout`; that state, its usage flags, retry label,
and `no_actionable_output` remain read-only compatibility for stored timelines and plugins.

There is no absolute per-attempt ceiling while transport events continue; model
token limits, task/round policy, and user Stop still govern it. Typed stream
evidence and Turn Trace continue recording request elapsed time, response
headers, transport bytes, complete SSE events, transport/semantic ages,
reasoning/content/tool progress, provider finish, client abort and bounded
malformed-frame diagnostics. Legacy usage flags are projections of that evidence, never a second classifier.

## Dispatch policy

`lib/model_routing` owns eligibility and route ordering;
`llm_dispatch` executes the resulting request-scoped slots and owns bounded
attempt/retry mechanics. No caller, provider adapter, OAuth bridge, or local
engine may create a second ProviderConfig/BYO/alias selector. Ordinary calls
may prefer a Provider but cannot hard-lock a Connection or Deployment.
Pricing-tier tags describe cost only and never establish protocol support.
Chat eligibility removes those managed tags before classifying operational
capabilities, and catalogue pricing refresh actively strips stale tier tags
from embedding/image/audio-only models.

Configured enablement and runtime health are distinct. A route-missing verdict
excludes only its Deployment, network failure its Connection, 401/402 its
Credential, and 403 the Credential×Deployment authorization. Pending identity
is reachable only through its explicit Provider+Offering reference; stale and
unprobed Deployments are excluded from automatic selection. Once every route
for an official Model is exhausted, compatible-model fallback remains subject
to the same capability, context, protocol, and explicit price limits.

Health penalties and retry decisions must be reasoned from typed failures.
Cancellation, user abort, and deterministic request errors are not provider health failures.
Local request construction/projection failures are typed before provider
ingress, release any reserved slot neutrally, and never trigger model fallback,
pool rescue, provider-health penalties, or upstream-attempt budgets.
Deterministic HTTP 400/404/422 rejections surface on the selected model — no configured fallback, pool-wide rescue, or caller-level translation replay.
 One bounded exception: a 404 on a subscription-OAuth route is absorbed by at most `SUBSCRIPTION_404_MAX_RETRIES` same-route transport retries (2026-09-02 chatgpt.com codex backend flapped per-request 404s for minutes while endpoint, token, and payload were healthy); the absorption is not a model switch, consumes no fallback budget, and a persistent 404 still surfaces request-scoped once the budget is spent. Keyed-gateway 404s are never absorbed — there the status really means the wire model ID is unknown. Surfaced 404 envelopes classify as kind `not_found` (distinct from `bad_request`) so the user-visible title stops claiming HTTP 400.
 An explicit 400 denying any route for a wire model ID is catalogue/routing evidence, not a payload rejection: dispatch excludes that ID durably for the call and process-locally until dispatcher rebuild/config or catalogue refresh, so the 60-second transient reset cannot resurrect it. Route-missing catalogue noise never replaces an actionable error from a provider-reaching route; among different payload 400s, exhaustion still re-raises the first, not the last fallback's.
A retry may not duplicate a completed tool side effect. A 401/403 body claiming a missing API key or authorization header while the final outbound headers contain a non-empty credential is a credential-delivery contradiction: it is gateway-class, never a permission exclusion/key-health failure, and sync/async/non-stream dispatch stops it after four actual responses.
After its one forced refresh opportunity, a typed HTTP 401 on an OAuth slot excludes that credential's whole provider key for the dispatch call because every sibling model shares the same bearer token; HTTP 403 remains pair-scoped because model entitlement can differ. Pool rescue is still non-strict, but softly prefers the configured default model before score-ranked catalogue alternatives and widens only when that default is unavailable or already failed.

A typed shared-project TPM or app/model RPM 429 is external contention, not a
key/model failure; the first rejection arms a process-local family gate. Every
later sync/async task reserves after local cache gates: a continuing rejection streak spaces provider/model probes
at 1, 2, 4, 8, then at most 15 seconds; deep queues recheck in abortable three-second slices. Automatic unpinned
work selects the eligible family whose probe is due first; explicit provider/model boundaries remain authoritative.
Reconstructible callers may opt into immediate-only admission: a due probe reserves atomically, while a still-blocked family returns typed `request_not_dispatched` without advancing the clock; durable/user-facing sync and async calls keep waiting by default. Slots stay healthy and eligible without evicting warm keys; waits are metered, two drained successes
clear the gate, quiet entries expire from a 256-family table, and live bounded gate state is exposed in dispatch status.

The Codex subscription settle profile follows OpenAI's [documented overflow routing above 15 requests/minute](https://developers.openai.com/api/docs/guides/prompt-caching): starts for one `prompt_cache_key` stay 4.2 seconds apart.
Cold cacheable requests and warm uncached tails of at least 8,192 tokens arm the extra five-second visibility window; smaller warm tails proceed after the send interval, with continued growth eventually restoring the hold.
`TOFU_CACHE_SETTLE_CODEX_WARM_WRITE_TOKENS` tunes the threshold; values below the 1,024-token cacheability floor use the safe default. Codex cache-health diagnostics retain only the prior wire message count plus a process-local digest inside the existing one-hour/4,096-entry bound; each new observation hashes its prior-length prefix to prove append-only growth, and one legacy rich entry migrates without losing evidence. Anthropic policy is independent. Auxiliary local-L2 summary prompts bind a separate opaque owner/conversation affinity to slot selection and their stream body: they neither inherit nor update the parent settle clock, repeated summaries keep a stable Codex session/cache route, and both identities remain inside the existing TTL/capacity bounds. The generic settle profile arms its 1.5-second visibility window only when the completed round proves a metered cache creation, has cold/missing cache telemetry, or has an unmetered warm suffix of at least 4,096 tokens. An explicit Anthropic `cache_creation_input_tokens=0` proves there is no new entry to settle. A smaller automatic-cache suffix can reuse the older visible prefix and proceeds immediately, bounding any extra reprocessing to 4,095 tokens instead of holding every fast tool-loop round; `TOFU_CACHE_SETTLE_WARM_WRITE_TOKENS` tunes that independent threshold. Missing or malformed usage remains conservative, and positive/cold writes retain the existing wait.

Retry execution and observability have separate budgets. Primary attended dispatch may rotate
through ordinary transient 429/cooldown states until recovery or abort; configured fallback and
pool rescue instead own a finite actual-response budget (default 3, task override clamped 1–16),
so their failure can settle rather than leave the client in `retrying`. Each LLM round persists
power-of-two samples for at most eight coarse signatures (16 each, 128 frames total); suppressed
samples still refresh liveness without unbounded `storage_events` or `storage_attempt_events` growth.

An all-slots-cooling callback carries a typed current-wait status; pool polling
is not an upstream request attempt and never increments a retry count. A real
429 or failed provider attempt may emit `retrying` with its actual attempt
number. Current-attempt waiting uses `waiting_model`; a stream that has emitted
model work and is currently transport-idle uses `stream_stalled`.
Retry callbacks identify the physical attempt model/provider and whether dispatch is strict or pool-wide; the logical requested model is not reused as
the label for a different rescue candidate.

Every task-owned logical model dispatch opens a correlated `model_request_start` span
before its waiting phase and closes it with `model_request_complete` on success,
failure, or abort. Provider/model identity and bounded failure detail live in
that diagnostic boundary; it does not settle the Turn. An allowed configured
fallback emits `model_fallback` at the decision point before the replacement
request starts. Deterministic 400/404/422 rejections close the selected-model
span and surface as failures instead of being mislabeled as provider
unavailability. The Turn lifecycle folds these facts into its bounded activity
timeline; none of them are appended to model messages.

Network route health settles on the complete stream, not response headers.
Credential-free `routeId`, `routeMode`, selection reason, and failure stage are
carried on request completion/usage diagnostics. A mid-stream close feeds a
failure to the concrete path and slot; the next stream retry avoids that slot
on its **first** dispatch pick when another slot for the user-selected model is
eligible. A stream-idle timeout is transport failure because the selected route
delivered no event for the full window. Malformed provider frames, empty
responses, and missing tool payloads penalize the provider slot but do not mark
a route that transported the stream normally as broken. User abort is neutral
to both health systems. Each reserved slot is settled exactly once. Explicit
bypass rules remain authoritative and are reported as
`direct:configured-bypass` rather than being mislabeled as a proxy timeout.

Stream-idle timeouts use the existing bounded premature-close retry budgets.
The smaller semantic-stall retry bucket exists only to render/recover legacy
attempt records; no live transport can enter it. Manual Retry remains
available, and other truncation signatures retain their own caps.

## Invariants

- One canonical body builder and one transport policy.
- Provider differences stop at focused translation modules.
- Streaming and non-streaming paths project the same terminal meaning.
- Requested ModelRef, Provider preference, and actual ProviderAccess/Offering/
  Deployment/Connection identity survive every round in RouteSnapshot.
- Secrets stay in outbound headers and redacted diagnostics.
- Chat and non-chat HTTP execution carry an explicit owner to v2; process-global environment credentials are a direct-library compatibility seam only.
- HTTP adapters never call model dispatch, provider pins, or slot reservation; an AST gate requires task execution or a declared application service, and log compression/compatible embeddings carry finite input, deadline, and admission budgets.
- Every network loop is cancellable/bounded; local health/discovery share one monitor with empty-result backoff.
- A started task-owned chat or `/api/v1/chat/stream-direct` dispatch is execution-owned: frontend/SSE, Sidecar, push/webhooks, presence and DB abort polling cannot block/cancel ingress; explicit Stop, upstream verdicts/deadlines and runtime/process failure remain termination boundaries. Direct relay work has a 600-second default/900-second hard request deadline and a finite launch-profiled production 429 allowance, so a detached observer can never retain admission indefinitely.
- Sync and async dispatch both fence every discarded transport or slot attempt. Task-backed projections may retract through their attempt-aware event state; OpenAI-compatible direct SSE retries only before the first visible delta and otherwise terminates honestly because that protocol cannot retract bytes.
- Caller deadlines propagate through dispatch rotation; an expired background request cannot fall through to a fresh direct-provider call.
- Dispatch remains indefinitely user-cancellable by default. Optional callers and every configured fallback/rescue may cap upstream 429 attempts; only provider-reaching requests count, exhaustion is typed and terminal, and `smart_chat` cannot bypass the budget through direct fallback. The synchronous language micro-classifier uses one attempt plus a 512-entry, process-keyed opaque-digest LRU; it caches only valid language codes and retains no prompt text.
- Retry/wait telemetry is hard-bounded per LLM round independently of the user-abortable loop; liveness updates are never sampled out.
- Each task-owned model request has one correlated start/complete diagnostic
  span, and every allowed model fallback has an explicit decision event.
- Usage/cache fields survive normalization; Codex cold writes, material warm tails, and per-key routing pressure retain separate controls, so skipping a small-tail hold cannot disable the send interval.
- Retries are bounded, observable, and do not reinterpret programmer errors.
- Locally derived payload pressure is recovered locally: compact first, retry
  the same model, and never treat cgroup headroom as a model-fallback signal.
- Official Model facts and Provider Offering facts remain separate; a missing
  remote directory row marks an Offering stale rather than deleting it.
- One Provider-scoped wire ID names exactly one Deployment; aliases and
  request-ID pools are migration input only.

## Change routing

| Change | Start here | Verify |
|---|---|---|
| Canonical request field | `lib/llm/body/` | all provider translators |
| Responses behavior | `responses_outbound/` | stream/non-stream parity |
| Anthropic behavior | `anthropic_outbound/` | tool/reasoning/usage parity |
| Model/provider/access entity or selection | `contracts/model_routing_v2.schema.json`, `lib/model_routing/` | model-routing contract, migration, API, dispatch, snapshot tests |
| New provider protocol | v2 Connection protocol + focused wire adapter | model-routing contract, translator parity, dispatch |
| Retry/health | `llm_dispatch/dispatcher.py`, health owner | cancellation, exhaustion, metadata |
| Connection/timeout | `lib/llm/_transport.py` | reuse and idle-stream tests |
| Managed local engine/flag policy | `lib/local_serve/_plan.py` (+ sibling stages) | local_serve probe/plan/env/process/tool pins + preset parity |

## Test map

```bash
pytest -q tests/test_llm_transport_connection_reuse.py tests/test_llm_idle_stream_timeout.py tests/test_stream_anomaly_retry_widening.py tests/test_retry_budget_envelope.py \
  tests/test_responses_outbound.py tests/test_responses_websocket.py tests/test_anthropic_outbound.py
pytest -q tests/test_dispatch_stream.py tests/test_dispatch_model_health.py tests/test_provider_pin.py \
  tests/test_model_routing_contract.py tests/test_model_routing_capability_adapter.py tests/test_model_routing_bootstrap.py tests/test_turn_serving_route.py
pytest -q tests/test_local_serve_probe.py tests/test_local_serve_plan.py tests/test_local_serve_process.py tests/test_local_serve_env_store_register.py tests/test_local_serve_tools.py tests/test_local_autodiscover.py
```
