# LLM I/O and dispatch

This domain selects a provider slot, builds one canonical request, normalizes upstream calls, and reports usage/health; model registration lives in [`../MODEL_REGISTRATION.md`](../MODEL_REGISTRATION.md). `lib.llm` and `lib.llm_dispatch` are lazy compatibility facades: focused imports and route registration do not initialize transport, provider discovery, or dispatcher state.

## Ownership

| Concern | Owner |
|---|---|
| Provider-independent chat/stream API | `lib/llm/chat.py`, `astream.py`, `stream.py` |
| Canonical request-body construction | `lib/llm/body/` |
| Shared transport, connection and timeout policy | `lib/llm/_transport.py` |
| Responses / Anthropic translation | `lib/llm/{responses,anthropic}_outbound/` |
| SSE byte framing | `lib/llm/_sse_framer.py` |
| Provider-payload normalization | `lib/llm/_sse_core.py`, provider `_sse.py` modules |
| Provider registry and discovery | `lib/llm_dispatch/provider_registry.py`, `model_entry.py`, `discovery/` |
| Slot selection and affinity | `lib/llm_dispatch/dispatcher.py`, `slot.py`, `conv_affinity.py` |
| Catalog/configuration and remote sync | `lib/model_catalog/`, `lib/llm_dispatch/{config,model_catalog_sync.py}` |
| Retry/health/caching policy | dispatch health modules, `lib/llm/cache.py` |

## Request flow

1. Resolve a canonical model entry and provider face.
2. Bind a managed, subscription, or request-scoped BYO slot.
3. Build the provider-neutral request body once.
4. Translate only at the provider adapter boundary.
5. Execute with bounded connect policy and a 300s default rolling stream-idle
   window. Every received SSE/WS transport event renews it; it is not a total
   request wall clock.
6. Normalize stream events and terminal usage into the internal vocabulary.
7. Record slot health, cost/usage, cache observations, and retry outcome.

Continuation rounds retain the original provider binding unless the retry
policy explicitly declares a failover. Conversation metadata stores canonical
model/provider identity, not a display label.

## Provider adapters

Provider adapters translate wire vocabulary; they do not own task policy,
context compaction, billing, or tool execution. OpenAI-compatible,
Responses-based, Anthropic, subscription, and BYO paths converge before task
code consumes deltas.

A translator preserves text/reasoning/tool work, finish and truncation meaning,
provider cache/reasoning usage, stable tool-call IDs, and typed upstream errors
without leaking credentials.

Unknown provider payloads fail explicitly. Do not add `.get()` chains that turn
a malformed response into an empty successful answer.

## SSE framing and stream-activity watchdog

All byte-stream transports use `lib/llm/_sse_framer.py`. It incrementally
decodes strict UTF-8 and frames SSE events across arbitrary byte boundaries,
including CR/LF/CRLF, comments, a leading BOM, repeated `data:` fields, multiple
events in one read and a split `[DONE]`. One event is capped at 1 MiB. Invalid
UTF-8, invalid JSON, an oversized event, or an unterminated EOF frame closes as
`malformed_stream` with bounded, credential-free diagnostics. The Responses
WebSocket path submits an already-decoded provider payload directly; it never
manufactures a `data:` line.

`TOFU_LLM_IDLE_STREAM_TIMEOUT_S` is one attempt's continuous transport-idle window,
not its maximum wall time: default 300 seconds, `0` disables it, and positive values
below 30 seconds clamp to 30. This matches native Codex's rolling stream behavior;
sync SSE, async SSE, and Responses WebSocket paths share these rules. The pinned
`@openai/codex` `rust-v0.149.1` [provider default](https://github.com/openai/codex/blob/rust-v0.149.1/codex-rs/model-provider-info/src/lib.rs#L26-L27)
is 300,000 ms, and its [Responses SSE loop](https://github.com/openai/codex/blob/rust-v0.149.1/codex-rs/codex-api/src/sse/responses.rs#L552-L575)
wraps each next stream event in that rolling timeout.

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

`llm_dispatch` owns slot eligibility, provider pinning, health, rate/capacity
signals, and bounded retry. A route may request a model or provider but may not
reimplement slot selection. BYO slots are request-scoped and owner-authorized;
managed slots are configured at composition.

Health penalties and retry decisions must be reasoned from typed failures.
Cancellation, user abort, and deterministic request errors are not provider
health failures. Deterministic HTTP 400/404/422 request rejections surface on
the selected model and may not trigger configured fallback or pool-wide rescue.
A retry may not duplicate a completed tool side effect.

A typed shared-project TPM 429 is external contention, not key/model failure;
the first rejection arms a process-local `(provider_id, model)` gate. Every
later sync/async task reserves after local cache gates: starts are one second
apart and deep queues recheck in abortable three-second slices. Slots/fallback
stay eligible; waits are metered, two drained successes clear, and quiet entries expire from a 256-family table.

Retry execution and observability have separate budgets. Dispatch may rotate
through transient 429/cooldown states until recovery or abort; each LLM round
persists power-of-two samples for at most eight coarse signatures (16 each,
128 frames total). Suppressed samples still refresh the non-durable heartbeat,
keeping the HUD truthful without unbounded `storage_events` or
`storage_attempt_events` growth.

An all-slots-cooling callback carries a typed current-wait status; pool polling
is not an upstream request attempt and never increments a retry count. A real
429 or failed provider attempt may emit `retrying` with its actual attempt
number. Current-attempt waiting uses `waiting_model`; a stream that has emitted
model work and is currently transport-idle uses `stream_stalled`.

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

## Paired Kimi benchmark paths

`evaluations/codex_kimi_proxy/` is a benchmark-only, one-request/one-Kimi-call
Responses adapter, not a provider. It pins the Codex binary, keeps compaction
client-local, normalizes tools, rejects unknown native types, and records raw
wall, total proxy CPU, and pure translation CPU separately; compact requests
invalidate a trial. The formal launcher strips Kimi secrets from Harbor,
restricts the guest to a same-UID private relay, re-verifies the binary, binds
provider/binary identity, and re-projects raw JSONL/metrics instead of trusting
non-empty output. Immutable task claims and identical release locks govern
resume/export; outer failures retain usage, artifacts, wall time, and terminals.

The paired `tofu-kimi` profile instead uses public production `AgentRuntime`.
Secrets stay host-side; the guest exposes only run/submit tools while Tofu owns
dispatch, context, compaction, settlement, and orchestration. Native events,
sanitized evidence, raw/visible tool audit, and ATIF-v1.7 must reconcile without
prompt/runtime/schema drift, call/usage mismatch, fallback, secret persistence,
missing compaction evidence, or an unverified final claim. Candidate wall time
is never proxy-adjusted, and failed outer attempts remain in the same ledger.

## Invariants

- One canonical body builder and one transport policy.
- Provider differences stop at focused translation modules.
- Streaming and non-streaming paths project the same terminal meaning.
- Provider/model identity is canonical and survives every round.
- Secrets stay in outbound headers and redacted diagnostics.
- Every network loop is cancellable/bounded; local health/discovery share one monitor with empty-result backoff.
- Caller deadlines propagate through dispatch rotation; an expired background
  request cannot fall through to a fresh direct-provider call.
- Retry/wait phase telemetry is hard-bounded per LLM round independently of
  the user-abortable retry loop; liveness updates are never sampled out.
- Each task-owned model request has one correlated start/complete diagnostic
  span, and every allowed model fallback has an explicit decision event.
- Usage and cache fields are preserved through normalization.
- Retries are bounded, observable, and do not reinterpret programmer errors.
- Locally derived payload pressure is recovered locally: compact first, retry
  the same model, and never treat cgroup headroom as a model-fallback signal.
- Registered entries own capability/pricing; remote `/models` sync has a
  persisted 6h floor, 12h unchanged and 48h failure ceilings, plus forced Save.
- Benchmark translation cannot introduce an extra model call or hide a native
  tool/compaction mismatch.

## Change routing

| Change | Start here | Verify |
|---|---|---|
| Canonical request field | `lib/llm/body/` | all provider translators |
| Responses behavior | `responses_outbound/` | stream/non-stream parity |
| Anthropic behavior | `anthropic_outbound/` | tool/reasoning/usage parity |
| New provider | provider registry + focused adapter | registration contract, discovery, dispatch |
| Catalog shape / projection | `lib/model_catalog/`, `routes/api_v1/model_catalog.py` | model-catalog contract + config/capabilities tests |
| Retry/health | `llm_dispatch/dispatcher.py`, health owner | cancellation, exhaustion, metadata |
| Connection/timeout | `lib/llm/_transport.py` | reuse and idle-stream tests |
| Paired Codex benchmark adapter | `evaluations/codex_kimi_proxy/` | real CLI command, stream translation, single-upstream-call metrics |
| Paired production-Tofu candidate | `evaluations/swebench/tofu_kimi_runtime.py`, `evaluations/long_agent_release/tofu_projection.py` | native/runtime/tool/ATIF reconciliation and release export |

## Test map

```bash
pytest -q tests/test_llm_transport_connection_reuse.py tests/test_llm_idle_stream_timeout.py
pytest -q tests/test_stream_anomaly_retry_widening.py tests/test_retry_budget_envelope.py
pytest -q tests/test_responses_outbound.py tests/test_responses_websocket.py
pytest -q tests/test_anthropic_outbound.py
pytest -q tests/test_dispatch_stream.py tests/test_dispatch_model_health.py tests/test_provider_pin.py
pytest -q tests/test_model_registration_contract.py tests/test_model_entry_contract.py
pytest -q tests/test_codex_kimi_proxy_cli_contract.py tests/test_codex_kimi_formal_runtime.py tests/test_harbor_codex_kimi_agent.py tests/test_codex_long_agent_projection.py
pytest -q tests/test_tofu_kimi_formal_runtime.py tests/test_harbor_tofu_runtime_agent.py tests/test_tofu_long_agent_projection.py tests/test_harbor_tofu_release_export.py && pytest -q tests/test_long_agent_v2_contracts.py -k codex_proxy
```
