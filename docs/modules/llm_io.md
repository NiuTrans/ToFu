# LLM I/O and dispatch

This domain compiles owner-authorized v2 routes, emits canonical requests, and normalizes upstream results. Entity/selection rules live in [`../MODEL_REGISTRATION.md`](../MODEL_REGISTRATION.md); `lib.llm` and `lib.llm_dispatch` are lazy execution facades, not configuration authorities. Provider terminal tool-text classification lives in `lib/llm/terminal_tool_text.py`; the standalone harness reuses it, while task recovery owns retries and never imports the CLI runtime or executes text packets. Historical reasoning continuity: Problem: body cleanup lost the settled-Turn label. Decision: retain it through protocol conversion; live/unproven thinking stays mandatory. Alternative rejected: dropping every signature loses live continuity. `test_protocol_conversion_checked.py` verifies the real builder/adapter boundary and caller immutability.

## Ownership

Problem: one round-fallback function mixed request rebuilding, transport,
billing, recovery and settlement; repeated charge loops could diverge, and a
bare re-raise after compaction could replace the latest typed rejection with
the earlier overflow. Decision: keep `_call._llm_call_with_fallback` as the
single coordinator and give these phases concrete owners in
`lib/tasks_pkg/llm_fallback/` (see its `AGENTS.md`). `_usage` records adopted and
discarded responses for every round path, including post-loop synthesis;
scope-denied charges deliberately publish no serving evidence. Recovery returns
the latest error, and strict primary policy raises that same error object.
Alternatives rejected: a second invocation state machine would duplicate policy;
forwarding private patch names through `_call` would hide ownership; source-text
billing checks can pass without exercising accounting. Behavioral tests cover
all billing paths, failure identity, refusal rollback and delivery cancellation.
Model selection, callbacks, early fallback receipts, retry budgets and typed
settlement remain owned by their existing contracts.

Input-scope denial is terminal on every attempt, including reactive, configured
and pool recovery. `_errors.raise_if_scope_denied` bills known discarded work
without publishing context, preserves the original exception and prevents
further routing. A configured attempt clears its decision-time fallback stamp
when scope is lost anywhere in that chain. Tests inject the same denial at
each phase; source co-occurrence checks could not expose the previous gaps.

Package import stability: Problem: importing the `lib.llm.chat` child before
accessing the lazy package export replaced the callable with a module, breaking
nonstream dispatch. Decision: explicitly re-export the lightweight `chat`
definition; it imports provider and HTTP dependencies at invocation. The public
signature, decorators, child import path and package monkeypatch seam stay intact.
Other package exports remain lazy. The existing module HTTP fault-injection seam
delegates lazily to the shared client; retry/model metadata fixtures use their
owning modules. Alternatives rejected: eager HTTP initialization
taxes startup; module-class interception hides ordinary import behavior; changing
individual callers leaves the public entry point broken. Import-order, cold-start,
real racing-dispatch and nonstream accounting tests cover this boundary.

Fallback continuity attribution: Problem: a destination protocol refusing native history hid the primary availability failure behind a non-retryable internal error. Decision: retain the primary error, model and reasoning policy when the typed `mandatory_continuity_unrepresentable` check blocks fallback before ingress; record `fallbackDetail`, `fallbackFailureStage` and `fallbackProviderReached` separately. Keep automatic recovery exhausted and preserve manual retry semantics. Alternative rejected: dropping native history or retrying arbitrary pool models would erase continuity or repeat an incompatible request. Other conversion defects remain attributed to the fallback. `test_fallback_continuity_attribution.py` exercises the real body builder/converter and unchanged tool history. Inside one non-strict pick (pool rescue) the same typed refusal excludes that route, meaning every credential that sends its wire model to the same provider endpoint, for the rest of the call without a hard attempt or health evidence, and the pick continues among the other eligible routes. A strict pick, or a call in which no remaining route can carry the request, surfaces the original `ProtocolConversionError`. Problem: pool rescue is non-strict, yet the first score-ranked candidate that could not carry the request's signed reasoning ended the whole rescue (2026-11-04), so compatible candidates were never tried. Spec: `test_dispatch_endpoint_outage.py::TestUnrepresentableRoutes`.

Refusal evidence: Anthropic `stop_reason: refusal` maps to `content_filter`, and both the streaming translator (`message_delta.delta.stop_details`) and the nonstreaming converter keep its bounded `category`/`explanation` as `usage['_model_refusal']` (`model_refusal_details` in `_from_anthropic.py`). Null fields are a normal permanent value and malformed details never fail the response. The explanation is display text only. The name differs from `provider_refused_request`, which marks a capacity rejection that consumed no tokens. Retry policy belongs to the task fallback layer, not transport; see [TURN_SETTLEMENT.md](../contracts/TURN_SETTLEMENT.md#content-filter-outcomes). Specs: `tests/llm/test_anthropic_outbound.py`, `tests/llm/test_stream_protocol_tolerance.py`.

| Concern | Owner |
|---|---|
| Provider-independent chat/stream API | `lib/llm/chat.py`, `astream.py`, `stream.py`; sync non-200 response reading and typed classification: `stream.py::_classify_stream_error_response` |
| Canonical request-body construction | `lib/llm/body/` |
| Shared transport, connection and timeout policy | `lib/llm/_transport.py` |
| Responses / Anthropic translation | `lib/llm/{responses,anthropic}_outbound/` |
| SSE byte framing | `lib/llm/_sse_framer.py`; sync/async code fidelity: `tests/llm/test_stream_code_fidelity.py` (controlled frames, no upstream guarantee) |
| Explicit pre-parse payload capture | `lib/llm/payload_capture.py`; context-local memory bounds; [operations](../LOGGING.md); `test_scoped_payload_capture.py` |
| Shared byte-stream terminal/EOF lifecycle | `lib/llm/_sse_stream.py` |
| Provider-payload normalization | `lib/llm/_sse_core.py`, provider `_sse.py` modules |
| Model/provider/access authority, migration, candidate compilation, scoped health, snapshots | `lib/model_routing/` |
| Build-time admitted local artifact metadata (no runtime enablement) | `lib/model_info/local_artifacts.py`, `scripts/gen_packaged_local_models.py`; contract: [local artifact publication](../MODEL_REGISTRATION.md#local-model-artifact-publication) |
| Request-owned chat/non-chat slot execution and affinity | `lib/model_routing/{dispatch_adapter,capability_adapter}.py`, `lib/llm_dispatch/{dispatcher,slot,conv_affinity}.py` |
| Cross-surface execution lifecycle, direct relay, bounded non-task model services | `lib/agent_core/{execution_session,direct_stream}.py`, `lib/logging_pkg/log_compression.py`, `lib/model_routing/embedding_execution.py` |
| Retry/caching and transport-attempt policy | dispatch health modules, `lib/llm/cache.py` |
| Dispatch operation surface (chat/stream/multi-key/budget/contention/hygiene) | `lib/llm_dispatch/api.py` (re-export facade) + `_api_{chat,stream,stream_state,retry_state,failures,multi,budget,contention,hygiene,errors}.py` shards |
| Semantic LLM port for non-route consumers | `lib/protocols.py` (`LLMService`, `resolve_llm_service`), `lib/llm_dispatch/service.py` (`DispatchLLMService`) |
| Dispatcher lifecycle after authority mutation | Public `lib.llm_dispatch.reset_dispatcher` / `get_dispatcher`. In-package health/autodiscover may rebuild slots in place. HTTP, local_serve, and template supply must not import `health_local._rebuild_dispatcher_slots`. |
| Managed local deployment (model path → running local provider) | `lib/local_serve/` (+ agent surface `lib/tasks_pkg/handlers/local_serve.py`, `lib/local_serve/tool_defs.py`) |

Managed local deployment ownership lives in `lib/local_serve/_store.py`. The durable ledger has no instance-count eviction: running, stopped and failed records remain until explicit removal. Field patches read/merge inside the atomic file lock; invalid existing JSON or ledger shape refuses mutation and preserves the file. Model registration, owner checks, port/process ownership and joined shutdown still use their existing authorities.

## Request-local candidate compilation

`routing.py` preserves public imports. `selection_resolution.py` owns authority
indexes and selector identity, `candidate_compiler.py` owns grants/eligibility and
ranking, and `route_snapshot.py` owns bounded diagnostics. Their dependency
direction is resolution → candidates → snapshots. Diagnostic formatting does not
select a route or manufacture an attempt. This replaces a mixed routing module;
duplicating selection rules in snapshots or retaining implementation in the
re-export facade would obscure these boundaries.

Problem: one route group separately normalized and indexed the same authority
for the requested model and its fallback candidates. The repeated validation and
copying grew with the complete catalog even when the caller requested a short
candidate prefix. Decision: one `RouteCandidateCompiler` owns both selections
from the same owner/revision snapshot. Selection finishes before plaintext
credential materialization; only chosen witnesses cross into slot creation.
Each new request reads and validates again, so revocation, owner and revision
changes cannot reuse a prior compiler. Standalone compilation helpers retain
their public signatures and independent snapshots.

Primary candidates still precede compatible fallback models; preferred-provider,
quality, health and stable tie ordering are unchanged. Explicit provider/offering
and issued-route selections retain their existing refusal/fallback boundaries.
An explicit candidate prefix remains a caller hint; the default retains every
eligible candidate. Alternatives rejected: a global normalized catalog cache
requires new invalidation/ownership machinery; removing validation weakens the
admission boundary; imposing a default shortlist silently drops authorized work.
Verify `test_request_candidate_compilation`, routing contracts, credential
materialization, native issued routes and dispatch pin/health/attempt tests.

`benchmarks/model_routing_request.py` compares fresh alternating source roots
using one authority read, real candidate compilation and ephemeral Slot
registration/disposal. Ordered route receipts, candidate counts, secret reads
and empty registries gate timing/allocation observations. Its in-memory source
and audit sink exclude storage, provider IO, inference and chat latency; see
`test_model_routing_request_measure` for complete-work refusal checks.

Synthetic 2026-10-28 measurement used five alternating fresh-process pairs,
21 timed complete requests and three separate allocation samples per case.
Median request preparation/disposal times were 2.024→1.610 ms (2 models,
4 candidates), 33.975→29.431 ms (32 models, 128 candidates), and
47.570→30.042 ms (128 models, explicit 8-candidate prefix). A 128-model request
selecting one primary stayed within noise at 22.519→22.570 ms. Corresponding
traced peaks were 65,589→59,005, 995,885→958,397, 1,089,309→983,565, and unchanged
979,861 bytes. All paired route receipts and operation counts matched. These
are component observations on a shared host, not provider or chat speed claims.

## Successful-response parameter receipts

Complete-text subscription calls: Problem: background deliverables used
`stream=false` against Codex Responses subscriptions and exhausted model
fallbacks with deterministic HTTP 400s. Decision: `chat` collects the existing
stream transport on that exact selected subscription route, then requires
verified provider completion before projecting text, tools, reasoning and
usage. Streaming retains OAuth ownership, budget admission and cancellation;
an explicit transport retry count preserves dispatch's single-attempt boundary.
Known Codex Responses slots remain eligible for complete-text dispatch.
Alternatives rejected: switching presentation models hides the transport
defect; accepting partial stream text silently publishes incomplete content;
adding a recipe-specific model loop duplicates dispatch. Verify
`test_subscription_chat_completion.py`, nonstream budget/receipt tests and
stream terminal/retry/cancellation tests.

Problem: treating sealed request bytes as a mapping raises after a successful provider response and can amplify a local error into repeated translation fallback attempts. Decision: both streaming and nonstreaming receipt readers consume a mapping; nonstreaming passes `AdmittedRequest`, never its `payload`. Scalar controls are read without decoding or copying prompt/image content. Alternative rejected: decoding the entire wire payload for a few receipt fields adds avoidable work proportional to input size. Transport still sends the sealed bytes. `tests/llm/test_nonstream_request_receipt.py` covers successful OpenAI, Anthropic and Responses replies and prompt-independent receipt extraction.

## Checked outbound conversion

### Common request preparation and replay boundaries

**Problem:** content-only merges could discard the second assistant's reasoning
or bind new prose to the first assistant's opaque replay state. Chat Completions
also had separate final projections: non-stream calls could leak native history
carriers or send text-part lists that the stream path already normalized.
**Decision:** the common sanitizer keeps assistant messages carrying reasoning,
signatures, native items or continuity markers atomic, including empty replay
fields. Different named participants also remain distinct. Plain adjacent text
keeps the existing merge behavior. `lib/llm/openai_outbound.py` owns final Chat
Completions projection for sync streams, async streams and non-stream calls. It
removes private request/message envelope fields, retains public vendor reasoning
extensions and nested user/tool JSON, and applies text-list normalization using
the logical model identity. Messages and Responses retain their native adapters.
Chat uses the same mandatory-continuity checks before discarding private carriers:
compaction/program state and unrepresentable active thinking cannot silently
vanish. Signed native thinking must match the complete public signed sequence,
including duplicate occurrences; optional foreign ciphertext omissions are
counted without removing portable text, reasoning or tools.
Alternatives rejected: merging reasoning strings cannot preserve signatures or
native item ordering; maintaining separate entry-point fixes allows them to drift.

**Problem:** generic exception wrappers hid local context overflow from reactive
compaction; non-stream construction failures could escape untyped and trigger
provider retries. **Decision:** `request.py::request_preparation` is the shared
unsent-failure boundary, including selected-slot body adaptation. It preserves
typed local failures, context overflow, image errors and cancellation. Unexpected
exceptions retain their cause but expose a fixed message, without request data.
Local hard-window refusals carry `origin=local`, `provider_reached=false` and
token/window evidence. Slot adaptation releases exactly once even on cancellation.
A host refusal of the header-wait watchdog or POST thread raises the same
typed failure with `stage='thread_start'`; only its envelope is retryable,
because the refusal is transient (`docs/RESOURCE_CONTROL.md`).
JSON/UTF-8 encoding rejects invalid values with a fixed local error before send;
unknown wire protocols and Responses dialect profiles fail locally instead of
silently selecting another format. Missing/empty wire protocol retains the
documented Chat default. Alternative rejected: broad retries cannot repair a
deterministic local request and would distort provider health or usage accounting.
Provider refusals and learned restrict-only repair retain their existing owners.
Learned signature withdrawal records digests from native blocks, flat signatures
and signed `reasoning_details`, including redacted state. On a later destination
requiring unsigned reasoning replay, it removes the rejected signatures while
retaining the original plain reasoning. This decision uses the selected logical
model even behind an opaque wire alias; Claude's unsigned-thinking omission and
the rejecting request's one-shot repair remain unchanged.

Specs: `tests/llm/test_request_robustness.py` exercises real transport preparation
and dispatch across the three protocols and entry points; neighboring signed
thinking, native replay, request-repair, budget and cancellation suites retain
their wire and settlement contracts.

Replay requirements are grounded in the official
[Responses reasoning guide](https://developers.openai.com/api/docs/guides/reasoning),
[Claude thinking/tool workflow](https://platform.claude.com/docs/en/build-with-claude/thinking-tool-workflows)
and [DeepSeek thinking-mode contract](https://api-docs.deepseek.com/guides/thinking_mode/).

Structured text normalization: `lib/llm_sanitize/text_blocks.py::normalize_text_blocks` is the pure authority used by the common message sanitizer and the Anthropic canonical-content converter, including direct calls that bypass `build_body`. Problem: removing whitespace-only blocks turned `['A', ' ', 'B']` into `AB` and destroyed chunked code indentation. Decision: preserve exact separator bytes inside contiguous runs of plain `{type, text}` string blocks, folding left when possible and otherwise right (including run-edge whitespace); keep nonblank blocks separate. Empty strings and wholly blank runs still disappear, with existing placeholder/omit-key healing afterward. No bytes are injected and nonblank single strings are untouched. Metadata-bearing blocks (even equal metadata), cache controls, signatures and multimodal/unknown blocks are hard boundaries: no text moves across them, blank metadata-bearing blocks are removed rather than merged. The normalizer is idempotent, copies only changed blocks, and does not mutate caller lists/dicts. Native opaque replay and existing adapter metadata projection/marker hoisting retain their contracts. Alternatives rejected: unconditional dropping loses meaning; merging all text would collapse cache and opaque-state boundaries. Synthetic byte/wire specifications: `tests/llm_sanitize/test_structured_text_whitespace.py`.

`lib/llm/conversion.py` owns `ConversionResult`: payload (absent on failure), `validation_issues` (one fixed code, fail-fast), and `permitted_conversion_losses` (fixed categories with saturated counts). Both outbound facades expose `openai_body_to_*_checked`; legacy converters keep their return shapes but require a successful result. Diagnostics contain no caller content; production and diagnostic requests share `lib.llm.request.strip_internal_request_fields` to remove private `_conversion_report` metadata while preserving nested caller content. Codex probes reuse `lib.oauth.outbound.resolve_oauth_request` for credentials and identity headers. Tool argument validation — Problem: checked adapters decoded standard JSON strings then re-encoded the full object solely for validation. Decision: the standard decoder records nonfinite floats during parsing and returns valid object roots without a second encoding pass. Python dictionary carriers and overridden decoders retain encoder validation; callback recursion retries the original decode/encode boundary. Root-shape errors retain priority over numeric overflow, and diagnostics remain fixed codes. Alternatives: bounded tree scanning duplicated traversal and showed small-input regressions; changing JSON libraries would expand numeric and custom-carrier compatibility scope. Verify with `tests/llm/test_tool_argument_validation_cost.py` and the protocol-conversion/outbound suites. Paired evidence is scoped to checked Anthropic/Responses conversion, excluding token admission, durable history, HTTP and inference; see `benchmarks/context_compiler/tool_argument_measure.py`. Native argument kernel experiment — Problem: numeric-heavy standard validation spends time in per-float callbacks. Decision: keep `benchmarks/context_compiler/native_json/` explicitly experimental until complete conversion, first-activation, resource and failure gates pass; no production backend is selected. The source-token guard preserves overflow discarded by duplicate keys, while canonical hooks, integer limits and expensive Unicode paths remain authoritative. Alternatives rejected: graph-only finite checks miss overwritten values; recursive union validation regresses; direct orjson decoding loses large-integer precision. Measured conversion failures and reuse/cold failures remain failures. The single-scan complete warm cohort removes duplicate eligibility scans but still fails seven tail guards; constructor cold costs remain separate. The deferred-state cohort still fails twelve complete warm guards and small-input cold tails; bounded retention evidence does not authorize a default. Native gains cannot waive other metrics or whole-context ownership.

Malformed/missing/non-object execution arguments, unknown canonical roles/content/tools, and unrepresentable executable or mandatory replay state fail closed. Long Responses tool names use collision-checked digest aliases and a request-local reverse map. Unsigned reasoning, reconciliation-blanked orphan programs and non-executable metadata may be omitted with counted losses. Native Anthropic blocks are authoritative, including future native blocks; empty text retains its placeholder policy. Conversions isolate caller-owned nested data. Completed Turn history carries an explicit reasoning-continuity classification from the settled Turn authority. Cross-protocol conversion may omit its Anthropic thinking signatures and thinking/redacted-thinking blocks with counted losses; visible text and executable tool state remain required. Problem: historical signatures blocked a new Codex turn after a Messages model completed. Decision: distinguish settled historical thinking from active continuation. Alternatives: erasing all signatures would weaken interrupted-round recovery; refusing all historical signatures would prevent switching protocols. Live, interrupted, unknown-status and unclassified Anthropic thinking retains mandatory continuity checks, and same-protocol replay keeps the original signatures. **Responses → Messages reasoning:** **Problem:** encrypted Responses reasoning was treated as mandatory even when the destination spoke another provider's Messages protocol, blocking otherwise representable text/function-call history. **Decision:** Messages conversion omits that provider-private reasoning with a bounded `cross_protocol_reasoning_omitted` count per encrypted item occurrence; it never decrypts it or fabricates a signed thinking block. Missing, null and empty-string ciphertext retain the metadata-only omission rule; other non-string values fail validation. Canonical visible text, function calls/arguments/IDs and tool results retain their existing projection and ordering. Caller-owned history retains the original ciphertext for a later Responses request. Same-protocol Responses replay and explicit feature denials are unchanged; opaque compaction, programs, hosted/unknown calls and their outputs still fail closed without a destination binding. **Alternatives rejected:** forwarding foreign ciphertext as Messages thinking is invalid; rejecting private reasoning prevents model switching; dropping every native item erases executable or compressed context. `test_cross_protocol_reasoning.py` covers real Responses ingress, checked conversion, final streaming/nonstreaming wires and unchanged caller history; the fallback attribution suite verifies the existing fallback succeeds without a new retry policy.

Explicit serial-tool requirements and strict function schemas survive both adapters. Authored empty schemas remain empty; malformed schemas are never replaced with unconstrained input. Required-tool aliases map to each native protocol. Native visible text survives cross-protocol projection; unknown native blocks require an explicit binding, not an optional-metadata omission. Anthropic maps canonical JSON Schema output to `output_config.format`; unsupported formats or conflicting native settings fail rather than become free text. Native replay carriers require the assistant role. Responses preserves native reasoning options, rejecting conflicting effort/context/mode and unrepresentable native output settings.

`lib/llm/route_features.py` binds selected `_route_inference_profile.features` to installed wire extensions; missing declarations do not enable features on generic/keyed Responses routes, and explicit unknown/unsupported always narrows support. `_inference_profile` is a rollout alias; `responses.feature_profile` names a dialect, not permission for all extensions. Model-family inference applies only without a selected route profile.

`chat_cache_breakpoints` is an OpenAI wire feature: explicit `unsupported`
skips `cache_control` creation and collapses text-only content parts to strings
at the shared stream/non-stream Chat boundary. A missing declaration, or any
other wire, retains the model-family cache policy; no new configuration knob
exists. The gate reads the declaration itself, because the closed-world
`route_feature` answers False for both cases ([LLM cost optimization
§10](../LLM_COST_OPTIMIZATION.md#10-cache-breakpoints-on-profiled-routes)). The observed
Baidu DeepSeek V4 Pro wire rejects its content-part variants but accepts the
same prompt with string content, so this declaration is Deployment-scoped.

`lib/llm/cache_affinity.py` owns prompt-cache affinity. It derives one stable, non-identifying key per request lineage (a child invocation, else the conversation, else the task). Every carrier sends that key: Responses `prompt_cache_key`, Codex session headers, and the gateway headers a route declares through its `cache_affinity_*` features. The YourProvider faces declare `Mt-Session-Id`. Both outbound header assemblies add declared carriers, and an owner-configured header of the same name wins. The decision record is [LLM cost optimization §7](../LLM_COST_OPTIMIZATION.md#7-cache-affinity-carriers); specs are in `tests/llm/test_cache_affinity.py`.

Codex subscription is an installed Responses dialect: its stateless `store=false` tool loop requires encrypted-reasoning replay and supports prompt-cache affinity even if the catalog profile declares only reasoning/sampling. Problem: treating partial catalog metadata as a complete feature denial succeeds for one round, then fails local request preparation on opaque reasoning. Decision: bind these stateless Codex capabilities at the dialect boundary; explicit denial still wins. Dropping mandatory items would corrupt continuation, while enabling replay for all Responses providers would send unsupported wire items upstream.

**Codex CLI wire parity.** Problem: the subscription transport predated several first-party Codex CLI 0.165 contracts: HTTP-only transport, uncompressed bodies, no per-turn sticky routing, and a legacy `OpenAI-Beta: responses=experimental` header. On a live Pro account, through Tofu's own request path with a ~60K-token context, a continuation round took a median 3.65 s over SSE and 1.27 s over the WebSocket chain, with identical cache-hit rates. Decision: follow `openai/codex` `core/src/client.rs` exactly where it touches the wire:
- Task rounds use the Responses WebSocket with exact continuation (see [OpenAI Responses features](../contracts/OPENAI_RESPONSES_FEATURES.md#websocket-mode)).
- HTTP bodies use zstd level 3 with `Content-Encoding: zstd` (`lib/llm/request_encoding.py`; identity when no codec is installed).
- `lib/oauth/codex_turn_state.py` keeps the server's `x-codex-turn-state` for one task turn and replays it as a header (HTTP) or in `client_metadata` (WebSocket), never across turns.
- Requests send `Accept: text/event-stream`, `version`, and `x-codex-routing-hint: model=<slug>`, and drop the legacy beta header and the internal `M-TraceId`.

The empty `instructions` field stays, because the native runtime's generated execution contract shares it and the backend accepts it. Alternatives rejected:
- Making the WebSocket opt-in would keep the slower path as the default.
- Re-deriving continuation from Tofu's history alone would re-send state the server already holds.
- Reusing sockets across tasks needs an idle-socket retention policy; a task's first round pays one connect, about 1 s through the deployment proxy.

Specs: `tests/llm/test_responses_websocket.py`, `tests/llm/test_codex_wire_parity.py`, `tests/oauth/test_oauth_outbound.py`.

### Stateless Responses replay integrity

**Problem:** encrypted-only assistants were mistaken for empty placeholders,
segment-driven cold reconstruction omitted their native items, and grouping all
opaque items before prose/functions changed cross-type output order.
**Decision:** the common body ghost predicate and trailing-history predicate
retain native sidecars; segment reconstruction takes them only from the
occurrence-owning rehydrated round. Equal items, recycled IDs and producing
Attempt boundaries remain distinct. Direct rounds and thin/rehydrated segments
must produce the same canonical history; no fallback reader substitutes for
missing state.

When native output is not representable by the ordinary opaque → prose → calls
projection, `lib/llm/responses_replay.py` appends a derived, versioned order
witness inside the existing private `_responses_items` carrier. It contains
bounded occurrence references and digests of the native items, ordered
references and canonical prose/calls, not a second copy of executable arguments.
References select current validated calls; changing/removing a call or mandatory
prose invalidates the witness rather than resurrecting captured data. The
witness binds the effective text authority, including a native text override;
optional phase metadata falls back to changed live text instead of restoring
redacted prose. Only unchanged native message text may replay. The marker is
neither provider input, permission, nor a cryptographic portability proof.
Legacy unwitnessed histories keep their existing projection; no order is invented
retrospectively.

### Model-specific thinking replay on Messages routes

**Problem:** treating the Anthropic Messages envelope as proof of Claude's
signature requirement discarded DeepSeek's unsigned `reasoning_content`, even
though ingress and tool history retained it. A later tool round then failed
upstream with HTTP 400. **Decision:** the outbound Messages adapter uses the
shared `model_requires_reasoning_content_replay` capability for the logical
model, carried through body construction even when the deployment name is
opaque. Required unsigned text becomes a `thinking` block, unchanged and before
text/tool-use blocks; it is not reported as an optional conversion loss.
This applies to plain assistant turns as well as tool turns, streaming and
nonstreaming sends, and cold history reconstruction. Continue's
`message_builder/_tool_history.py` uses the same replay capability instead of
discarding unsigned checkpoint reasoning. `conv_message_builder/_transform.py`
retains the stored terminal thinking separately from each tool round when the
destination requires it, so a cold rebuild cannot lose the final answer's
reasoning. Empty/error-row omission and existing checkpoint validation remain.
Native signed blocks
remain authoritative and retain their existing validation and order.
See DeepSeek's [thinking replay contract](https://api-docs.deepseek.com/guides/thinking_mode/)
and [Messages compatibility](https://api-docs.deepseek.com/guides/anthropic_api/).

**Alternatives rejected:** forwarding unsigned thinking to every Messages
provider breaks Claude's signature rule; disabling thinking or switching models
conceals a local conversion loss; placeholder text/signatures do not restore
provider state. Existing unsigned Claude/unknown-dialect omissions remain.
The adapter neither invents missing historical reasoning nor rewrites durable
history. Specification: `tests/llm/test_deepseek_messages_reasoning_replay.py`.

### Anthropic signed thinking replay

**Problem:** Opus 4.7+, Opus 5/5.5, Fable/Mythos 5.x, Sonnet 5/5.5 and Haiku
5.5 return thinking with `display: "omitted"` by default: each block has an
empty `thinking` field and its `signature` carries the encrypted reasoning;
Opus 5.5 also returns its notes between tool calls as further thinking blocks.
The flat `reasoning_content` + `thinking_signature` projection required text,
so every signature-only block was dropped on the next tool round, and several
blocks were concatenated into one forged signature. The API accepted the
requests, so the model silently lost its prior reasoning on every round
(400/400 live Opus 5.5 rounds logged `thinking=0chars`).
**Decision:** a signed `thinking` block is replay state. The stream and
non-stream translators keep the ordered native content array in the existing
`_anthropic_content_blocks` sidecar, and the Anthropic adapter replays it
exactly as returned ([preserved thinking](https://platform.claude.com/docs/en/build-with-claude/preserved-thinking)):
every block, in order, with each JSON object's keys sorted. Live blocks keep
the stream's key order while TofuDB returns stored objects key-sorted, so
verbatim replay changed every native turn's bytes at the next turn boundary
and re-billed the cached history (detector signature: first-round
`WIRE PREFIX CHANGED` with `<bytes>assistant{content}` culprits).
Unsigned gateway reasoning still uses the flat projection; build_body strips
it for Claude and warns only when the newest assistant turn was unsigned
(fresh evidence), logging the per-round re-strip of history at DEBUG. Cross-protocol
replay keeps the existing rule: a settled Turn, rebuilt or restored from a
checkpoint ([settled reasoning on restore](../CONTEXT_COMPACTION.md)), may omit
historical thinking, while active signed thinking stays mandatory; native `text`/`tool_use` are
represented by the canonical projection. Live probe on 2026-10-23
(`your-llm-gateway.example.com/v1/anthropic`, `kunlunbedrock.claude-opus-5.5-m17`): the
gateway returns signature-only blocks by default, readable text with
`display: "summarized"`, accepts exact signature-only replay, and ignores the
`thinking-binding-controls-2026-08-01` beta (no `input_transformations`).
The sidecar is the authority for the provider's own blocks only: the
`execute_tools` gateway expands a model call into local sibling `gw_*`
tool_calls that never appear in the returned native array. Verbatim replay
therefore dropped those `tool_use` blocks while their `tool_result` messages
survived, and a strict Anthropic-shape provider rejected the entire request
(production 2026-11-01, conv mvupi0cariefzy task 42fdc410: Claude→Kimi
fallback died on `invalid_request_error: tool_call_id  is not found`). The
adapter appends exactly the canonical tool_calls the sidecar does not already
carry, after the provider's own blocks
(`tests/llm/test_anthropic_native_tooluse_pairing.py`).
**Alternatives rejected:** synthesizing placeholder text for empty blocks
(the API ignores the field but the forged projection still loses block order
and per-block signatures); widening `reasoning_content` to a list (every
consumer of the flat field would need a second representation).
Specification: `tests/llm/test_anthropic_signed_thinking_replay.py`.

The witness uses the existing 4,096-position Responses vocabulary and the
256 KiB phase-text boundary. Unrepresentable mandatory interleaving preserves
original values and fails local preparation with a fixed code; it is never
truncated into an apparently valid request. Optional phase-only replay retains
its existing overflow behavior. There is no new cache, worker or storage owner.
A synthetic alternating reasoning/call sequence retains a 352-byte UTF-8 marker
at four output items and 55,432 bytes at the 4,096-item boundary; this measures
logical serialized growth, not RSS, provider latency or throughput. Native prose
retained for required interleaving stays within the existing text boundary.
**Alternatives rejected:** ID/hash deduplication collapses legitimate
occurrences; blindly replaying saved function shells bypasses current canonical
validation; dropping all encrypted state hides continuity loss. These controls
assume stateless native ordering is semantically meaningful; remove the derived
witness only when a replacement canonical representation preserves that ordering
through ingress, hooks, cold reconstruction and egress. Tests:
`tests/llm/test_responses_replay_order.py`, `test_responses_message_phase.py`,
`tests/tasks_pkg/test_continue_lossless.py` and the empty-assistant specifications.

### Invalid encrypted state and deterministic retry fencing

**Problem:** `invalid_encrypted_content` was treated as an ordinary 400, while
capacity cycling could reopen deterministic rejected pairs. Opaque provider
messages could also obscure the structured code behind clipping/token-limit
heuristics. **Decision:** exact structured `error.code` selects
`InvalidEncryptedContentError(RequestScopedError)` before text/status heuristics
across HTTP, nonstreaming, SSE and WebSocket. The existing public `bad_request`
envelope remains nonretryable. Retain fixed code, a bounded structural input
path label and actual failure stage; unrecognized/arbitrary `param` strings are
omitted rather than clipped. This prevents opaque echoes and misleading
parameter text from reaching diagnostics or token-limit learning/retry
heuristics. Do not attach ciphertext/provider-message bodies to that exception.
The exception stops the dispatch and task fallback with one neutral reservation
release, unchanged history and completed-tool evidence, and no
model/key health penalty. Unknown usage retains the existing accounting fence.

Ordinary payload `BadRequestError` releases its reservation without a key-health
penalty and durably excludes the rejecting provider endpoint plus wire model
for the current dispatch. Replaying an unchanged body across credentials on
that route cannot repair a deterministic shape rejection and turned one
optional-translation 400 into a credential-wide log storm. The existing
next-route or next-model selection may serve un-pinned work; a strict pin or
exhausted candidate set surfaces the typed error. Another endpoint serving
the same wire ID remains eligible because its request profile can differ.
Response-state binding takes precedence: a request that can only use its
minting Deployment keeps the other credentials on that Deployment eligible.
Credential-scoped billing signals stay `RateLimitError(is_quota=True)` even
when a gateway wraps them in HTTP 400, so they still rotate credentials.
Explicit quota, permission, route-missing and transport classes retain their
distinct rotation contracts. A new dispatch starts fresh; this is not a
global blacklist or proof that reminted route handles share cryptographic
identity.

**Alternatives rejected:** repeated unchanged encrypted requests, automatic
ciphertext stripping, compaction-item deletion, generic cross-model rescue and
completed-tool reexecution are not recovery. Matching local digests do not prove
provider decryptability, account binding or ciphertext lifetime. This fail-stop
policy assumes an unchanged rejection is deterministic; replace it only with a
provider-validated, authority/accounting-bound recovery protocol—not an elapsed
retry window. Specs: `tests/llm/test_invalid_encrypted_content.py`,
`tests/llm_dispatch/test_dispatch_invalid_encrypted_content.py` and first-error,
usage, cancellation and recorded-root-loop neighbors. The dedicated
`tests/llm/test_invalid_encrypted_turn.py` drives the real root invocation,
completed file tool, durable event log and terminal failure with only synthetic
provider transport; it never rewrites the existing recorded fixtures.

### Encrypted state binds failover to its minting Deployment

**Problem:** one logical model can be served by several v2 Deployments behind
different upstream accounts, and replayed `encrypted_content` decrypts only on
the account that minted it. Dispatch treated every slot of the model as
interchangeable. Measured on the YourProvider gateway (2026-10-31): state minted on
the Kunlun Deployment of `gpt-6.1-sol` stayed valid across 2,039 rotations
between that Deployment's credentials, while the first failover to the public
`gpt-6.1-sol` Deployment was rejected `invalid_encrypted_content`. Two gateway
heap-exhaustion 400s had excluded Kunlun pairs, so the public slot was the
only ready candidate. That pick also rebound conversation affinity, so the next
turn was rejected on its first request.

**Decision:** when a stream settles on a slot with a Deployment and the
assistant message carries encrypted state, dispatch appends one private
`tofu_state_origin` marker naming that Deployment to the existing
`_responses_items` sidecar (`lib/llm/responses_state_origin.py`). The marker
rides every path that already persists and rebuilds the sidecar. It is never
provider input: the Responses adapter replays only its item allow-list,
checked conversion and the replay-order witness skip it, and usage recovery
retires it with the state it describes. Each sync, async and non-stream
dispatch derives the origins of state its body still carries. If a slot on an
origin is in the request's scope (provider pin, preferred model),
`LLMDispatcher.responses_state_binding` returns every other Responses slot in
that scope. The request then holds those as durable pairs from its first pick,
so capacity checks see them and no reset reopens them. Failover rotates the
origin's credentials, waits for it, or fails with its own error. Other
protocols keep their existing cross-protocol omission or refusal. An origin
outside the scope (model switched, Deployment removed) and unmarked historical
state keep existing routing. A marker adds 87–95 serialized bytes (measured for
the two production Deployment ids) to an assistant message that already
carries ciphertext; nothing else is persisted. Separately, a gateway servlet's
`nested exception is java.lang.OutOfMemoryError` in a 4xx body is classified
as a gateway transient rather than a payload rejection.

**Alternatives rejected:** stripping foreign ciphertext loses active-turn
continuity and is the recovery already rejected above. A hard sticky pin is
in-memory, expires and is recorded at pick time, so the failing failover pick
itself rebinds it. A credential-plus-Deployment binding forbids the credential
rotation the incident proved safe. Binding by logical model is the behavior
that failed. A marker records origin; it does not prove the ciphertext is
still decryptable there, so `invalid_encrypted_content` keeps its fail-stop
policy. Specs: `tests/llm/test_responses_state_origin.py`,
`tests/llm_dispatch/test_dispatch_state_binding.py` and
`tests/llm_errors/test_classifier_vendor_transient.py`.

### Learned request repair

**Problem:** routes reject optional controls they do not implement
(`Unsupported parameter: 'temperature'`, Anthropic `top_k: Extra inputs are not
permitted`), and preserved-thinking Anthropic routes reject a replayed thinking
block whose prefix changed (``messages.N.content.M: Invalid `signature` in
`thinking` block. The block is bound to a different conversation.``). Both were
generic `BadRequestError`s: the request failed, dispatch excluded the
`(key, wire)` pair for that dispatch only, and the next request repeated the
mistake.

**Decision:** `lib/llm/llm_errors.py` types two repairable HTTP 400 shapes as
`BadRequestError` subclasses, so an escaped error keeps existing dispatch
bookkeeping. `UnsupportedRequestParameterError(param)` requires exactly one
parameter path named by a recognized phrasing (OpenAI
`unsupported_parameter`/`unknown_parameter` code + `param`, `Unsupported
parameter: 'X'`, `Unknown parameter: 'X'`, Anthropic `X: Extra inputs are not
permitted`, `'X' is not supported with this model`) and that path must be in the
closed `REPAIRABLE_PARAMETER_WIRE_NAMES` vocabulary: temperature, top_p, top_k,
presence/frequency penalties, stop/stop_sequences, seed, parallel_tool_calls,
logprobs, explicitly named reasoning_effort and thinking.display. Required
fields, nested paths, several names, value errors and non-400 statuses stay
generic. `ThinkingBlockBindingError` matches only the documented binding
sentence; a bare invalid (tampered) signature stays generic.

`lib/llm/request_repair.py` owns the restrict-only repair. Sync/async stream
loops and non-stream `chat` retry the same request once per repair kind
(per-request `RequestRepairState`, mirroring the `ModelLimitError` same-slot
retry and `on_attempt_restart`); a repeated or no-op repair re-raises, and
repairs plus the limit clamp share the stream loop's `MAX_STREAM_RETRIES`
restart bound on both paths. A clamped or repaired rejection answered by a
non-200 status ran no prompt: `mark_status_line_refusal` (shared classifier
and `chat`'s non-200 branch) witnesses it as a provider refusal and `chat`
settles the attempt with `refused=True`, so a finite execution budget admits
the retry instead of fencing it with `execution_token_usage_unknown`. A
repairable rejection arriving as an SSE error frame after any content,
reasoning or tool output is re-raised as a plain `BadRequestError`: the
provider already ran, so nothing is retried or learned. The parameter repair
removes the canonical key (only the `display` subkey of `thinking`;
`top_logprobs` follows `logprobs`) and learns `{param, first_seen, last_seen,
count, error_code}` under the body's `_route_output_limit_key`, the Deployment
key dispatch stamps on stream bodies and passes to
`chat(route_output_limit_key=)`, so facts carry across both paths (provider +
wire model only for undispatched calls). Facts persist in `server_config.json`
`route_request_omissions` through `update_json_atomic`, outside the store lock
(the stored view merges back newest-wins per fact), are sanitized and repaired
at lazy load like `route_model_limits`, keep the 2,048 routes with the newest
evidence, and expire 7 days after the last rejection so a fixed gateway is
re-probed; one INFO line marks each new fact. The binding repair applies the
documented portable fallback (a live probe showed our gateway ignores the
binding beta): every assistant message loses `thinking_signature`,
`reasoning_content`, `reasoning_details` and thinking/redacted_thinking
`_anthropic_content_blocks` (copy-on-write; an emptied list is deleted). sha256
digests of the withdrawn signatures, never signatures, are remembered per
conversation (`_conv_id`, else `_task_id`) in a process-local LRU (1,024
conversations x 512 digests, 7-day sliding TTL). The pre-send hook
`apply_learned_request_facts` runs on the canonical body copy before protocol
translation in `prepare_request` and `chat`: it omits the route's learned
parameters and withdraws only the Anthropic-signed state bound to remembered
digests (thinking blocks, `thinking_signature`, signed `reasoning_details`
entries and the `reasoning_content` projected into them); plain reasoning stays
for a later non-Anthropic route, and blocks produced after the withdrawal stay.
Losing that memory is safe: a re-added block yields the same typed rejection
and its one-shot repair. A non-stream error object embedded in an HTTP 200 body
is classified but not repaired.

**Alternatives rejected:** name-based rules guess per model family and go stale
when a gateway or deployment differs from its family; observed route evidence
is exact. Dispatch-level exclusion only rotates keys within one dispatch and
learns nothing, while every key of the deployment rejects the same payload.
Unbounded learning would let a noisy gateway grow durable config without limit,
never re-probe a fixed gateway, and could strip required fields; the closed
vocabulary, TTL and capacity bound keep each fact restrict-only,
provenance-bearing and self-expiring. Witnessing every non-200 as a refusal
would hide billable policy blocks; only validation rejections the transport
repairs or clamps qualify. Repairing after streamed output with a restart
discards delivered output and learns from a provider that already ran.
Withdrawing remembered reasoning wholesale strips replay a DeepSeek-style
route requires, and keying it on the target family would put model knowledge
in the repair owner. Replacing the in-memory facts with the stored view outside
the lock could roll back a concurrent writer's fact. Specs:
`tests/llm/test_learned_request_repair.py` (classification tables, sync/async/
non-stream retry-once and shared bound, budget admission of retries, no repair
after output, cross-path route keys, TTL, durable load/repair and unlocked
write, binding strip and scoped withdrawn-digest memory) and
`tests/llm/test_nonstream_budget.py`.

### Readable reasoning summaries

**Problem:** reasoning effort, billed reasoning tokens and encrypted replay were
mistaken for readable thinking; generic Responses requests did not opt in even
when the route explicitly supported summaries. **Decision:** the installed
`responses_reasoning_summary` feature independently requests
`reasoning.summary="auto"` for a declared reasoning model whose effective effort
is not off. A Connection capability never activates unknown/nonreasoning Models. Codex supplies that installed binding; the official OpenAI Connection recipe
declares it on its endpoint, never as a global Model capability. Explicit
unknown/unsupported declarations suppress the automatic default. Generic routes
without a declaration keep omission. An explicit native `summary` value,
including null to opt out, wins over the automatic request. The same converter
owns streaming and nonstreaming requests; private route metadata never enters
provider JSON. **Alternatives rejected:** enabling summaries on every compatible
endpoint can send unsupported parameters; inferring summary availability from
reasoning effort or encrypted replay conflates independent capabilities.

A summary request is not a delivery guarantee. A provider may report positive
reasoning-token usage and return opaque encrypted items with empty summary
arrays even when it accepts and echoes `auto`/`detailed`. The frontend must not
decode ciphertext or fabricate reasoning text. The direct-chat HTTP control
allowlist remains unchanged; explicit native summary requests use the existing
Python dispatch `generation_options`/canonical-body boundary. Specs:
`tests/llm/test_responses_summary_request.py` and the Responses adapter suite.

### Preserve function strictness across protocols

**Problem:** omitting `strict` in Chat Completions means non-strict function calling, while Responses attempts schema normalization into strict mode. That can change the meaning of optional fields, including mutually exclusive message targets. **Decision:** the Chat-to-Responses adapter emits `strict: false` when the canonical function omits it; explicit booleans and parameter schemas remain unchanged. This is protocol-default preservation, shared by Codex and compatible Responses routes. **Alternatives rejected:** relying on provider defaults changes the contract; making every optional argument nullable would require changing execution semantics across all tools; weakening explicitly strict tools loses authored guarantees. Regressions exercise actual protocol conversion and both message target forms. See the [official function-calling contract](https://developers.openai.com/api/docs/guides/function-calling#strict-mode).

## Request flow
1. Parse a structured native ModelRef or resolve one compatible string with explicit Creator/Provider hints; ambiguity is an error. 2. Read the owner-scoped v2 aggregate, hard-filter visible access switches, authorization, capability, context, protocol, budget, stale/pending, and health state, then bind the bounded ordered candidates as request-owned slots. 3. Build the provider-neutral request body once; root rounds retain a positive full-prompt admission count as an internal-only sidecar for upstream accounting and cache-settle classification; every provider wire strips it. Final selected-slot hard-window admission always recounts messages/system/tools locally after adaptation, using content-digest tokenizer caches (the bounded shared cache plus, for a task round's body, that task's own `RequestTextCounts` through its provider view) rather than trusting that integer or a same-model reuse hint; tool schemas are re-encoded from content (see *Round token counting* below). The first exact wire-schema digest is reused only while the model and every ordered schema object remain identical. Per-slot adaptation and wire preparation never write the canonical history: each helper returns an adapted copy of only the messages, blocks and tools it changes, so every family reuses the canonical list and caller-byte immutability is the enforced boundary. A VLM-to-text fallback projects only that derived copy: one bounded marker replaces all images in each image-bearing message at their first image position, adjacent captions/references/tool results/prior assistant descriptions remain, pixels are declared unseen, and the durable multimodal history is unchanged. Final transport diagnostics derive semantic, whole-message-byte, and field-byte fingerprints in one shared history traversal; canonical field normalization emits the temporary tool alignment key in the same message scan, takes standard strings/text blocks directly while routing images and unknown shapes through the generic normalizer, and both raw-byte views hash the sealed request's per-message and per-field compact-JSON slices, so the history is not serialized again; a sealed `"cache_control":` key marks the uncommon item that is stripped and serialized (field by field, compact) for those views, as are malformed items and standalone callers. Primitive fields use byte-identical direct encoding, complex fields share a stateless configured stdlib encoder, and tool arguments use sorted orjson canonicalization. Message-sized values use process-local keyed integers, canonical rows retain only their alignment key plus field map, and stable-format hoisted/static/routing digests remain separate while standalone diagnostic APIs retain identical outputs. 4. Provider-boundary translation is followed by `lib/llm/request.py` sealing a request-lifetime immutable `AdmittedRequest`; Claude translated/cloaked text is recounted before sealing. Ingress detaches count-bearing input by rebuilding its containers around shared immutable leaves and computes the semantic identity before counting; sealing encodes each wire field once, the provider-prefix identity hashes those sealed slices, and transport diagnostics read the request-owned projection instead of decoding the payload. Transports consume detached envelopes or sealed bytes; scalar lookups use immutable metadata, complex lookups decode only their field. Fixed-size semantic and provider-prefix identities extend existing diagnostics, not provider cache-key claims. Responses WebSocket reuses the connection but sends full requests with explicit previous-response reset until incremental state equivalence is proven. 5. Execute with bounded connect policy and a 300s default rolling stream-idle window; every SSE/WS event renews it, so it is not a total request wall clock.
6. Normalize stream events and terminal usage into the internal vocabulary.
7. Settle health on the factual Deployment, Connection, Credential, or Credential×Deployment scope; record cost/usage, cache observations, retry outcome, and the final redacted RouteSnapshot.

Single-encode request ingress — Problem: every model request JSON-encoded its whole conversation four times (a round trip to detach input, the semantic identity, the sealed fields and a second encode for the provider-prefix identity) and decoded the sealed payload again for diagnostics; selected-slot adaptation without an admitted count (a model change, or no sidecar) also rescanned the full history with the entropy estimator's regexes. All of it holds the GIL and grows with history. Decision: detachment equals `json.loads(_encode(value))` without serializing: exact lists and dicts are rebuilt, exact `str`/`int`/`bool`/`None` and finite floats are shared, and any other carrier (tuples, subclasses, non-string keys, non-finite floats, cycles or nesting beyond the frame limit) takes that reference round trip and its unsent `request_json_invalid` error. The sorted-key semantic encode runs right after detachment, so lone surrogates and oversized integers in shared leaves still fail before admission. `seal_request` encodes each wire field once; the provider-prefix digest hashes the compact-JSON framing around those sealed slices through the `lib.utils` GIL-held primitive, which is byte-identical to the former second encode. Diagnostics read `strip_internal_request_fields` of the translated body, whose encode is the payload. The completion clamp's fallback estimate uses the shared text-digest cache (`cached_cheap_estimate_text`, request history is reusable), which returns exactly `cheap_estimate_text`, so clamp decisions are unchanged; API-field projection no longer issues a recursive copy call per immutable leaf. Per request the provider wire is encoded once and the canonical input once (the semantic identity); nothing is decoded. Alternatives rejected: deriving the semantic identity from sealed bytes (it is defined over pre-translation input with sorted keys, which no wire slice contains, so every recorded identity would change); one Python walker emitting sorted and wire bytes together (string escaping stays per byte, and float/subclass/key exactness would reimplement the stdlib encoder); cross-request encoding caches (identity keys pin history and need an eviction policy); trusting a foreign model's admission count in the clamp (changes its decisions). Measurement (`benchmarks/model_request_cpu.py --tokens 50000 200000 --rounds 6 --contention 4`, Python 3.12, alternating fresh processes, equal `resultDigest`): calling-thread CPU per request for `build_body` + slot adaptation + `prepare_request` fell from 28 to 15 ms at 57k input tokens and from 107–108 to 52 ms at 203k input tokens (1.07 MB payload) on the Chat wire, with Anthropic and Responses wires alike; slot adaptation fell from 42 to 11 ms and ingress from 62 to 38–39 ms at 203k tokens. Specs: `tests/llm/test_request_single_encode.py` (pinned identities, reference-pipeline equality, encode ledger), `tests/llm/test_llm_body_completion_clamp.py`, `tests/meta/test_model_request_cpu_measure.py`.

Round token counting — Problem: one model round counts the same request four or five times (compaction gate, prompt admission, selected-slot admission, stream ingress and the Anthropic recount) and estimates it again for the network-tier prefilter, the gate's entropy floor, L1's hot tail and the slot clamp. Only the gate and admission used the task memo, which kept one request; the final-body counts relied on the shared digest LRU, which skips text under 512 characters and is shared by every task, so each round re-tokenized every short text (each tokenizer call releases the GIL), re-ran the entropy regexes, re-dumped the tool-schema JSON and re-hashed the history per count. A live profile (pid 16090, 300 s) attributed 1,159 GIL samples and 14% of active samples to token counting. Decision: (1) the task-owned `RequestTextCounts` keeps content-free `digest -> (exact, entropy)` entries per projection view (`request`, `admission`, `provider`, `provider:anthropic`); a count replaces only its view's entries with exactly its request's texts and reads every view, so alternating projections never evict each other and retention stays one request per view. Keys stay per text (a message's count is the sum of its texts'), so an edited, stripped or re-projected message re-tokenizes only its changed texts; settled rows keep their per-text results in their own memo slots. (2) `build_round_request` attaches the task's provider view to the body (`TEXT_COUNT_VIEW_KEY`; underscore fields never reach a provider). Slot admission, ingress, the clamp and the recount still count the body from content and only text no view has counted reaches the tokenizer or the estimator. (3) Every text is encoded on its own (the joined-batch proof above), so a request's text tokens are the sum of its texts' tokens: memoized texts, the new tail and the system/tool parts are summed and the per-message, image and structural framing is added exactly as before; provider-measured tiers are unchanged. (4) Inside a view's `estimating()` scope `cached_cheap_estimate_text` answers from that view, returning exactly `cheap_estimate_text`. (5) The root loop's dispatch opens `task_round_text_identities`: every view created in the round shares one identity ledger, so the round digests each text object once; the ledger holds only that round's text objects and leaves the memo when the round ends, so the memo stays content-free between rounds. (6) The gate and admission views reuse the tool-schema JSON and its digest while the ordered schema objects are the same objects (copy-on-write catalog values, already trusted for admission's schema count); provider views re-encode schemas from content. Alternatives rejected: per-message digest keys (re-tokenize unchanged texts of edited or Anthropic-projected messages); trusting the admitted integer or the semantic request id at the final admissions (neither proves the body's content, and the sorted-key semantic identity ignores tool key order, which changes the schema text); identity- or `str`-keyed memo entries kept across rounds (would retain prompt text past compaction and terminal release); a larger shared LRU (still skips short text and is shared across tasks); a cross-view generation or capacity knob. The remaining per-round cost is one SHA-256 pass and per-text lookups over the history plus the content-proof schema encodes. Measurement (`benchmarks/round_token_count_cpu.py --no-l1 --tokens 50000 200000 --rounds 20 --contention 4`, Python 3.12, alternating fresh processes against an exported base tree, equal `resultDigest`, p50 calling-thread CPU per warm round of the counting entry points): OpenAI route 11.0–11.2 -> 4.7–5.1 ms at 71k input tokens and 39.3–40.5 -> 10.9–11.3 ms at 216k; Anthropic route 15.7–18.9 -> 6.9–7.6 ms at 67k and 57.3–61.3 -> 14.6–14.8 ms at 185k (beside four busy threads 44.3–44.7 -> 12.3–12.4 and 64.1–64.7 -> 16.2–17.6 ms at the large sizes). Texts reaching the tokenizer per warm round fell from 118–1,180 to 4–5 (the new turn) and entropy scans from 141–1,801 to 4; with default L1 compaction the large replays shrink to 76–78k input tokens and counting fell 33.5 -> 9.6 and 50.0 -> 13.6 ms. Specs: `tests/token_counter/test_round_text_count_reuse.py` (property and differential equality across rounds and projections, settled rows, schema identity, once-per-round digests and once-across-rounds tokenizer/estimator counters), `tests/token_counter/test_token_text_cache.py`, `tests/meta/test_round_token_count_cpu_measure.py`.

Non-chat surfaces enter at step 2 through the capability adapter. Listings emit only enabled, authorized model/provider pairs; historical probe evidence is diagnostic, never an access gate. Execution pins and disposes a request group; strict logical matching includes later-injected slots. Missing routes fail closed without legacy provider/global-key fallback.

Continuation rounds retain Provider preference and candidate ordering unless typed failure policy declares failover. Failover never rewrites conversation preference; metadata stores structured requested and actual v2 identities, not display labels.

Pure request preparation — Problem: preparation guarded mutating helpers with whole-body deep copies: `checked_conversion` deep-copied every body before the Messages/Responses adapters and selected-slot adaptation deep-copied the history for cache-marker, Claude and Gemini families. Live profiling (35be815b9, 12,617 GIL samples) charged 413 samples per observed window to the conversion copy and 101 to the slot copy, both growing with history; the adapters themselves never wrote their input. Decision: the writers became copy-on-write. `add_cache_breakpoints` replaces only the body's own `messages`/`tools` entries and copies each marked or normalized message, block list and tool; `_strip_trailing_assistant_for_claude`, `_validate_image_blocks`, `_downscale_oversized_images` and `_inject_gemini_thought_signatures` return adapted lists and copy only what they change; the Claude cloak replaces a converter `tool_choice` it shares with the canonical body instead of renaming it in place. Both copies were removed. Adapter payloads may share nested values (text, tool inputs, schemas) with the canonical body; later stages replace them, never edit them. `pin_model_images` still writes only its task-owned projection message, now from fresh validated blocks. Alternatives rejected: shallower defensive copies (still history-sized per attempt and each new writer needs auditing); keeping in-place helpers behind slot-local list copies (nested blocks and tool calls stay shared, so a Gemini signature would still reach the caller's prefix). Specs: `tests/llm/test_request_preparation_purity.py` runs every converter, slot helper, slot adaptation and ingress on a deep-frozen `settle` input (any in-place write raises), pins 17 golden receipts recorded before the change (sealed payload, semantic and prefix identities, admitted tokens, stable diagnostics, non-stream payloads) and fails on any deep copy of a body or history during adaptation and ingress; `tests/llm/test_slot_adapt_body_idempotent.py` keeps the mrne3bqe swap guard with an in-place neuter.

One per-message request encoding — Problem: after sealing, transport diagnostics serialized every message again (default separators) for the message and field byte fingerprints, and the semantic identity re-encoded the whole canonical history on every request although an agent loop only appends; live profiling charged 156 + 293 + 163 of 12,617 GIL samples to the fingerprint, identity and seal encodes. Decision: `seal_request` encodes `messages`/`input` item by item and each string-keyed item field by field (compact JSON frames members as `"key":value`, so the joined pieces are the former encode) and keeps the item/field offsets on the `AdmittedRequest`; the message and field fingerprints hash those sealed slices (memoryview slices, no copy), so diagnostics serialize only marker-bearing or malformed items. Their keyed runtime integers now hash compact UTF-8 bytes instead of default-separator text: both encodings are injective over JSON values, the values are process-local by contract (FloorRetry/cache diagnostics and one previous-round baseline, never persisted), and the standalone `wire_byte_prefix`/`wire_byte_field_prefix` share the same owner, so live and replayed rounds still compare. Settled (deep-immutable, `lib/conversation_sync/settled_rows`) messages memoize in their own memo slot, which lives exactly as long as the row: detachment shares a settled row that equals its JSON round trip (checked once per row); the semantic identity, whose sorted encoding starts with the messages, advances a SHA-256 state message by message and a settled row keeps the state it extended and the state after it, so a later request over the same settled prefix hashes only its new messages; a settled wire item keeps its sealed bytes and field offsets, and the Chat adapter passes a message without private fields through unchanged so a settled row stays its own wire item. Without settled rows the identity remains one encode per request and the wire one encode, item by item. Scope: `build_body` rebuilds the canonical history per round and production request messages are not settled yet, so the cross-request memos engage only for callers that hand in settled rows; cache-marker normalization and the Messages/Responses adapters create fresh items per request, limiting reuse there to the identity of untouched messages. The raw-archive request capture and token admission keep their own encodes. Alternatives rejected: deriving the semantic identity from wire slices (defined over pre-translation input with sorted keys; every recorded identity would change); identity-keyed side caches (pin history and need an eviction policy, which the row-owned memo replaces); memoizing each row's sorted bytes (retains a second history copy; two digest states per row suffice); keeping default-separator fingerprints (a second serialization of the history per request). Retention: a settled 200k-token Chat history (237 rows, 1.25 MB compact JSON) retains 1.43 MB of memo after one request, dominated by the sealed item bytes; nothing is retained for unsettled histories. Measurement (`benchmarks/model_request_cpu.py --tokens 50000 200000 --rounds 6 --contention 8`, frozen exports of the base tree and this change, two alternating fresh processes each, equal `resultDigest` in every run; the comparison also spans the encoder reuse and the removed deep copies): calling-thread CPU p50 per request for `build_body` + slot adaptation + `prepare_request` beside 8 CPU-bound threads at about 180k input tokens (0.91-0.93 MB payload) fell from 65.0/65.1 to 58.0/58.4 ms (Claude, Messages), 61.4/54.6 to 47.3/50.9 ms (Claude, Chat), 74.1/65.5 to 56.0/56.5 ms (Claude, Responses), 62.2/59.7 to 53.1/48.9 ms (GPT-5.6, Chat), 65.3/69.8 to 59.5/61.1 ms (GPT-5.6, Messages) and 59.4/57.6 to 50.8/51.1 ms (GPT-5.6, Responses); at about 52k tokens 17-21 ms fell to 15-20 ms. Ingress over a settled Chat history (an ad hoc component probe with the same 8 threads, each request appending one tool round, equal sealed digests) took 15.5 instead of 35.7 ms at 200k tokens and 3.8 instead of 8.7 ms at 50k; the remainder is dominated by token admission. Specs: `tests/llm/test_request_single_encode.py` (encode ledger: one sorted identity encode, the wire once item by item, diagnostics re-encode only marker-bearing items; settled-chain identity equals one encode; detachment shares exact settled rows), `tests/llm/test_request_preparation_purity.py` (golden receipts; a later request over a settled history encodes only its new messages and reuses settled wire items), `tests/tasks_pkg/test_wire_fingerprint.py` (sealed-slice capture equals the standalone views).

Conversation-to-key affinity across failover and re-mints — Problem: Anthropic's prompt cache is keyed per upstream credential, but two mechanisms silently undid affinity on model-routing v2 request-scoped groups (10-24/10-25 logs: `CACHE NAMESPACE SWITCH` + prefix re-writes at turn boundaries with no failure in the lane). (1) Affinity was recorded against `Slot.key_name`, a request-random `ephemeral_<16hex>` handle minted per turn, so the recorded key never exists in the next turn's fresh group and the picker fell back to `route_rank` order — undoing a mid-turn failover rebind at the next boundary. (2) `execute_invocation` wrapped EVERY invocation in `isolated_conv_affinity`, discarding the scope's routing entries on exit; a root invocation's scope IS the conversation id, so a root turn's warm key was discarded at turn end, contradicting the documented cross-turn affinity contract. Decision: `Slot.affinity_key()` returns the STABLE route identity (`route_credential_id:route_deployment_id`; a deployment belongs to exactly one connection, so the endpoint is implied; configured v2 pool slots already use that name and converge). The recency map stores that identity; the dispatcher's `_select` and `sticky_cooldown_remaining_s` match on it, and `sticky_cooldown_remaining_s` additionally scopes to the thread's pinned provider so one request's group never waits on a credential shared with another pin. `isolated_conv_affinity` takes `ephemeral=policy.is_child`: a child's one-shot scope is discarded on exit, a root conversation scope retains its warm key. Failover stays correct: the sticky identity is only honored while its slot is eligible (not cooled/excluded/disabled) — a failed or excluded credential never sticks. Alternatives rejected: keeping per-turn `key_name` (loses affinity at every re-mint); keying on the raw API key (leaks a credential secret into the diagnostic map); letting `sticky_cooldown_remaining_s` ignore the pin (cross-group contention). Specs: `tests/llm_dispatch/test_conv_affinity_failover.py`, `tests/conversation/test_conv_affinity.py`, `tests/llm_dispatch/test_sticky_warm_key_hold.py`, `tests/tasks_pkg/test_child_invocation.py`.

Root invocation route binding — Problem: `execute_invocation` runs every invocation in a copy of its caller's context, and `bind_provider_and_affinity` set the provider pin only when the task carried `_pinned_provider_id`. A root without its own route group therefore dispatched under whatever pin its caller still held: a retired request group confined it to slots that no longer exist (`no_slot`), and a live foreign group would have routed it through another request's credentials. Production pins are scoped, so the defect was latent until a test left a pin on its worker thread. The conversation affinity was already root-owned (`isolated_conv_affinity` plus the unconditional rebind). Decision: a root binds exactly its own route, its `_pinned_provider_id` or no pin; a child without its own group keeps its parent's pin, so it fails closed inside the parent's authority. Alternatives rejected: clearing every inherited pin, children included (a child could leave its parent's group for the configured pool); a second reset in `execute_invocation` (duplicates the binding owner; no dispatch precedes the binding). Specs: `tests/llm/test_upstream_turn_replay.py::test_root_invocation_dispatches_outside_a_route_left_on_its_caller` (the real invocation inside `provider_pin('retired-request-route-group')`; `no_slot` before the fix), `tests/llm_dispatch/test_lib_orchestrator_provider_binding_wire_parity.py`.

Fallback wire-byte stability — Problem: `_llm_call_with_fallback` rebuilt the fallback body via `build_body(...)` and dispatched it without the shared `preflight_wire_tool_body` boundary. `build_body` returns the canonical tool catalog verbatim; the provider-specific projection (Kimi MFJS root-`anyOf`/`oneOf` relaxation, GPT root relaxation) lives only at that boundary, which the normal round path always runs. A refusal opus→kimi emitted raw canonical schemas (e.g. `write_file` root `oneOf`) while every native kimi round emits MFJS, so the fallback round's hoisted `tools` bytes diverged inside the kimi cache namespace and re-billed the whole prefix on the NEXT kimi round (10-25 18:25, conv mvl5wxpj, rids 7f0c4531/52b1c908: `PREFIX MUTATION BREAK` 33024/30976 cache_write tokens, `first_changed_idx=-1`). Decision: a rebuilt configured-fallback or refusal switch-back body attaches the round's request metadata through `llm_fallback/_request_metadata.attach_rebuilt_request_metadata`: the round's stable identity (raw-archive context retargeted to the destination model) plus the task's tool authority/visibility from the ordinary `attach_tool_request_metadata` owner. The shared final projection (`project_tool_wire`, ending in `preflight_wire_tool_body`) then sends exactly the tools of an ordinary round on the destination model; without that metadata it skipped Tool Search, PTC and multi-agent resolution (the raw stable wire list instead of the deferred projection), and a missing `_conv_id` changed the Responses `prompt_cache_key`. Attempt latches and compiled reasoning of the source request never transfer; failover still changes model+key namespace intentionally (a real failover IS a namespace switch). Alternatives rejected: normalizing inside `build_body` (it is model-agnostic and must not assume a wire dialect); reusing the primary's projected tools (the fallback model's dialect differs); a separate preflight of the rebuilt body (it lacks Tool Search/orchestration authority, so it cannot match the ordinary projection); copying the source body's private keys wholesale (carries attempt latches and route state). The same metadata helper covers reactive overflow retries; destination builds retain the original thinking depth and the task-owned, owner-bound image preparation state. Specs: `tests/tasks_pkg/test_model_refusal_fallback.py::test_fallback_wire_tools_match_native_model_round` (the refusal path's dispatched fallback body through the final projection versus an ordinary round on the fallback model), `tests/tasks_pkg/test_fallback_wire_parity.py` and `tests/tasks_pkg/test_fallback_request_metadata.py`. Provider occurrences follow the [execution policy](../contracts/TOOL_EXECUTION_POLICY.md): distinct positions survive equal arguments and recycled IDs; stream-slot retransmission remains assembly-owned. Signature counting/collapse is retired in Python and native providers, so it cannot interrupt terminal usage or remove actions. No new pool/cache replaces the removed per-attempt signature maps. A Python 3.12 local probe (128 distinct calls, 1 KiB text each, 25 warmed samples) measured median accumulation/finalization at 5.73 ms before and 1.26 ms after removal; a separate traced sample retained 234,427 versus 216,267 bytes (peaks 238,562 versus 220,274). This is parser-only evidence, not network/tool execution latency. Specs: `test_tool_call_flood_admission.py`, Responses translation and native `tool_policy`/`tool_assembly`.

## Declared inference profiles

`contracts/model_routing_v2.schema.json` owns bounded optional `inference_profile` on Model, Offering, Connection and Deployment. `lib/model_routing/inference.py` resolves declared fields in that order (Deployment wins), without model-name or endpoint guesses. Unknown is not unsupported. With no reasoning declaration the isolated legacy path remains; controls expose unknown.

Legacy `thinking_format`/`responses_profile` enter only at metadata/binding boundaries. Profiles follow the selected route into pooled and ephemeral slots. Recipe facts stay on Offerings rather than shadow Connection overrides on Deployments. Provider → face/endpoint partial overrides use the same section resolver; explicit unknown/unsupported clears inherited enable details. `thinking_extra` is limited to 16 finite scalar fields.

Template normalization imports an authored effort contract only for IDs exactly listed in `contract.models`, using its ordered efforts/default rather than family guesses. Thus the packaged GPT-6 entry receives its declared contract without widening it to adjacent unknown IDs. Owner overrides remain authoritative; existing installations are not silently rewritten.

**Aggregate normalization cost.** Problem: `normalize_document` runs several times per settings, OAuth-status and server-config read. On the live 603-Model aggregate one pass took 115 ms in isolation, while the endpoints measured 0.7–3.2 s: each Model's defaults lookup issued its own `stat` of `model_inference_defaults.json`, every stat released the GIL, and with three busy threads each reacquisition waited a switch interval (offline: 2.3–3.8 s per pass with per-Model stats versus 0.43–0.61 s with one). Decision: `model_defaults.current_model_defaults()` snapshots the validated file revision once per normalization and `apply_model_defaults(..., profiles=)` reuses it; a changed file is still observed by the next normalization. `validate_inference_profile` remembers only the pure jsonschema pass verdict for exact builtin-JSON profiles (bounded at 512 identities; tuples, booleans-as-numbers and other look-alikes always revalidate) and still runs the finite/bound checks and returns a detached copy every call. The forbidden-field walk formats a path only after a hit, and row detachment uses `lib.utils.fast_deepcopy`. Measured on the live aggregate: `normalize_document` 115→46 ms, `public_projection` 119→49 ms, `RouteCandidateCompiler` 125→51 ms, `reasoning_controls` 155→63 ms, with identical output and identical error type/kind/field/message on 18 valid/invalid fixtures. Alternatives rejected: a cross-request normalized-aggregate cache needs owner/revision invalidation across every CAS writer and would become a second authority; skipping validation for stored documents weakens the fail-closed read boundary.

### Reasoning intent compilation

`lib/llm/reasoning.py` owns initial/failover compilation. `build_body(inference_profile=..., reasoning_intent=...)` retains `_reasoning_intent`: original choice, bounded sampling inputs and explicit native budget. Retargeting restores that snapshot, never infers choice from transformed wire fields. Off, lowest, default, preset and exact effort remain distinct; `minimal` is not off. Explicit profiles reject undeclared support, incompatible exact effort and unrepresentable budgets before I/O; product presets and disable preferences use the resolver's recorded degradation policy. Compilation commits atomically; plugins receive isolated nested input. Prebuilt streaming bodies capture explicit caller controls only when no retained intent exists; retained intent remains authoritative across retries.

Root rounds and post-loop synthesis use `build_body(defer_reasoning=True)`: `_reasoning_intent` is staged until selected-slot compilation, and transport rejects `_reasoning_pending` as `unbound_reasoning`. Same-model reactive rebuilds preserve the selected profile and compiled direct-library compatibility; pending requests stay pending, and different-model rebuilds cannot inherit source capabilities.

Conversation config derives a missing legacy `thinkingEnabled` from the selected `thinkingDepth`; a server-default false cannot overwrite a saved enabled depth. An explicit legacy switch remains an input preference. The shared resolver maps an unavailable product Off preference to the destination's lightest reasoning control, recorded as `_reasoning_resolution.reason = 'disable_unavailable_fell_back_to_lowest'`; an unavailable preset similarly records `preset_clamped_to_representable`. The original intent remains intact for retargeting. A model that can disable still honors Off. Exact native API efforts retain their precise destination-ladder validation, including the final Responses conversion boundary.

A saved `thinkingDepth` from a richer model may be unavailable after a model switch. The composer slider is a pure renderer: `frontend/src/features/thinking-depth-slider.ts::sync` always holds the verbatim saved depth. When the server restoration table maps an unavailable choice to a native rung, the thumb parks there while the saved choice remains flagged as unavailable; only an unmapped choice adds a flagged rung. The default intent remains a separate reset action, outside the native ladder. Persistence is gesture-only (pointer/keyboard commit); sync() never rewrites saved intent, so the choice survives a later rebinding back to a ladder that can represent it. Native names (including `ultra`) remain intact and are never re-ranked in TypeScript. Unknown/no-route controls preserve unverifiable intent. Explicit API efforts still fail closed; the UI projection does not change that contract. Local preparation errors end both primary and configured-fallback attempts without pool rescue. If dispatch never selected a slot, its error says no request was sent; a configured retry ceiling is not an attempted-request count.

`resolve_reasoning_control` is shared by compilation and `lib/model_routing/controls.py`. Composer controls intersect eligible deployment choices using one authorization witness per deployment, independent of credential count. A support label alone cannot expose an uninstalled or unrepresentable control. Saved choices on unknown or unavailable routes remain visible and unchanged; executable routes supply the server restoration table described above. Unconfigured callers retain `_reasoning_legacy.py`; old prebuilt bodies enter through the one-time native parser.

A `default` intent on the `reasoning_effort` wire emits the destination's declared `default_effort` explicitly; every other wire format keeps omission semantics. Problem: kimi-k3 was pinned to the Anthropic face (2026-09-10, OpenAI-face protocol-frame poisoning); with no reasoning control on the Messages wire the upstream thinks only opportunistically (~85% of tool-loop rounds streamed zero thinking, raw-archive evidence), while the composer stores `default` for the follow-the-model-default slider position and paints it at the declared `default_depth` (max) — the UI showed Max, the wire carried nothing. Decision: the declared default rung is the provider-documented default admitted with evidence, so emitting it is a no-op on conforming upstreams and a repair on divergent ones; scoping to the effort-ladder wire leaves Claude's adaptive default and unknown/unsupported destinations untouched. Alternative considered: emitting nothing for `default` everywhere (status quo) — rejected, it makes "default" indistinguishable from the cheapest rung exactly on the wire family where the effort field is the only control.

## Provider adapters

Wire adapters converge OpenAI-compatible, Responses, Anthropic, subscription and owner ProviderAccess results before task code sees deltas; task policy, compaction, billing and tool execution remain elsewhere.

Responses tool results containing canonical `image_url` blocks become native `function_call_output.output` arrays of `input_image` and `input_text`, retaining the call ID, block order, image detail and adjacent text. User/tool images share one image projection. Text-only tool outputs retain their existing string/JSON contract; malformed images and unsupported roles still fail before transport. The program interpreter's bounded JSON text lane rejects images explicitly instead of serializing pixels or bypassing its budget. This applies to both Responses profiles and stream modes; it does not rewrite stored history. Specification: `tests/llm/test_responses_tool_images.py`. Protocol authority: [OpenAI function outputs](https://developers.openai.com/api/reference/python/resources/responses/methods/create). Claude canonical/gateway bodies carry top-level `effort`; only Messages conversion moves it to `output_config.effort`, preserving format and giving canonical effort conflict priority. Native ingress normalizes before dispatch. Opus 4.7+ explicitly sends every adaptive rung (product medium versus API default high); off disables thinking without effort, ultra maps to max. Older-family omit-medium remains a compatibility gap. Explicit low is not the low preset; same-model reactive rebuilds preserve effort, intentional fallback owns its policy. Native disabled thinking may independently carry low/medium/high effort. Diagnostics allowlist effort, never schemas. `test_claude_effort_final_wire.py` and rung-parity tests prove local transport, not gateway consumption or route identity; [cache/beta limits](../LLM_COST_OPTIMIZATION.md#claude-effort-and-prompt-cache) remain separate. Messages also projects canonical `reasoning_effort` to `output_config.effort`, preserving the model-adjusted rung and native output format. Priority is `effort` → `reasoning_effort` → native value; missing effort is never synthesized. This matches [Kimi Messages](https://platform.kimi.ai/docs/api/messages.md) (`low`/`high`/`max`) without a CatPaw/model-name branch. `test_anthropic_reasoning_effort.py` checks final stream/non-stream wires across Chat, Messages and Responses; it does not prove gateway consumption.

The v2 Connection protocol `openai_responses` becomes the execution protocol `responses` once in `Slot.__post_init__`, shared by pooled and request-scoped slots. The persisted Connection and RouteSnapshot retain the v2 vocabulary. Both subscription and keyed Responses routes must therefore emit `/responses` with a Responses body; passing the storage spelling through to the transport would fall through to `/chat/completions` and produce a deterministic Codex 404.

A translator preserves text/reasoning/tool work, finish/truncation meaning, cache/reasoning usage, ordered tool-call occurrences, and typed errors without leaking credentials. Streaming tool assembly continues an index-less active slot only while ID/name evidence remains compatible. A different ID opens a new slot even when its name arrives later; same-name/no-ID ambiguity or an invalid index makes the stream malformed instead of merging executable calls. Exact complete-frame retransmission at the same stable slot is ignored, but equal payloads at different positions remain distinct.

Responses adapters use `output_index` as the primary occurrence identity and use an item ID only while it is unambiguous. Recycled item IDs at distinct positions remain separate; a later delta carrying only an ambiguous ID fails closed. A terminal `response.output` array is the ordered authority and replaces provisional opaque items position-for-position, preserving even byte-identical entries. Complete visible text snapshots append only a missing suffix after matching the emitted prefix with a constant-space length/hash proof; conflicting text fails, while omitted/null text metadata adds no text authority. Hidden agent messages stay hidden. The WebSocket incremental-input ledger counts occurrences rather than set membership, and advances state only after a verified terminal response; malformed or interrupted streams retire the socket. Unknown provider payloads fail explicitly: do not add `.get()` chains that turn a malformed response into an empty successful answer. Recognizable foreign model envelopes on a Chat stream fail fast with `protocol_mismatch`, using bounded protocol names, not provider text. An SSE event label or generic ping alone is not protocol proof; valid Chat payloads and structured provider errors retain their existing semantics.

### Readable Responses snapshot reconciliation

**Problem:** some providers send readable reasoning only in text/part `.done`,
item snapshots or the terminal response; consuming deltas alone discarded that
text. **Decision:** `responses_outbound/_reasoning.py` owns readable item/channel/
part prefix reconciliation. Verified snapshots emit only unseen suffixes; equal
text at distinct positions stays distinct. Paragraphs begin with nonempty text,
not empty or encrypted-only markers. Repeated lifecycle snapshots are idempotent.
Known output positions retain relative emitted order even while other positions
are unknown; conflicting prefixes or late earlier-paragraph insertion/reordering
fail typed validation before a successful terminal is emitted. Missing/null/empty
arrays remain metadata rather than evidence of erased text. Opaque replay stays
with `_sse.py`, independent of readable summaries and usage.

Each item/part keeps a length/hash proof, not the whole emitted text. State has at
most 4,096 item occurrences and a separate shared total of 4,096 parts; blocked
text shares the framing owner's 1 MiB event-byte bound. In-order reasoning retains
no queued text. Parser-only `tracemalloc` samples retained 933 bytes after 64 KiB
and 869 bytes after 8 MiB of one live part; a blocked 1 MiB sample retained
1,452,325 Python bytes and refused the next byte without exceeding its text bound.
These measure reconciler state, not accumulator, network or application memory.
**Alternatives rejected:** ignoring final snapshots loses actual provider text;
text-based deduplication merges distinct occurrences; retaining complete prefixes
adds memory proportional to already-delivered reasoning; speculative reordering
cannot repair bytes already shown to a reader. Specs:
`tests/llm/test_responses_outbound_reasoning.py` and the shared SSE parity suites.

### Known non-output Responses events

`_KNOWN_IGNORED_EVENTS` in `responses_outbound/_sse.py` lists event types that
carry no canonical delta; any other unrecognised type logs one WARNING per
stream and the stream continues. **Problem:** ChatGPT-backend (Codex) streams
send `response.metadata` after `response.in_progress`, so every such stream
warned. The frame is absent from the public Responses streaming reference; its
`metadata` object carries prompt-moderation flags
(`openai_chatgpt_moderation_metadata`), model-verification recommendations or
safety-buffering notices, never generated output, and it can precede an `error`
rejection (observed upstream as `server_is_overloaded`). Upstream Codex parses
it only as a UI side channel. **Decision:** it is a known no-op; the completion,
refusal and error events stay authoritative. **Alternatives rejected:**
surfacing the moderation/verification values needs a product consumer Tofu does
not have; validating the frame's shape would let an informational frame fail a
stream. Spec: the `response.metadata` cases in
`tests/llm/test_responses_outbound.py`.

## SSE framing and stream-activity watchdog
Sync/async HTTP error responses retain at most a 256 KiB diagnostic prefix through `lib/llm/provider_error_body.py`; HTTP status and UTF-8-first decoding remain authoritative, and clipped raw archives are incomplete. Sync and async diagnostic reads stop after five seconds or caller cancellation, including while waiting for bytes; expiration preserves the prefix and HTTP status. Sync reads reuse the attempt’s single transport watchdog, arming it even when rolling idle timeout is disabled; diagnostic capture creates no extra reader or timer thread. Incoming bytes cannot renew this diagnostic deadline. Caller Stop wins races with error-body EOF, read failure and byte-limit truncation. Iterator cleanup errors are type-only diagnostics and never replace status/Stop; async iterator closure has a separate 100 ms budget. Response closure remains the transport owner’s responsibility. Specs: `test_provider_error_body_bounds.py`, `test_provider_error_body_deadline.py`. Sync HTTP uses decompressed urllib3 `read1` (legacy one-byte fallback), preserving requests errors; short SSE frames must arrive before EOF for close/length/chunked bodies, plain or gzip. Async and relay readers already yield available chunks. Watchdog interruption shuts down the active read socket before reader-owned response closure; closing a buffered response from the watchdog would wait on its read lock. The legacy urllib3 path remains inside the transport adapter. Specs: `test_sse_http_delivery.py`, `test_sync_stream_socket_cancel.py`. Chat `reasoning` is a string alias; whitespace-only stop is empty; SSE `message` without `delta` is malformed, never silently discarded. `[DONE]` without finish is not completion; finish without DONE may be valid. `LLM_DEBUG_RAW_SSE=1`/model substring enables redacted capture (64 Ki chars/line, 400 lines/256 KiB ring, per-write file rotation via log policy); default anomaly logs keep original decoded character counts and bounded framing categories, not bodies. `RawSSEDumper.payload` alone owns redaction and length measurement, avoiding double-redacted placeholder counts. JSON is redacted structurally; tool input/argument fragments, malformed/oversized payloads and over-depth values are omitted even in debug. Truncated/redacted capture is not byte-exact replay. All byte-stream transports use `lib/llm/_sse_framer.py`, driven by the shared `SSEByteStream` lifecycle. It incrementally decodes strict UTF-8 and frames SSE events across arbitrary byte boundaries, including CR/LF/CRLF, comments, a leading BOM, repeated `data:` fields, multiple events in one read and a split `[DONE]`. One event is capped at 1 MiB; lazy event delivery uses at most a 64 KiB decode slice without copying an entire bytes-like input or retaining a chunk-sized list of events. Dense invalid-byte runs are counted without repeatedly decoding/copying their suffix. Invalid UTF-8, invalid JSON, an oversized event, or unterminated data at actual EOF closes as `malformed_stream` with bounded, credential-free diagnostics. Data-free comments/id/event/retry tails are discarded; retry hints require bounded ASCII integers.

A consumer-recognized protocol terminal (OpenAI `[DONE]`, translated Anthropic `message_stop`, or Responses terminal) ends parsing at that complete event, not at an arbitrary network chunk boundary. Bytes after it never affect content, tools, usage, errors or framing, even when the same read includes a partial/invalid trailer. Intentional close releases the parser without manufacturing EOF truncation; it never erases earlier corruption, grants success to DONE without finish, or bypasses tool-input validation. A finish reason alone does not stop parsing: final usage must still arrive, and pending data at EOF must still fail. Clean finish without `[DONE]` remains valid. Client cancellation is distinct from both protocol completion and EOF. Before HTTP headers, async Stop cancels and joins one request-scoped opening task; its response context closes even when Stop races with header arrival. Pre-stopped calls open no connection. Problem: body-only cancellation left silent header waits unresponsive. Decision: own setup and teardown in the existing async transport; alternatives rejected: abandoned POST tasks, arbitrary generation deadlines, or replaying a possibly accepted prompt. Real-socket and context-race specs: `test_async_stream_header_cancel.py`. The Responses WebSocket path submits an already-decoded provider payload directly; it never manufactures a `data:` line.

Sync and async attempt keep-alive EOF cleanup with fixed ceilings in `_transport.py`: 100 ms, 64 KiB and 32 chunks, whichever is reached first. Sync uses the attempt's existing watchdog and reader-owned response closure; no additional reader or timer thread is created. Cleanup bytes do not re-enter the parser; timeout/read failure discards the connection, not completed work, and explicit user/task cancellation propagates, including cancellation racing with watchdog expiry. A continuously emitting trailer cannot renew this deadline or cause a billed retry. Specs: `test_sse_terminal_boundary.py`, `test_sse_terminal_cleanup.py`, `test_sse_framer.py` and connection-reuse tests.

Problem: stopping at the SSE terminal left HTTP chunked EOF unread, so sync response closure discarded otherwise reusable proxy/TLS connections. Decision: perform bounded transport cleanup after the protocol terminal. Alternatives rejected: immediately closing forces a new handshake each round; unbounded draining lets trailing bytes hold completed answers; increasing connect timeouts does not restore reuse. Measurement: three sequential chunked streams now accept one TCP connection instead of three, matching content-length responses without changing active concurrency or retained pool limits. Real-socket silent-EOF tests verify prompt completion with idle timeout disabled and both current and legacy urllib3 interruption.

`TOFU_LLM_IDLE_STREAM_TIMEOUT_S` is one attempt's continuous transport-idle window, not its maximum wall time. **It is permanently disabled by default (`0`); a deployment opts in with an explicit positive value.** Do not restore a default cutoff here — see the seal note on the env var in `lib/llm/_transport.py`. When a value is set, `0`/unset disables the window, and positive values below 30 seconds clamp to 30. The shape matches native Codex's rolling stream behavior; sync SSE, async SSE, and Responses WebSocket paths share these rules. (The pinned `@openai/codex` `rust-v0.149.1` [provider default](https://github.com/openai/codex/blob/rust-v0.149.1/codex-rs/model-provider-info/src/lib.rs#L26-L27) of 300,000 ms and its [Responses SSE loop](https://github.com/openai/codex/blob/rust-v0.149.1/codex-rs/codex-api/src/sse/responses.rs#L552-L575) informed the window's *shape*, not its default; the 2026-10-23 incident showed a default idle cutoff discards healthy slow answers from buffered/reasoning providers.)

The live response arms the rolling idle window at response headers. The preceding header wait still runs the same watcher for Stop and heartbeat polling, but it cannot arm the idle window: there is no response handle to interrupt and no body activity to renew the clock, so an expiry there could only latch and then discard the first chunk of a healthy late answer (the 2026-10-23 zero-byte incident). The header wait remains bounded by the caller's `first_byte_timeout`/`attempt_timeout` deadline and the abort poll. Every byte chunk or WebSocket message renews the window, including SSE comments/keep-alives, empty data, signatures, usage, finish metadata and protocol-only events such as `response.in_progress`. Reasoning/content/tool deltas remain semantic diagnostics; their absence never terminates generation. A full window without transport activity closes the attempt as `premature_close` / `midstream_close`, using the existing bounded retry and route-health policy.


### Silent-stream deadline

Problem: the idle-window seal above assumed a wedged request is still bounded by `attempt_timeout`/`first_byte_timeout`, but interactive root-agent and sub-agent rounds pass neither. On 2026-10-28 a provider sent response headers and then no byte; the round blocked in `ssl.read` under `_iter_response_bytes` for more than 3 hours with `TOFU_LLM_IDLE_STREAM_TIMEOUT_S=0`, and fallback never ran because no error was ever raised.

Decision: every attempt arms the named `SILENT_STREAM_DEADLINE_SECONDS = 900.0` backstop at response headers (sync SSE, async SSE, Responses WebSocket). The fixed hardware-independent liveness contract lives here and in `lib/llm/_transport.py`; it has no tuning environment variable. It uses the same rolling transport clock as the idle window, so any byte renews it: SSE comments, keep-alives, thinking deltas. Expiry closes the response from the watcher, which also unblocks the reading thread, and raises `ResponseReadTimeoutError(cause_type='stream_silence')`. This error is never accepted as a partial answer. It skips the same-slot retry loop (as `attempt_deadline` does), so dispatch excludes the slot and rotates to another slot or the fallback model, and the turn either continues or fails visibly. User Stop keeps its own abort poll and still lands during the silence. An opted-in tighter idle window still fires first, with its premature-close semantics. The header wait is unchanged: it is not covered, so gateways that buffer headers (2026-10-23) are unaffected.

Threshold evidence (2026-10-28 logs): the agent `[Timing]` request-to-first-token latency (header wait plus first-byte silence) was p50 7.8 s, p99 37.7 s and max 128.5 s over 119 rounds. Per-chunk arrival times are not logged (`raw_sse_anomaly.log` frames carry only the per-request `created` stamp), so a long post-header silence of a healthy stream is bounded by that first-token figure. The 2026-10-23 incident had first-token latency just past 300 s. 900 s is 7x the observed maximum and 3x that incident, while bounding a dead stream to 15 minutes instead of indefinitely.

Alternatives considered: (1) restoring a default `TOFU_LLM_IDLE_STREAM_TIMEOUT_S`, rejected because it reverses a recorded seal and reuses premature-close semantics that discarded healthy answers; (2) a caller-side `attempt_timeout` on root-agent rounds, rejected because it is a total wall-clock bound that would cut long healthy generations, and because it is a per-caller workaround that leaves other callers exposed; (3) per-transport socket read timeouts, rejected because they would add a second, transport-specific deadline mechanism next to the shared watcher that already owns Stop, heartbeats and the transport-activity clock. Verification: `tests/llm/test_llm_silent_stream_deadline.py` uses a real local HTTP server (headers then silence, then slow steady chunks, then Stop).
`TOFU_LLM_SEMANTIC_IDLE_TIMEOUT_S` and `TOFU_LLM_NO_ACTIONABLE_TIMEOUT_S` are deprecated lower-priority aliases for the same transport-idle duration. They log an import-time warning because keep-alives now renew the value. New attempts never emit `semantic_progress_timeout`; that state, its usage flags, retry label, and `no_actionable_output` remain read-only compatibility for stored timelines and plugins.

There is no absolute per-attempt ceiling while nonterminal transport events continue; model token limits, task/round policy, and user Stop still govern it. Typed stream evidence and Turn Trace record request elapsed time, response headers, transport bytes, complete SSE events, transport/semantic ages, reasoning/content/tool progress, provider finish, client abort and bounded malformed-frame diagnostics. Legacy usage flags project that evidence, never a second classifier.

Problem: continuous transport traffic suppressed waiting-status callbacks, and a socket exception skipped accumulator finalization, losing the failed physical attempt's progress when a retry succeeded. Decision: publish waiting status on the existing watcher interval during semantic silence or unfinished tool-argument generation, independently of incoming bytes; preparing a tool call uses the existing localized generation phase and never implies tool execution. Record bounded `llm.stream_interrupted` counts and the physical trace ID before propagating an iteration error. Argument bodies and credentials are never included. Alternative rejected: a semantic deadline would interrupt live Codex generation; raw archives alone cannot diagnose attempts after quota exhaustion. No additional watcher or retention tier is introduced. Verification: `tests/llm/test_stream_interruption_visibility.py` covers metadata traffic, semantic progress, retry preservation, and partial argument failure evidence. Measurement: 240 callbacks over a simulated hour at the default 15-second interval persist eight sampled phase frames through the unchanged 128-frame per-round retry/wait budget. One interrupted-attempt sample with 13,643,802 transport bytes serializes 629 bytes of fixed progress evidence; the watcher adds one 24-byte float on Python 3.12 and no thread or queue. Existing log rotation owns the once-per-interruption diagnostic's retention.

## Dispatch policy

### Shared dispatch retry state and attempt failures

Problem: separate sync and async exception handlers drifted. A response-read
timeout rotated a failed pair in sync dispatch but surfaced immediately in async
dispatch, leaving the route eligible for selection again. Non-stream dispatch
also kept a separate set of counters and exclusions: permanent model bans were
invisible to capacity-recovery checks, and cancellation could leak a reservation.

Decision: `_api_failures.DispatchFailurePolicy` owns the common exception
classification, failed-slot settlement, retry notices and single OAuth refresh
per request. `_api_retry_state.DispatchRetryState` owns counters and exclusions.
Each loop retains transport, cancellable waits and queue timing. The historical
facade name `_StreamRetryState` refers to the same state implementation.

`ChatRetryState` keeps non-stream model bans durable so a wait cannot resurrect
them. It preserves the existing subscription-wrapper saturation-clock policy;
local waits and actual upstream retries remain separate counters. Its legacy
aggregate retry metadata stays unchanged. `ChatFailurePolicy` keeps complete
response transport errors request-scoped and retires stream-only models;
streaming response-read failure rotates a pair. Request/credential ownership,
route pins and caller budgets stay with their current owners.

Every pre-transport admission failure and cancellation releases its reservation
without provider-health damage. After non-stream success has settled the slot,
later bookkeeping failures propagate without releasing again or initiating a
new provider attempt. Public tests retain another reservation on the same slot
to detect cross-request corruption. Quota tests inspect resulting key health
through chat, sync stream, async stream and racing calls: HTTP 402 stops the
account, while ambiguous quota HTTP 429 stops only the affected model.

Alternatives rejected: separate exception ladders can drift again; forcing
whole-response retries to behave like streaming can duplicate delivery; adapting
async dispatch through a synchronous transport changes event-loop ownership.
Source call-count tests cannot establish quota scope when implementations are
shared. Verify public timeout, untouched-error, cancellation, OAuth, contention,
capacity-recovery, post-settlement ownership and budget contracts instead.

### Subscription route recovery

Connect recovery ownership — Problem: exhausting the subscription route plan raised an endpoint failure, adding a 30-second model cooldown and a dispatch pair exclusion even when a sibling restored the shared proxy seconds later. Decision: connect-only exhaustion preserves typed evidence through the shared HTTP client and becomes `SubscriptionConnectionError`, handled by the existing bounded transport retry loop (at most `1 + MAX_STREAM_RETRIES` attempts, each trying eligible routes sequentially). Each retry re-enters owner-scoped route admission; the route manager supplies the only recovery delay, so a successful sibling can release the wait immediately. Persistent exhaustion and the recovery-wait deadline stay request-scoped and never penalize model/credential health. Sync, async (including proxy negotiation), and nonstream calls share this behavior. An ambiguous sync disconnect before headers is terminal request-transport failure, not replay-safe connection evidence. Alternatives rejected: disabling route circuits hammers unavailable proxies; another dispatch retry loop duplicates policy; model/key rotation cannot repair the shared path. No worker, cache or durable state is added. Controlled measurement: a route recovered at 2 seconds completes with two connection attempts, one dispatcher selection, zero model errors/cooldowns, and a paired wait/recovery notification; cancellation sends no second request. Specs: `tests/llm/test_subscription_connect_recovery.py`, `test_egress_recovery.py`.

Problem: a single interrupted response opened a host/proxy circuit; sibling requests mistook its cooldown for a new endpoint failure and added a 30-second model cooldown. Decision: midstream I/O/EOF makes the concrete route require one immediate shared probe. Only failed probes/connections open route backoff. `lib/llm/egress_recovery.py` waits before sending, reusing the route manager's deadline and owner-scoped desktop fallback. Sync/async/nonstream transports share this policy, cancellation and the transport-idle wait budget (explicit nonstream timeout wins). No request is resent and no provider/credential error is recorded merely for waiting. Socket idle measurement begins after route admission. Recovery clears the wait HUD immediately; transient wait phases use the existing bounded retry-event budget and never claim a model/route switch. Paired wait/recovery frames share one signature budget (at most 8 + 8), so sampling cannot strand a visible wait after recovery; the 128-frame cap stays. Alternatives rejected: disabling circuits hammers dead proxies; another model cooldown prolongs a recovered outage; changing credentials cannot repair a shared network path. No worker, cache or persistent record owner is added. Measurement/specification: 50 concurrent-path polls behind a failed revalidation produce one probe; simulated recovery after 2s bypasses a 5.7s circuit deadline with one successful dispatcher invocation and zero model/credential errors. Tests: `test_egress_recovery.py`, `test_subscription_routes.py`, and the browser network recovery journey.

Transport evidence: streaming sync pools keep the 10-second TCP/TLS connect budget (override retained) and use an independent 60-second upload budget, matching async writes. Problem: ConnectionError wraps post-connect write failures too. Decision: only positive connect evidence is endpoint-unreachable; upload/read/pool/TLS/unknown failures retain their phase as RequestTransportError and surface without key/model penalties or fallback. Envelopes carry failureStage, transportReason, transportCause and available requestBytes/timeoutSeconds, never payload or credentials; read timeouts keep bounded transport retries. Alternatives rejected: increasing connect time delays real dead-host failover; treating every ConnectionError as connect invents outages. Specs: tests/llm/test_transport_failure_phases.py and dispatch stream tests.

Proxy setup timeouts — Problem: urllib3 marks the proxy TCP connection established before its CONNECT tunnel completes; a timeout reading that tunnel response can therefore escape as a response-read timeout and exhaust same-slot retries instead of trying another eligible network route. Decision: the model-pool connection wrapper translates raw/urllib3 read timeouts raised strictly inside `connect()` to urllib3's connect-timeout type. Explicit setup evidence outranks raw socket write wording; typed post-connect write timeouts remain upload failures. The existing subscription route planner then owns safe failover before any model request is sent. Actual post-connect response reads and uploads retain their separate error types and policies; no timeout, retry count, model selection, or whole-agent replay is added. Alternative rejected: classifying all ReadTimeout errors as connect failures can replay a request already accepted upstream. Verification: `tests/llm/test_proxy_connect_timeout.py` exercises the real requests adapter with synthetic pre/post-proxy-TCP setup failures, no network traffic, and unchanged response-read handling.

Request-scoped proxy exemptions — Problem: minting an ephemeral slot for a self-hosted base URL (private, local-suffix or bare-IP host) registered the host as a process-lifetime proxy bypass (`register_no_proxy_host` plus a `no_proxy`/`NO_PROXY` token) before the pre-flight probe ran, and disposal never removed it. Every distinct host a request named, including mints the probe rejected, permanently re-routed all later traffic to that host for every owner and grew the environment every child process inherits; a pytest worker's registry and `no_proxy` grew with each ephemeral spec. Decision: request-scoped owners lease the exemption (`lease_no_proxy_url` / `release_no_proxy_host`). The handle leases after the probe and before publication; disposal and every path that does not publish return the lease exactly once. Concurrent holders of one host share the exemption; the last release removes it and the `no_proxy` token that lease appended. A permanent registration (configured providers, discovery, local serving) always outlives a lease, and a token present before the lease stays. Alternatives rejected: keeping process-lifetime registration (residue keyed by request input, unbounded in distinct hosts); unregistering on disposal without counting (breaks a concurrent slot or a configured provider on the same host); isolating only the tests (hides the product leak). Specs: `tests/proxy/test_proxy_bypass_leases.py`, `tests/llm_dispatch/test_ephemeral_slot.py::EphemeralSlotTest::test_private_host_bypasses_proxy_only_while_minted`, `tests/llm_dispatch/test_ephemeral_capacity.py::test_failed_mint_returns_its_proxy_exemption`.

Recovery backoff debt — Problem: a successful lightweight probe cleared the route's failure streak AND reset its backoff staircase, so a flaky route alternated failure→probe-ok→failure with only 5s circuits instead of the intended 5→15→30→60→120s progression. Decision: probe success reopens route admission (the circuit closes and candidates are served) but only a successful real request forgives the backoff debt. In-flight probe results are fenced by a `(generation, request_revision)` snapshot so a stale probe can never overwrite a newer real outcome. The recovery HUD shows `stream.phase.connectingModel` ("Connecting to {model}…") instead of claiming the request was already sent; generic `waitingForModel`/`waitingModel` labels drop the premature "sent" wording. Alternative rejected: clearing backoff on probe success conflates reachability with delivery and amplifies flapping. Verification: `tests/llm/test_subscription_routes.py` covers probe-admits-without-forgiving, real-request recovery, singleflight fencing, and host/route isolation; `frontend/src/conversation/presentation/network-recovery.test.ts` covers the honest recovery label in both locales.

### Native physical writes and stdio framing
Production composition — Problem: native application fixtures copied short HTTP timeouts. Decision: `lib/tasks_pkg/native_http_policy.py` translates the existing LLM transport owner’s cached launch-scaled keepalive settings, connect/upload/stream-idle budgets and terminal drain bounds. It adds no probe or client; explicit validated overrides remain separate. Unbounded header waiting stays under caller Stop/deadline. Alternatives: copied defaults drift, and a new environment parser creates a second policy owner. `system_native_production_http_policy_20261016.json` verifies this compiler; control/account/checkpoint profiles and default migration admission remain separate.
Problem: the native transport had no independent write budget, H2 could erase a writer timeout into BrokenPipe, and dropping an HTTP/1 response future could leave its buffered upload running. Decision: optional positive `write_ms` bounds each pending post-connect physical write/flush; null/omission retains an unbounded write budget. The maintained, non-publishable reqwest 0.12.28 patch at `packages/vendor/reqwest` keeps TLS, explicit proxy, decompression, HTTP/2 and pooling with their existing owners. Connection-local timeout evidence is captured from the exact connection selected for each attempt; it cannot contaminate another connection. Native transport also opts into an HTTP/1 upload guard when the write budget is absent: dropping a pre-header request poisons and wakes that connection for closure. HTTP/2 cancellation remains per stream and never cancels a shared connection merely because one caller stops. No polling task, count gate or retry is added; at most two pending timer registrations live on an existing connection, and a ready operation/drop cancels its timer. The write budget does not include DNS/TCP/TLS/proxy setup, response-header/read waiting, or HTTP/2 receive-window credit waiting. Those phases retain connect, header, stream-idle and caller Stop/deadline policy. Typed upload failures retain `failureStage`, `transportReason`, `transportCause`, `requestBytes` and `timeoutSeconds` in the native terminal error without URLs, credentials or payloads. Alternatives rejected: a whole-request timeout interrupts model generation; body-producer EOF does not prove network writes; matching the text BrokenPipe invents a timeout; a client-global flag misclassifies healthy connections; a new HTTP implementation duplicates the existing protocol and TLS ownership.

Problem: rescanning the complete stdin prefix after each 8 KiB read made large native configuration frames quadratic. Decision: `cli_agent::Input` retains a delimiter scan cursor across cancellation and uses memchr only on newly appended bytes; completed frames reset the cursor after draining. Frame limits, malformed-frame refusal, authenticated scope unwrap and retained tail transfer keep their existing boundaries. Rejected: shrinking the fixture hides the production-size regression; increasing a handshake deadline leaves the quadratic work. Verify `http`, `http_header_wait_stdio` (including the real 32 MiB child input), Agent/stdio/model-account neighbors, `test_native_http_policy`, `test_native_http_write_budget` with its explicitly built native probe, and the installed v4 policy fixture. `system_native_http_write_budget_20261016.json` records 176 passing cases and a release framing diagnostic: 16 MiB median 5821 ms → 36.5 ms across three interleaved pairs. This diagnostic includes startup, input/JSON parsing and configuration refusal; it does not grant whole-system performance or production-default admission.

`lib/model_routing` owns eligibility and route ordering; `llm_dispatch` executes request-scoped slots with bounded attempt/retry mechanics. No caller, provider adapter, OAuth bridge, or local engine may create a second ProviderConfig/BYO/alias selector. Ordinary calls may prefer a Provider but cannot hard-lock a Connection or Deployment. Pricing-tier tags describe cost, never protocol support: chat eligibility removes them before classifying operational capabilities, and catalogue pricing refresh strips stale tier tags from embedding/image/audio-only models.

Configured enablement and runtime health are distinct. A route-missing verdict excludes only its Deployment, network failure its Connection (dispatch holds an unreachable endpoint for the call instead of learning health; see "Unreachable endpoints"), 401/402 its Credential, and 403 the Credential×Deployment authorization. Pending identity is reachable only through its explicit Provider+Offering reference; stale Offerings are excluded from automatic selection. Request-name access is per-wire within a credential's grant via `excluded_wire_model_ids`; probe evidence never overrides switches or credential/model grants. Once every route for an official Model is exhausted, compatible-model fallback remains subject to the same capability, context, protocol, and explicit price limits. Health penalties and retry decisions must be reasoned from typed failures. Cancellation, user abort, and deterministic request errors are not provider health failures. Local request construction/projection failures are typed before provider ingress, release any reserved slot neutrally, and never trigger model fallback, pool rescue, provider-health penalties, or upstream-attempt budgets; the in-dispatch exceptions are the credential-source outage below and, inside a non-strict pick only, a route that cannot represent mandatory native continuity (excluded for the call; see "Fallback continuity attribution"). Deterministic HTTP 400/404/422 rejections surface on the selected model — no configured fallback, pool-wide rescue, or caller-level translation replay. One bounded exception: a 404 on a subscription-OAuth route is absorbed by at most `SUBSCRIPTION_404_MAX_RETRIES` same-route transport retries (2026-09-02 chatgpt.com codex backend flapped per-request 404s for minutes while endpoint, token, and payload were healthy); the absorption is not a model switch, consumes no fallback budget, and a persistent 404 still surfaces request-scoped once the budget is spent. Keyed-gateway 404s are never absorbed — there the status really means the wire model ID is unknown. Surfaced 404 envelopes classify as kind `not_found` (distinct from `bad_request`) so the user-visible title stops claiming HTTP 400. An explicit 400 denying any route for a wire model ID, or the gateway's explicit protocol-unsupported 404 (`GATEWAY_PROTOCOL_UNSUPPORTED_MARKERS`; see `docs/MODEL_REGISTRATION.md` "Health and traceability"), is catalogue/routing evidence, not a payload rejection: dispatch excludes that ID durably for the call and process-locally until dispatcher rebuild/config or catalogue refresh, so the 60-second transient reset cannot resurrect it. Route-missing catalogue noise never replaces an actionable error from a provider-reaching route; among different payload 400s, exhaustion still re-raises the first, not the last fallback's. A gateway 5xx no-channel verdict is availability evidence for the same Deployment, held and re-probed instead of durably excluded (see "Gateway no-channel 5xx" below). A retry may not duplicate a completed tool side effect. A 401/403 body claiming a missing API key or authorization header while the final outbound headers contain a non-empty credential is a credential-delivery contradiction: it is gateway-class, never a permission exclusion/key-health failure, and sync/async/non-stream dispatch stops it after four actual responses. After its one forced refresh opportunity, a typed HTTP 401 on an OAuth slot excludes that credential's whole provider key for the dispatch call because every sibling model shares the same bearer token; HTTP 403 remains pair-scoped because model entitlement can differ. Pool rescue is still non-strict, but softly prefers the configured default model before score-ranked catalogue alternatives and widens only when that default is unavailable or already failed.

Credential-source outages — Problem: one unreadable subscription login source (for example a missing explicit `CODEX_HOME`: `codex_home_unavailable` / `codex_command_unavailable`) fails every slot bound to that credential before provider ingress. Nine pooled Codex slots shared one credential, so a cheap call that picked one failed outright, and every later request rediscovered the same outage. Decision: `LocalRequestPreparationError(provider_reached=false)` caused by `SubscriptionCredentialUnavailableError` or `SubscriptionLoginRequiredError` (missing/expired sign-in, learned with reason `credential_source_unavailable:sign_in_required`; it never earns same-source retries) is a credential fact, not a body bug (`lib/llm_dispatch/_credential_outage.py`). The sync/async stream and non-stream loops release the slot neutrally and exclude, for the call, every slot sharing the source: same key, owner-scoped `route_credential_id`, or the same outbound OAuth resolver. They spend no hard attempt and keep picking under the unchanged capability/prefer/strict/pin constraints; strict_model never switches logical model. When nothing else qualifies, exhaustion re-raises the original typed error object that shared credential recovery and summary evidence consume. The fact is recorded as `credential_source_unavailable` (credential scope) in the credential owner's `owner_health_registry` (explicit owner+tenant, LRU-bounded, process-local; a request never teaches another owner's table). Only a request whose positive owner equals the slot's routing owner learns, consults or clears that slot's fact; an ownerless request keeps only the per-call exclusion, so it can never cool a shared login for every owner. Failures observed while the credential still cools (credential-recovery retries, a demoted slot tried again, concurrent requests) are the same outage: the first cooldown stays 300 s and doubles only when the outage outlives it. Later calls exclude a cooled credential only while another eligible slot remains. When nothing else qualifies, the cooled credential is still tried, so its typed error and the re-auth/retry path survive, and a verified provider success clears the fact. Every other preparation failure keeps the immediate re-raise above. Non-stream `chat()` wraps subscription bearer resolution in the same typed `LocalRequestPreparationError(stage='wire_projection')` boundary as streaming `prepare_request`; untyped, a missing login or credential source reached `dispatch_chat`'s generic handler, which charged slot health and excluded the model for a request that never left the process. Minting stays health-blind for the same reason: auxiliary summary selection alone filters cooled credentials, its minted plan is authoritative, and dispatch is the only place a summary's credential outage is learned (`docs/CONTEXT_BUDGET_POLICY.md`). Alternatives rejected: slot `cooldown_until` (strict callers would wait out the backoff instead of receiving the typed error, and slot state is not owner-scoped); retrying sibling slots one by one (N identical local failures); rotating on every preparation failure (replays deterministic body bugs and charges fallback/health); a durable or provider-global flag (survives repair and crosses owners); a second, summary-level recorder (one outage counted four times, a 2400 s cooldown); leaving sign-in on the immediate re-raise (2026-10-24: an expired Codex login whose refresh was rejected was never learned, so it stayed the top-ranked auxiliary summary route and every compaction retried it). Specs: `tests/llm_dispatch/test_credential_source_outage.py`, `tests/model_routing/test_route_health_owner_registry.py`, `tests/compaction/test_summary_credential_health.py`.

Gateway no-channel 5xx — Problem: New-API's distributor answers HTTP 503 `{"error":{"code":"model_not_found","message":"分组 G 下模型 M 无可用渠道（distributor）"}}` when no enabled channel serves a wire model in the key's group: the model is unprovisioned or every channel is disabled, and auto-disable can make that transient. Filed as a whole-gateway outage, a strict provider-pinned task (2026-10-25, e39cc4d9) re-probed that route every 0.5 s for 50 minutes (~700 requests), logged it as `429 rate-limited`, never tried the group's other route for the same model and showed only `Upstream error`. Decision: `_classify_http_error` raises `ModelRouteUnavailableError` for a 5xx only on the exact structured code `model_not_found` or the exact distributor phrase, never on the broad HTTP 400 `ROUTE_MISSING_MARKERS`. It stays a gateway-class `RateLimitError`, so every escape path keeps the owner's waitable, no-model-switch 5xx policy (2026-08-18, 2026-08-20). The sync/async stream, non-stream and race loops release the slot neutrally (no slot, key-stats or credential health), and `lib/llm_dispatch/_route_unavailable.py` holds every slot that sends that wire model to the same provider endpoint, so another eligible route or model serves at once. When only held routes can serve, the loop sleeps abortably until their next probe on the fixed `ROUTE_UNAVAILABLE_BACKOFF_S` schedule (2, 5, 10, 20, 30 s, then every 60 s within one call), so the turn never fails on the user's behalf and continues on the first served probe; a hold also lapses at its probe time, so a long call notices recovery while another route cools. A verdict counts as one upstream response (explicit `max_429_attempts`, the opt-in `TOFU_GATEWAY_OUTAGE_BUDGET_S` streak) but spends no hard attempt. The fact is learned at Deployment scope (`classify_failure(kind='model_not_found')`) in the slot routing owner's `owner_health_registry` under the credential-outage owner rule, where health-aware planners such as auxiliary summary routing demote it, and a provider success on that Deployment clears it. Retry notices carry the reason `Model route unavailable: <gateway message>` (request id dropped, at most 160 characters; the prefix is `ROUTE_UNAVAILABLE_REASON_PREFIX` in `lib/llm_dispatch/retry_i18n.py`). `retry_phase_fields` maps it to `stream.phase.retryRouteUnavailable` with `{model}`, `{attempt}` and the verbatim gateway `{message}`, so the HUD names the cause in the UI language and never shows the rate-limit chip; the legacy `detail` string is unchanged. Alternatives rejected: 0.5 s cycling (futile load and no failover); the generic `stream.phase.retryReason` copy (its `{reason}` is the English dispatcher token, so the zh HUD read `重试中…Model route unavailable: …`); a nested `reasonKey` with arguments (the live HUD resolves a nested `reasonKey` without arguments, so `{message}` would render unfilled); the process-wide `mark_model_route_missing` disable (the verdict heals, but recovery would need a dispatcher rebuild); per-key holds (every key re-proves one group's verdict; keys of one provider in different gateway groups are therefore held together); a launch knob for the schedule (a fixed retry policy, not tuning); failing strict turns fast (overrides the owner's waitable policy). Spec: `tests/llm_dispatch/test_dispatch_route_unavailable.py`.

Unreachable endpoints — Problem: `EndpointUnreachableError` is positive connect-phase evidence (DNS resolution, refused or unreachable host, TCP/TLS connect timeout; see "Transport evidence") that no connection to a network endpoint opened, so no provider received the request. Dispatch treated it as slot health: it cooled the slot for 30 s (`TOFU_UNREACHABLE_COOLDOWN`), excluded the Credential×Deployment pair and spent a hard attempt. On 2026-11-04 a DNS failure (`gaierror`) of the shared gateway host lasting about 30 s failed three keys of the primary model within one second and three more of the configured fallback behind the same host, and the turn settled as an error while other conversations resumed when DNS returned. Decision: the failure is a request-local outage of the endpoint, not of a credential or model. `lib/llm_dispatch/_endpoint_outage.py` holds every slot whose `base_url` (else the global default) names the same host and port (paths ignored, default ports normalized), and the failure policy releases the slot without slot health, cooldown or key-stats evidence. Slots on another endpoint (a later rank of a pinned group, or another model when non-strict) serve at once. When only held endpoints can serve, the sync/async stream and non-stream loops sleep abortably until the next probe on the `ROUTE_UNAVAILABLE_BACKOFF_S` schedule shared with no-channel holds (2, 5, 10, 20, 30 s, then every 60 s), send one probe request, and continue the turn on the first served probe; holds lapse at their probe time. A caller without an explicit upstream budget, such as the primary task call, therefore waits until the network recovers or its owner presses Stop. Each of its observations opens or extends the gateway-outage streak, so the streaming loops' opt-in `TOFU_GATEWAY_OUTAGE_BUDGET_S` bounds the wait exactly like a 5xx storm, and a success clears the streak. A caller with explicit `max_429_attempts` (configured fallback, pool rescue, auxiliary work) spends one hard attempt per observation and receives the typed endpoint error once `max_retries` is spent. Retry notices keep the reason `Endpoint unreachable` and number the observations (hard attempts for budgeted callers). The racing dispatch also releases an unreachable slot neutrally. Alternatives rejected: rotating sibling keys behind the same host (each re-proves one dead path and spends an attempt); a longer connect timeout (delays failover to a live endpoint); switching models behind the same host (same path, and the fallback's failure replaced the primary cause); a process-wide circuit or slot cooldown (strict callers inherit a backoff another request learned, and slot health blames credentials for the network); a launch knob for the schedule (a fixed retry policy, not tuning); failing strict turns fast (overrides the owner's waitable policy). Spec: `tests/llm_dispatch/test_dispatch_endpoint_outage.py`.

Authority refusals — Problem: an execution-account observer can refuse a model request at `mark_model_request_dispatched`, before provider ingress (for example a Coordinator request whose human source changed). It raised a builtin `PermissionError`, which the stream loops' generic branch charged to slot health and the fallback layer replayed on other models: on 2026-10-29 one refusal became six failed/retried requests across three keys and kimi-k3, and the turn blamed the last model. Decision: such refusals raise `ModelAdmissionRefused`, an `AdmissionRefused` subclass of `AbortedError` (`lib/llm/llm_errors.py`), as `ToolAdmissionRefused` already did for tools. Every dispatch loop releases the slot without health evidence and stops, `_llm_call_with_fallback` never falls back, and the round loop re-raises any `AdmissionRefused` instead of recording a user Stop, so `handle_task_fatal` settles the turn failed with a non-retryable `permission` envelope whose own title and hint say that no provider was contacted. A refusal that coincides with Stop stays a plain `AbortedError`. Alternatives rejected: a dedicated branch in each dispatch loop (sync, async, non-stream and WebSocket paths would each need one, and any untyped refusal would still rotate); `LocalRequestPreparationError` (it marks unsent body defects and routes credential-source outages, not authority); a new envelope kind (a closed-enum and browser i18n contract change for text the existing `permission` kind carries verbatim). Specs: `tests/llm_dispatch/test_dispatch_admission_refusal.py`, `tests/tasks_pkg/test_tool_admission_refusal_verdict.py`, `tests/project_mod/test_coordinator_request_budget.py`.

Pinned route readiness — Problem: a request-scoped v2 group (`provider_pin`) orders its slots by the compiler-owned `route_rank`, and `LLMDispatcher._pick` returned `min(pool, key=route_rank)` on the stated assumption that cooldown was already filtered. It was not. Candidate assembly filters capability, chat compatibility, exclusions (no-channel route holds, unreachable-endpoint holds and credential outages arrive as `exclude_pairs`/`exclude_keys`), admin availability, key health and the pin, and the shared-contention gate narrows the pool; cooldown exists only as `Slot.score() == inf`, read by the score ordering, the sticky branch and the strict check. A cooling rank 0 therefore shadowed every ready sibling: strict picks returned `None` and polled the cooldown out (an unbounded implicit hold on a cooling warm key, despite the warm-key hold ceiling), non-strict picks sent into the cooling slot, and after a no-channel hold the next cooling rank kept healthy lower ranks idle. Decision: the rank ordering applies readiness first. Among ready slots the lowest `route_rank` wins regardless of score; with every route cooling, rank order names the fallback, so strict callers wait and non-strict callers keep their unchanged all-cooling behavior. A ready warm (affinity) slot still wins over rank, a cooling one rebinds to the best ready rank, and the pinned-scope warm-key hold (`sticky_cooldown_remaining_s`) still waits out a short cooldown before the pick. Alternatives rejected: filtering cooldown out of the candidates (changes unpinned non-strict semantics, where a cooling preferred model would fall back to another model, and the all-cooling strict wait); a re-pick in each dispatch loop (duplicates selection policy); ordering pinned groups by score (overrides the compiler's provider/deployment preference). Spec: `tests/llm_dispatch/test_pinned_route_readiness.py`.

Slot pool scopes — Problem: every task mints a request-scoped route group of one slot per eligible Deployment (45–59 in production; [MODEL_REGISTRATION.md](../MODEL_REGISTRATION.md) keeps every eligible candidate), so about 1,600 slots are live. `LLMDispatcher._pick` held the process-global dispatcher lock while walking every live slot and reading key health for each (a key-store `stat`, which releases the GIL) before it tested the provider pin, and the per-attempt stream debug line scored the whole pool under the same lock even when debug logging was off. Mints and disposals wait for that lock. With CPU-bound threads a pinned pick took seconds and a group mint tens of seconds (`benchmarks/dispatch_contention.py`). An unpinned pick could also select another request's slot and use that request's credentials, although `context_policy.py` and auxiliary summary routing already treat such a slot as unavailable without a pin, and the `TOFU_DISABLE_CONFIGURED_SLOTS` mode exists so that an unpinned dispatch fails loudly with no slot. Every request-scoped group is dispatched under its own pin (HTTP routes through `_pinned_provider_id`, capability and auxiliary routes through `provider_pin`). Decision: `LLMDispatcher.slots` is a `SlotPool` (`lib/llm_dispatch/slot_pool.py`) indexed by dispatcher `provider_id`. A pinned context selects only `scope(pin)`: its route group, or one configured provider for an auxiliary provider pin. An unpinned context selects only configured slots, so a request-scoped slot, whose credentials and lifetime belong to the minting request, never serves another caller's unpinned dispatch. Every selection API (`pick_slot`, `pick_and_reserve`, `pick_top_n`, `pick_best_slots`, `pick_key_for_model`, `has_capable_slots`, `cooling_cause_summary`, `sticky_cooldown_remaining_s`, `responses_state_binding`, `summarize_slots`) uses that one scope, and `mark_model_route_missing` reads the provider index. A pick applies pool-state filters cheapest first and reads key health last, at most once per slot of its scope. It checks key-store freshness once (`key_admission_batch`), before taking the dispatcher lock, and then reads key health under that lock, where no store `stat` runs any more. Reading it before the lock was measured and rejected: concurrent picks interleave one key_stats-lock acquisition per slot, and under GIL pressure that lock convoys (pinned picks rose to about 30 s in the benchmark); serialized under the dispatcher lock the reads are uncontended and the critical section is about 0.25 ms for a 52-slot group. The strict capacity probe uses the same batch and reads the credential-health identity the recorder writes, which also fixes request-scoped slots that it used to probe under their pin identity. The stream loop builds its slot summary only when debug logging is enabled. Each candidate's last-resort siblings come from the pool's per-namespace key-health credential index, which publication and withdrawal update in the same critical section ([auth_providers_billing.md](auth_providers_billing.md), "Key-health siblings"). Generic list mutation of `dispatcher.slots` rebuilds the index; assignment copies into a new pool. Every pick records `tofu_llm_route_selection_seconds{scope,outcome}` (dispatcher-lock wait included), so agent and background threads report selection latency that `[Translation:timing]` shows only for translation. Alternatives rejected: excluding request-scoped slots only in `_pick` (the other selection APIs would still disagree with it); a per-group pool object owned by the ephemeral registry (two sources of truth for pool membership); caching key verdicts across picks (a staleness knob that delays a recorded billing stop). Specs: `tests/llm_dispatch/test_slot_pool_scopes.py`.

Route group publication — Problem: a route group published each candidate's slot through its own `with _lock, dispatcher._lock` acquisition (45–59 per task), so registration queued behind concurrent picks once per candidate; a production 1.86 s gap between two mints matched one concurrent pick. Every mint also re-ran the endpoint checks (egress validation resolves DNS in `validate_egress_url`; a self-hosted endpoint is TCP-probed) although a group spans a few Connections. Disposal removed slots one `list.remove` at a time, comparing the ~46 dataclass fields of each earlier slot, under one lock acquisition per slot. Decision: `mint_routed_slot_group` prepares every candidate without a lock (`prepare_ephemeral_slot`), sets route metadata and rank, then `publish_ephemeral_slots` registers and publishes the whole group in one dispatcher-lock critical section, in rank order, so a concurrent pick sees none or all of it. A group-local `endpoint_verdicts` map checks each distinct base URL once; a failed check skips only that Connection's candidates. `dispose_ephemeral_slots` withdraws a group in one pass by identity, leaving every other slot in order, then attempts every proxy-exemption release and re-raises the first failure; plaintext is cleared even when detachment fails and the group stays retryable. A prepared handle that never reached publication is retired by the same call, so a failed mint returns every lease. Every eligible candidate is still minted. Alternatives rejected: capping the group (contradicts the all-candidates registration contract); a process-wide endpoint-verdict cache (would outlive the DNS changes the use-time SSRF check exists to catch); per-slot publication under a cheaper lock (still one contended acquisition per candidate). Specs: `tests/llm_dispatch/test_ephemeral_capacity.py`.

Configured-pool rebuild — Problem: `initialize()` rebuilt the configured pool (owner authority read, secret resolution, benchmark file) while holding the dispatcher lock, so a settings change or health rebuild stalled every pick, mint and disposal for the storage round trips. Decision: one builder at a time (`_build_lock`) builds the configured slots and logical index off the dispatcher lock; `_build_slots` returns them instead of writing the live pool, and `_load_benchmark_data` seeds only that list. One critical section then swaps them in with `replace_configured`, keeping every request-scoped slot live at that moment. Callers that need the configured pool wait on the build lock; mints and disposals do not. A reload that arrives during the build (a generation counter) publishes the finished build and leaves the next call to rebuild, matching the old order where a reload right after a build forced the next one. A failed build still publishes an empty configured pool and retries on the next call. Alternatives rejected: double-buffering two dispatchers (live slot objects and request reservations must survive reloads); building under the lock with a timeout (a knob that still stalls). Specs: `tests/llm_dispatch/test_dispatcher_reload_lifecycle.py`.

A typed shared-project TPM/RPM or app/model RPM 429 is external contention, not a key/model failure; the first rejection arms a process-local family gate. Every later sync/async task reserves after local cache gates: a continuing rejection streak spaces provider/model probes at 1, 2, 4, 8, then at most 15 seconds; deep queues recheck in abortable three-second slices. Automatic unpinned work selects the eligible family whose probe is due first; explicit provider/model boundaries remain authoritative. Reconstructible callers may opt into immediate-only admission: a due probe reserves atomically, while a still-blocked family returns typed `request_not_dispatched` without advancing the clock; durable/user-facing sync and async calls keep waiting by default. Slots stay healthy and eligible without evicting warm keys; waits are metered, two drained successes clear the gate, quiet entries expire from a 256-family table, and live bounded gate state is exposed in dispatch status.

Latency policy: `TOFU_CACHE_SETTLE` and `TOFU_CONV_STICKY_HOLD` default off; `=1` opts into bounded cache-cost holds. Problem: speculative cache waits delay eligible requests, including human follow-ups across Turn boundaries. Decision: send immediately, retain stable prefixes, sticky routing and cache diagnostics, and skip disabled-policy prompt scans. Alternatives: shorter unconditional sleeps still add unproven delay; removing affinity loses free cache reuse. Mandatory provider backpressure, cancellation and durability remain. More uncached input may cost more; no upstream TTFT improvement is assumed. `benchmarks/send_startup.py` measures local overhead only. Route preparation resolves/decrypts each `(secret_reference, kind)` once within one admitted owner-scoped group, including known invalid/missing verdicts; every new group re-resolves for rotation and ownership. Storage/authorization failures still abort and release handles. Alternative: global plaintext caching weakens revocation. Verify with `tests/model_routing/test_route_credential_materialization.py`. When explicitly enabled, the Codex subscription settle profile follows OpenAI's [documented overflow routing above 15 requests/minute](https://developers.openai.com/api/docs/guides/prompt-caching): starts for one `prompt_cache_key` stay 4.2 seconds apart. Cold cacheable requests and warm uncached tails of at least 8,192 tokens arm the extra five-second visibility window; smaller warm tails proceed after the send interval, with continued growth eventually restoring the hold. `TOFU_CACHE_SETTLE_CODEX_WARM_WRITE_TOKENS` tunes the threshold; values below the 1,024-token cacheability floor use the safe default. Codex cache-health diagnostics retain only the prior wire message count plus a process-local digest inside the existing one-hour/4,096-entry bound; each new observation hashes its prior-length prefix to prove append-only growth, and one legacy rich entry migrates without losing evidence. Anthropic policy is independent. Auxiliary local-L2 summary prompts bind a separate opaque owner/conversation affinity to slot selection and their stream body: they neither inherit nor update the parent settle clock, repeated summaries keep a stable Codex session/cache route, and both identities remain inside the existing TTL/capacity bounds. The generic settle profile arms its 1.5-second visibility window only when the completed round proves a metered cache creation, has cold/missing cache telemetry, or has an unmetered warm suffix of at least 4,096 tokens. An explicit Anthropic `cache_creation_input_tokens=0` proves there is no new entry to settle. A smaller automatic-cache suffix can reuse the older visible prefix and proceeds immediately, bounding any extra reprocessing to 4,095 tokens instead of holding every fast tool-loop round; `TOFU_CACHE_SETTLE_WARM_WRITE_TOKENS` tunes that independent threshold. Missing or malformed usage remains conservative, and positive/cold writes retain the existing wait.

Stateless translation dispatch drops conversation affinity for the call (`conv_affinity_cleared` around every engine dispatch in `lib/translate/engine/_engine.py`): each payload is fresh, with no warm prefix worth preserving. The picker stays score-based, never engages the warm-key hold, and a 429 rotates directly to the healthiest eligible slot rather than waiting on the conversation's sticky key. The call neither records nor rebinds that key; the worker thread's binding is restored on exit.

Retry execution and observability have separate budgets. Primary attended dispatch may rotate through ordinary transient 429/cooldown states until recovery or abort; configured fallback and pool rescue own a finite actual-response budget (default 3, task override clamped 1–16), so failure settles rather than leaving the client in `retrying`. Each LLM round persists power-of-two samples for at most eight coarse signatures (16 each, 128 frames total); suppressed samples still refresh liveness without unbounded `storage_events` or `storage_attempt_events` growth.

An all-slots-cooling callback carries a typed current-wait status; pool polling is not an upstream request attempt and never increments a retry count. A real 429 or failed provider attempt may emit `retrying` with its actual attempt number. Current-attempt waiting uses `waiting_model`; a stream that has emitted model work and is currently transport-idle uses `stream_stalled`. Retry callbacks identify the physical attempt model/provider and whether dispatch is strict or pool-wide; the logical requested model is not reused as the label for a different rescue candidate.

Each task-owned logical dispatch opens `model_request_start` before waiting and closes `model_request_complete` on success/failure/abort; scoped dispatcher children, monotonic clocks and coverage follow [TURN_TRACE_CONTRACT.md](../contracts/TURN_TRACE_CONTRACT.md). These diagnostic provider/model/failure facts feed the bounded activity timeline, not Turn settlement or model messages. An allowed configured fallback emits `model_fallback` before its replacement starts. Deterministic 400/404/422 rejections close the selected-model span as failures, never provider unavailability.

Network route health settles on the complete stream, not response headers. Credential-free `routeId`, `routeMode`, selection reason, and failure stage accompany request completion/usage diagnostics. A mid-stream close fails the concrete path and slot; the next stream retry avoids that slot on its **first** pick when another slot for the user-selected model is eligible. A stream-idle timeout is transport failure: the selected route delivered no event for the full window. Malformed provider frames, empty responses, and missing tool payloads penalize the provider slot, not a normally transporting route. User abort is neutral to both health systems; each reserved slot settles exactly once. Explicit bypass rules remain authoritative and report `direct:configured-bypass`, never a proxy timeout.

Stream-idle timeouts use the existing bounded premature-close retry budgets. A zero-byte stream that closes fast keeps the large zero-byte budget, while one that consumed at least 60 s of the rolling idle window is an upstream stall: it gets two alternate-slot retries and stops unattended whole-Turn replay, because each replay would wait another full window. Heartbeat-only EOF and short reasoning prefixes without a provider finish recover even on round zero, using capped, jittered, abort-aware backoff and same-model slot rotation; pings never grant an immediate retry or prove zero billed tokens. Chat, Anthropic and Responses errors inside HTTP 200, SSE or WebSocket preserve structured code/type/status and quota/reset details through `llm_errors.provider_error_http_status` and the shared HTTP classifier; explicit gateway status wins. Responses top-level `error` is terminal failure; `error:null` is not. WebSocket preserves typed errors; provider rejection does not penalize HTTP network-route health. Unknown provider errors remain explicit application failures, not invented socket errors. The smaller semantic-stall bucket only renders/recovers legacy records; no live transport enters it. Manual Retry remains available; other truncation signatures retain their own caps.

### Logical model identity for knowledge and billing

Problem: selected-slot adaptation built bodies, clamped output and stamped `usage['_dispatch']` with the Deployment's wire id. Exact-key knowledge misses decorated or opaque gateway spellings: `_clamp_max_tokens('kunlunbedrock.gpt-6.1-sol-m17')` returned 32,768 instead of 128,000, an opaque Claude wire (`yuju-opus-55-evaDaily`) skipped the prefill guard, and paths that priced from `_dispatch.model` (child-round snapshots, L2 summary calls and receipts, compaction rows, paper meters, pool rescue) fell back to `default_estimate` (USD 1.9125 instead of 0.50 for identical usage). Anthropic 1-hour cache writes were billed at the 5-minute 1.25x rate. Decision: every `_dispatch` producer (stream settlement, `dispatch_chat`, race dispatch) stamps `logical_model` beside the wire `model`; pricing consumers and call rows prefer it, and run evidence keeps it. Adaptation passes `knowledge_model=slot.logical_model` to `build_body` and uses it for output clamps, reasoning compilation, Claude/Gemini message tweaks and the `pinned_context_profile` knowledge fallback and output cap; non-stream `dispatch_chat` and race dispatch forward the same `knowledge_model` through `chat()`, which also seals the request with `logical_model=knowledge_model` (kept by the non-stream budget re-seal), so request settlement prices the logical model on both paths instead of the wire alias's `default_estimate`, which strict billing rejects as `IncompleteRequestUsage`. `build_body` and stream adaptation clamp with `_clamp_route_max_tokens(..., wire_model=)`: the logical id's family ceiling plus only the wire spelling's per-name learned limit, so a limit learned where a Deployment's wire id equals the logical id never clamps another gateway's spelling. `body['model']`, route-limit keys, learned context windows, exclusions/health, transport cache markers and selected-slot input admission stay on the wire id. Transport re-admission matches `_route_context_model` against `body['model']`, so switching admission would drop the declared route window. `_convert_usage` carries Anthropic's `cache_creation` TTL breakdown on stream and non-stream paths; `compute_cost` prices the 1-hour share at the row's `cacheWrite1hMul`, else 2.0x base input for Anthropic-family rows on the official 1.25x write schedule ([pricing](https://platform.claude.com/docs/en/about-claude/pricing)), clamped to the reported write total; a row with its own write multiplier keeps it for both TTLs. Usage without the breakdown is priced unchanged. Alternatives rejected: adding wire-spelling rows or another prefix-strip normalizer keeps exact tables growing and still misses opaque ids; rewriting `_dispatch.model` to the logical id would break wire-keyed exclusion, rotation and diagnostics; billing every cache write at 2.0x over-charges 5-minute writes; applying the logical id's learned limit alongside the wire's imports another Deployment's evidence. Specs: `tests/llm_dispatch/test_dispatch_logical_identity.py`, `tests/cost/test_cost.py::CacheWriteTtlCostTest`, the TTL cases in `tests/llm/test_anthropic_outbound.py` and the logical rescue case in `tests/tasks_pkg/test_pool_rescue_fallback.py`.

## Task image preparation budget

Text-only preparation skips hydration/pinning and preserves references. First vision preparation performs an owner-scoped bounded source read, retaining original unresized bytes/references for fallback; only fresh wire bodies resize. `lib/llm/body/_image_reuse.py` is an explicit task-owned derived-wire LRU: lean defaults **8 entries / 4 MiB**, hard ceilings **32 / 16 MiB**, counting URL storage and per-entry overhead. Both limits apply; oversized entries bypass retention. Keys bind exact source-URL SHA-256, model, dimension cap, transform version, quality/format, metadata policy and base64 byte budget. Owner mismatch fails closed; terminal heavy-field release calls `clear()` and releases `_modelImageReuse`. No original replacement, file writes or cross-owner cache. `tests/llm/test_image_preparation_reuse.py` gates zero repeated Pillow work, byte-identical wire/source preservation, eviction, invalidation and owner/text-to-vision boundaries; `test_release_heavy_task_state.py` gates terminal release. The synthetic fresh-original benchmark measures local body preparation only, never end-to-end/provider gains.

Claude image limits (`lib/llm/body/_images.py`, facts from https://platform.claude.com/docs/en/build-with-claude/vision as of 2026-10-23): Claude generation 4.7+ in any line (`claude_line_version`) gets a **2,000 px** long-edge cap, every other Claude model **1,568 px**, and every image at most **5 MB of base64** (Amazon Bedrock; the Claude API allows 10 MB). A re-encode keeps any image with transparency data (RGBA, LA, palette or colour-key) as RGBA PNG and turns other modes into JPEG q85, shrinking until it fits; it never composites alpha onto a matte, since black (the former JPEG flattening) erased black line art, white would erase white text and logos, and shrinking always fits PNG into the budget. Problem: one 1,568 px cap justified by "the model never sees more" discarded detail 4.7+ models resolve (their high-resolution tier reaches 2,576 px), and an image within the cap but over 5 MB of base64 reached Bedrock unchanged. Decision: per-model caps that never depend on image count, so an image is re-encoded at most once and stays byte-identical across rounds. Alternative rejected: 2,576 px for 4.7+ — a request with more than 20 images must keep every image at or under 2,000 px, so the 21st image would retro-shrink all earlier ones and miss the prompt cache. `tests/llm/test_image_downscale_uniform_cap.py` gates tiers, count independence, idempotence and the byte budget.

## Invariants
- One canonical body builder and one transport policy.
- Provider differences stop at focused translation modules.
- Streaming and non-streaming paths project the same terminal meaning.
- Requested ModelRef, Provider preference, and actual ProviderAccess/Offering/Deployment/Connection identity survive every round in RouteSnapshot.
- Secrets stay in outbound headers and redacted diagnostics.
- Chat and non-chat HTTP execution carry an explicit owner to v2; process-global environment credentials are a direct-library compatibility seam only.
- HTTP adapters never call model dispatch, provider pins, or slot reservation; an AST gate requires task execution or a declared application service, and log compression/compatible embeddings carry finite input, deadline, and admission budgets.
- Every network loop is cancellable/bounded; local health/discovery share one monitor with empty-result backoff.
- A started task-owned chat or `/api/v1/chat/stream-direct` dispatch is execution-owned: frontend/SSE, Sidecar, push/webhooks, presence and DB abort polling cannot block/cancel ingress; explicit Stop, upstream verdicts/deadlines and runtime/process failure remain termination boundaries. Direct relay work has a 600-second default/900-second hard request deadline and a finite launch-profiled production 429 allowance, so a detached observer can never retain admission indefinitely.
- Sync and async dispatch both fence every discarded transport or slot attempt. Task-backed projections may retract through their attempt-aware event state; OpenAI-compatible direct SSE retries only before the first visible delta and otherwise terminates honestly because that protocol cannot retract bytes.
- Caller deadlines propagate through dispatch rotation; an expired background request cannot fall through to a fresh direct-provider call.
- Dispatch remains indefinitely user-cancellable by default. Optional callers and every configured fallback/rescue may cap upstream 429 attempts; only provider-reaching requests count, exhaustion is typed and terminal, and `smart_chat` cannot bypass the budget through direct fallback. The synchronous language micro-classifier uses one attempt plus a 512-entry, process-keyed opaque-digest LRU; it caches only valid language codes and retains no prompt text.
- Retry/wait telemetry is hard-bounded per LLM round independently of the user-abortable loop; liveness updates are never sampled out.
- Each task-owned model request has one correlated start/complete diagnostic span, and every allowed model fallback has an explicit decision event.
- Usage/cache fields survive normalization; Codex cold writes, material warm tails, and per-key routing pressure retain separate controls, so skipping a small-tail hold cannot disable the send interval.
- Retries are bounded, observable, and do not reinterpret programmer errors.
- Locally derived payload pressure is recovered locally: compact first, retry the same model, and never treat cgroup headroom as a model-fallback signal.
- Official Model facts and Provider Offering facts remain separate; a missing remote directory row marks an Offering stale rather than deleting it.
- Model knowledge and billing key on the Offering's logical model; the wire id remains the request, route, health and admission identity.
- One Provider-scoped wire ID names exactly one Deployment; aliases and request-ID pools are migration input only.

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
pytest -q tests/llm/test_reasoning_contract_compiler.py tests/tasks_pkg/test_reasoning_retry_lifecycle.py tests/llm/test_inference_local_dispatch_errors.py
pytest -q tests/model_routing/test_route_inference_contract.py tests/llm_dispatch/test_discovery_inference_profiles.py tests/model_routing/test_model_reasoning_controls.py
pytest -q tests/llm/test_protocol_conversion_checked.py tests/llm/test_protocol_conversion_contract.py
pytest -q tests/llm/test_reasoning_controls_app_browser.py tests/application_shell_fragments/test_thinking_depth_slider_browser.py
pytest -q tests/sse/test_sse_framer.py tests/sse/test_sse_core_parity.py tests/sse/test_sse_terminal_boundary.py tests/sse/test_sse_terminal_cleanup.py \
  tests/sse/test_sse_diagnostic_payload.py tests/llm/test_anthropic_reasoning_effort.py
pytest -q tests/llm/test_llm_transport_connection_reuse.py tests/llm/test_llm_idle_stream_timeout.py tests/tasks_pkg/test_stream_anomaly_retry_widening.py tests/tasks_pkg/test_retry_budget_envelope.py \
  tests/llm/test_responses_outbound.py tests/llm/test_responses_websocket.py tests/llm/test_anthropic_outbound.py tests/llm/test_stream_protocol_tolerance.py \
  tests/llm/test_learned_request_repair.py tests/llm/test_llm_retry_helpers.py
pytest -q tests/llm_dispatch/test_dispatch_logical_identity.py tests/cost/test_cost.py
pytest -q tests/llm_dispatch/test_dispatch_stream.py tests/llm_dispatch/test_dispatch_model_health.py tests/providers/test_provider_pin.py \
  tests/model_routing/test_model_routing_contract.py tests/model_routing/test_model_routing_capability_adapter.py tests/model_routing/test_model_routing_bootstrap.py tests/turn_lifecycle/test_turn_serving_route.py
pytest -q tests/local_serve/test_local_serve_probe.py tests/local_serve/test_local_serve_plan.py tests/local_serve/test_local_serve_process.py tests/local_serve/test_local_serve_env_store_register.py tests/local_serve/test_local_serve_tools.py tests/local_serve/test_local_serve_recommend.py tests/local_serve/test_local_autodiscover.py
```

Streaming dispatch accepts an explicit `model_budget_admit` port for message-list callers. Each selected-slot/retry body retains that same account for physical request admission and settlement. Conflicting prebuilt-body accounts fail locally; the port grants no owner, replay, or recovery authority. This is plumbing; research discovery admission remains disabled until all stage calls and durable accounting are bound.

Non-stream `chat` accepts an explicit budget admission port. Each direct/relay send has its own lease after final protocol projection; retries settle failed usage before admitting again, and learned-limit recursion retains the same account. Unknown failed usage stays unknown, while pre-send preparation can void an unsent lease. This does not yet bind discovery translation or durable goal recovery.

Non-stream `dispatch_chat` and `smart_chat` forward an explicit model budget across slot selection and direct fallback. Budget/cancellation refusal releases the selected slot and propagates without health damage or fallback. This remains a caller-owned port, not authentication or durable recovery authority.

`search_budget.content_filter_budget_scope` binds an explicit owner and admission port using the existing request-local SearchConfig. A swallowed typed budget refusal is raised at scope exit; normal filtering failure behavior remains. Only the configured host adapter may be wrapped, nested replacement is refused, and workers must finish before scope closure. Task tool callers still need to enter this scope.

Best-effort filter budgets — Problem: a nearly spent search deadline handed the gate a 1.09 s remainder that no verdict fits (1.3 s fastest of 124 measured), and each timeout produced ERROR records with tracebacks at three layers (non-stream `chat` exhaustion, the tofu-search adapter and the filter) for an outcome the caller had chosen. Decision: the filter skips a call whose budget is below its 1.5 s floor with one INFO line (cached verdicts still apply); `_chatui_llm` raises a read-budget expiry as the builtin `TimeoutError`, which tofu-search reports once at WARNING; non-stream `chat` logs read-timeout exhaustion under an explicit caller `timeout` at WARNING without a traceback and still raises it. HTTP/server failures, connect/upload/TLS timeouts and read timeouts without a caller budget keep ERROR. Alternatives rejected: a `best_effort` flag through dispatch (passing `timeout` already declares the caller's budget); making `ResponseReadTimeoutError` a `TimeoutError` (adds `OSError` to every transport `except` path); lowering every filter failure (hides outages); an adaptive latency floor (process state without evidence it is needed). Specs: package `test_content_filter_modes.py`, `tests/llm/test_transport_failure_phases.py`, `tests/search/test_search_bridge_config.py`.

Problem: `dispatch_parallel` and `smart_chat_batch` queued owner-authorized requests behind local worker counts. Decision: both use the shared demand executor; their legacy count parameters size idle reuse. Each request retains its owner, model pin, tools, payload and usage authority; genuine unstarted worker failure receives the existing per-item error shape without suppressing siblings. Empty prompt batches return an empty result. Alternatives: larger fixed pools retain queues; detached calls lose settlement and teardown ownership. Real rendezvous, caller-authority and startup-fault tests verify this boundary. Measurement: 32 requests rendezvous with idle hint 1 in 10.14 ms (`dispatch_parallel`) and 8.75 ms (`smart_chat_batch`), with zero workers after joined shutdown.

Subscription probe demand — Problem: independent network route probes queued behind a local thread count. Decision: the shared demand executor starts every eligible probe; the legacy count sizes idle reuse. Target/route singleflight, route scoring, circuit/backoff policy, reset generation fencing and cold-race waits remain. A genuine unstarted submission enters the failed-future lane without penalizing route health or suppressing siblings. Alternatives: larger counts retain queues; duplicate probes lose shared identity; detached workers lose close ownership. Rendezvous, startup-fault and late-generation tests verify the boundary. Measurement: 32 independent probes with idle hint 1 completed in 9.74 ms; joined shutdown leaves zero workers.

Template-supply probe demand — Problem: independent curated recipe cells queued behind eight local workers. Decision: the shared demand owner starts each eligible cell; the legacy worker value sizes idle reuse. A genuinely unstarted cell retains unknown evidence while sibling served verdicts still finish and merge. Owner credentials, template matching, insert-only reconciliation, deleted-model fences and registration admission remain. Alternatives: larger pools retain queues; missing probes cannot grant serving authority; detached probes lose cleanup. Rendezvous and real startup-fault tests verify delivery and evidence ownership. Request usage includes `_request_parameters`, a bounded allowlist receipt from `lib/llm/request_receipt.py` for stream and non-stream requests. It retains numeric sampling/output controls and fixed reasoning enums, never prompts, URLs, headers or tools. `reasoning_effort` is read through `lib.llm.reasoning.native_reasoning_effort`, so Claude `effort`, `output_config.effort` and Responses `reasoning.effort` are recorded too; conflicting spellings record unknown. Missing controls mean provider default/unknown, not parameter equality.

## One owner for non-stream dispatch

Problem: `smart_chat` caught routed failures and sent a new request with a legacy default credential, producing repeated HTTP 401 after slot exhaustion and bypassing owner, pin, billing and budget authority. Decision: this convenience API delegates to `dispatch_chat` and propagates its original failure. The dispatcher still owns configured model retries/fallback. Alternatives rejected: checking that a default key is nonempty still bypasses routing authority; catching only selected errors leaves a second request loop. Timer verification passes its explicit owner and live cancellation callback through this boundary; proactive polls, scheduled prompts and daily-report analysis also pass their caller-owned identity. Tests: dispatch owner propagation, nonstream budgets and timer readiness. <!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->
