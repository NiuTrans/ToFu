# Context engineering

This domain turns durable conversation/project state, memory, tool history, and model limits into bounded LLM messages, and owns automatic/user-requested compaction. Durable transcript authority is [`../STORAGE.md`](../STORAGE.md).

Model continuation bounds live in dependency-free `lib/agent_core/model_context_limits.py`.
Problem: importing storage for limits crossed core boundaries. Decision: share core policy; reject duplicated limits that drift.
`lib/agent_core/tool_errors.py` owns declaration assembly failures; core never imports repositories for exception types.

## Ownership

| Concern | Owner |
|---|---|
| Context assembly and provider ordering | `lib/tasks_pkg/context_composer/` |
| Automatic injection inventory, necessity, authority, placement, and cost policy | `lib/tasks_pkg/context_composer/registry.py` |
| Chronological/auxiliary injection writer and bounded debug receipts | `lib/tasks_pkg/context_composer/injections.py` |
| System/project/user context sources | `lib/tasks_pkg/context_composer/_providers.py` |
| Demand provider read execution | `lib/tasks_pkg/context_composer/_provider_executor.py` |
| Model context-window policy | `lib/context_limits/` |
| Token counting | `lib/token_counter/` |
| Automatic compaction | `lib/tasks_pkg/compaction/` |
| Automatic handoff identity, pre/mid-turn placement and restoration ordering | `lib/tasks_pkg/compaction/_handoff.py` |
| L2 carrier covered-Turn range and recall cursor | `lib/tasks_pkg/compaction/_coverage.py`; [contract](../CONTEXT_COMPACTION.md#summary-coverage-and-recall) |
| Required ordinary Turn acceptance scope | `lib/tasks_pkg/context_composer/turn_requirements.py`; [contract](../contracts/ARTIFACT_REVIEW.md#shared-execution-context) |
| Task-state projection / required checklist restoration | `lib/tasks_pkg/context_composer/task_state.py`, `checklist.py` |
| Persistent manual compaction | `lib/tasks_pkg/compaction/_manual.py`, `_persist/` |
| Scheduled working-view compaction (protected or busy conversations) | `lib/conversations/compaction_policy.py` marker, `_manual.py` schedule/cancel, `manager/_registry.py` wake/claim, `_pipeline.py` apply/settle; [contract](../CONTEXT_COMPACTION.md#coordinator-context-lifecycle) |
| Long-term memory, My Context, and [working facts](../contracts/WORKING_FACTS.md) | `lib/memory/`; `frontend/src/features/memory/preference-actions.ts` |
| Conversation message construction | `lib/tasks_pkg/conv_message_builder/` |
| Completed-task model view recovery | `lib/tasks_pkg/model_replay.py`, `lib/storage/model_continuation.py` |
| Cost experiments and trace | `lib/cost_experiments/cost_experiments.py`, context trace modules |
| Price tiers and per-round cost aggregation | `lib/pricing/`, `lib/cost.py` |

## Assembly flow

1. A task receives an immutable view of authenticated conversation and project state.
2. `context_composer` invokes declared providers in deterministic order behind one 15-second request deadline, records per-provider status/timing, and reuses the task-owned project-prefetch future; timed-out providers operate on detached carriers, so late completion cannot mutate the task or prompt.
3. Memory prefetch selects bounded evidence; it does not mutate the transcript.
4. The token counter resolves the model-specific counter, with an explicit heuristic fallback when no exact counter exists. Repeated per-round counts of one task's request pass the task-owned `RequestTextCounts` memo (`task['_requestTextCountMemo']`), so local tokenizers encode only texts new since that task's previous count (record: *Round-prep token counting* below).
5. Context-limit policy computes the usable input budget after output and safety reserves.
6. Compaction removes, folds, or summarizes only through registered steps.
7. The final request body is built once by the LLM body builder. Budget pressure is **telemetry only**: `context_budget` records content-free working-message estimates once per compaction window; it never appends a model message or records an intervention `system_note`. Automatic compaction and final-wire admission remain the capacity authorities.

Provider reads inherit the shared [worker startup transaction](../DEFENSIVE_PATTERNS.md#worker-startup-transactions): one ordinary physical startup fault can recover on the original unpublished Future, without replaying the callable. Successful admission does not imply immediate callable completion. Two failed attempts return an immediately terminal typed-error Future with both startup causes, not a pending read that waits for the request deadline; accepted siblings retain their ownership. Context regressions in `tests/context/test_context_provider_executor_lifecycle.py` cover bounded results, exactly-once entry, persistent refusal and joined cleanup; shared worker recovery tests retain cancellation, initializer and control-exception fences.

Tool-round continuations preserve round-zero provider/model binding and stable prefix order; providers do not silently reread mutable globals later. Responses assistant phase replay and scoped task/authorization continuity follow [the context contract](../CONTEXT_COMPACTION.md#assistant-phase-and-task-continuity); regression and budget checks: `tests/llm/test_responses_message_phase.py`.

Cache-stable prefix: runtime context never rewrites operator/user-authored base policy or canonical included tool declarations. Four registry blocks own system carriers after explicit policy and before dialogue: `platform_static` (reused unchanged, preserved by local lossy compaction), `ui_nav_directory` (browser-only, withdrawn off-surface/ablation/replace-mode without changing sibling policy), `parallel_execution` (available Spawn Agent lane), and `delegate_mode` (the narrowed-root contract that replaces generic Spawn Agent guidance). Verified legacy user-role orchestration envelopes migrate into this prefix without retaining a duplicate tail copy.
Other composer blocks are content-addressed appended user-role TAIL carriers: identical bytes keep position; changes append versions/retractions. Exception: accepted checklist recovery is visibility-driven and standalone, withdraws superseded recovery sections, and survives compaction without new notes.

- `environment` (cwd / is-git / platform / model) ships as a tail block, never inside `platform_static`, so changing the project path does not rewrite the cached prefix. The model perceives the path from that block each turn; a move appends a separately addressed one-shot event and the turn projection gains a `projectPathChange` provenance chip. Baselines are in-memory, conversation-scoped (task-scoped without a conversation), and bounded (256 scopes), so a restart re-baselines without false transitions. A composition with neither identity keeps no baseline. Problem: such compositions (the debug panel's snapshot builds a throwaway task without a conversation) shared the MCP selector's `task:anonymous` fallback scope across owners, so one snapshot's project path fired a project-path-change event in the next; the cold `/debug-messages` wire form then depended on process history and diverged from the hot build (`test_cold_hot_wire_form_byte_identical` failed under xdist after any earlier anonymous composition with a project). Decision: `_tail_transition_scope` returns no scope for an anonymous composition, for the project-path and MCP-delta baselines alike. Alternatives rejected: a fresh scope per snapshot (fills the bounded store and evicts real conversations' baselines); resetting the store in the test (hides the product divergence). Specs: `tests/tasks_pkg/test_wire_messages_fidelity.py`, `tests/context/test_context_composer.py::test_anonymous_compositions_keep_no_transition_baseline`.
- Environment-specific multi-root, remote-worktree, programmatic, and
  multi-agent routing guidance also lives in the tail. Included tool schemas
  retain their canonical descriptions; a schema budget may omit an optional
  tool but never strip annotations from an included tool.
- The wire `tools` array freezes at the session's first tool selection: MCP server disconnects/reconnects and newly discovered tools no longer mutate it. Tools that appear after the freeze are advertised by the tail `mcp_tools_delta` block (at most eight names and short capability hints, only when schema discovery is available). Complete schemas are obtained on demand through `search_tools` and tools are callable through `execute_tools`; truncated JSON is never presented as an executable schema. An unchanged reachable set reuses the existing bytes; a changed set appends a new version, and losing the last delta appends one retraction tombstone. Turns where the reachable set actually changed vs the frozen wire carry a `mcpToolsDelta` provenance chip (added/removed names, capped at 8 each).
- `mcpToolExposure='delegate'` (typed option, per-conversation resolvable) keeps EVERY MCP schema off the orchestrator's wire: `_build_mcp` returns an empty wire while `_mcpAllowedToolCatalog` stays the full execution authority, so `search_tools`/`execute_tools` still reach every tool and `spawn_agents` sub-agents inherit the complete catalog through the parent executable catalog (never the wire subset). The mode writes no selection freeze — it is a per-assembly decision, so an explicit mid-conversation exposure switch never fights stale freeze state. The `mcp_tools_delta` block is then the per-turn advertisement: it renders the whole connected catalog as names+hints and points at sub-agent `tools`-allowlist delegation. Specs: `tests/mcp_protocol/test_mcp_delegate_exposure.py`.
- Programmatic and multi-agent wire projection is latched from the task's first orchestration decision. Observed fan-out, later round numbers, retries, serial-gateway experiments, and program fallback state may update telemetry or execution behavior, but cannot add/remove tools or switch an existing tool schema mid-task. Local programmatic projection remains additive.

Preparation reuse — Problem: My Context acquisition reread one owner’s profile for prompt and event projections, and plan rendering serialized the same tool schema twice. Decision: derive prompt, character count, and structured event items from one acquired profile snapshot; reuse the render-local canonical tool-manifest digest for cache epochs. A populated profile now needs one read instead of three (empty: one instead of two); schema serialization needs one pass instead of two per planned render. No bytes, hash formats, epoch inputs, or token budgets change. Alternatives: cross-request caches risk stale owner state; parsing event items from Markdown loses multiline work-rule actions. Block tokenization remains unchanged rather than adding another cache. Count/parity specifications: `tests/context/test_context_preparation_work.py`.

### Snapshot freshness and provenance

Problem: metadata-only supersession left contradictory instructions visible; retrieval misses were mistaken for knowledge invalidation and redundant ambient facts polluted the model input.
Decision: changed contracts use scoped precedence notices and verified withdrawal. Memory no-match is diagnostic-only, not a withdrawal. Date reuse checks retained owned clock sections and top-level system/developer facts, never user/tool quotes or examples; missing/cross-day facts append once. Optional Project Brain pages reach only the available owner/project-bound coordinator, not every worker, and may await success ACK only after complete admission. Unchanged states append zero bytes; open tool batches defer updates.
Read failures retain last-known contract snapshots with typed receipts; optional absent/failed project pages add no model notice. Historical evidence and ordinary worker inbox/overlap delivery remain unchanged.
Rejected alternatives: rewriting consumed history breaks replay stability; withdrawing on a miss confuses relevance with truth; independent date watermarks and a second coordination state machine duplicate authority. Tests: `tests/context/test_context_integrity.py`, `test_context_composer.py`, `test_context_injection_registry.py`, `test_context_necessity.py`.

### Central automatic-injection contract

The public `context_composer` interface and `registry.py` own every automatic source. [CONTEXT_INJECTIONS.md](../CONTEXT_INJECTIONS.md) specifies all injection necessity/admission decisions, protected lanes, optional controls, bootstrap authority, tool-batch deferral and bounded Inspector receipts. Add a registry entry before any provider or chronological writer; no raw synthetic-message bypasses.

### Runtime message-mutation boundary

Ordinary completed chat tasks prepare their final model view before settlement and publish it atomically with Turn terminal state. The next task validates owner, lane, Turn content witnesses, model/system/permission/experiment settings and attachment availability before restoring it and appending new Turns; only the new suffix goes through same-role merging.
Edits, fresh plans, incompatible policies, missing media and unsupported recovery modes rebuild from authority, never an unvalidated checkpoint. Restart and disposable-cache eviction preserve the Sidecar-owned continuation.
Policy-refusal observability (2026-10-25): a refused restore used to log only an opaque `policy_changed` digest mismatch, so R1 cold rebuilds (e.g. the swarm auto-continuation, conv mvl5wxpj 22:56) left no evidence about WHICH setting moved. Decision: the checkpoint now stores a `policyFields` canonical field map beside its `policy` digest, and `restore_prefix` names the drifted policy fields on `config['_modelReplayPolicyDrift']` (plus a WARNING log) when it refuses. `policyFields` is additive — `encode_projection` and the `policy` digest are byte-identical, so existing checkpoints and replay fixtures are untouched; legacy checkpoints without the map report only the digest mismatch. Alternatives rejected: relaxing the witness to ignore ambiguous fields would weaken the valid restore gate; logging the full config would leak content. Specs: `tests/scheduler/test_swarm_autocontinue_replay_warmth.py`, `tests/model_routing/test_model_continuation_policy.py`.
Every producer of a continuation config must reproduce the policy its request was pinned with, not an equivalent-looking subset: an absent field is drift even when its runtime default matches. Coordinator continuations take their model-view selection from the accepted request attempt, and Coordinator ingress applies the role's tool settings before the pin (`docs/PROJECT_COORDINATION.md`, continuation model selection; `tests/project_mod/test_coordinator_replay_policy.py`).
Text-only history may cross a route or effort change when every other policy witness matches: old composer carriers are removed, current rules recomposed, provider reasoning/signatures/opaque native items stripped, and model-specific wire latches not restored; L2 history-carrier and inbox provenance is portable text metadata, while multimodal and unknown private state remains conservatively nonportable ([continuation checkpoints](../CONTEXT_COMPACTION.md#continuation-checkpoints)).
Problem: replay/cache eviction used to lose frozen MCP declarations and silently reselect a different tools prefix. Decision: owner-scoped conversation settings persist the declaration wire independently of replay; restart, failed/resume/unsupported replay and memory reclaim preserve it. Only the orchestration latch is restored from compatible replay. Reject replay-owned or memory-only declaration authority: neither spans the conversation lifecycle. Current gateway permissions always govern execution.

Text tool receipts retain the exact L0/V2-bounded model text, including results over 50,000 characters. Ordinary replay and Continue retain every todo receipt. Screenshot
receipts separate display text from `toolModelContent`; images reuse the existing owner-scoped immutable media store and its per-turn count/byte budgets.
Missing/foreign/unretained images produce explicit unavailable markers; media-store failure permits at most 256 KiB of inline image text per image and 1 MiB per Turn.
MCP tool results carry ImageContent out-of-band on a `str` subclass (`MCPToolResult.image_contents`): capture is ordered before every plain-`str` re-minting transformation (content filtering, L0/aggregate budgeting) and projects through the same `toolModelContent` lane as screenshots (tests: `tests/tasks_pkg/test_fetch_url_mcp_images.py`, `tests/mcp_protocol/test_mcp_result_images.py`).
Reconstruction returns detached blocks; request-local image normalization pins the first observed
bytes, so later tool rounds do not reread mutable local paths. The image L1 step protects the entire latest unconsumed batch until a model response proves consumption.

Context providers detach metadata without traversing executor-owned `messages`, `_turn_input_messages`, `events`, `result` or `toolRounds`; the synchronous renderer and state projection keep the original message/task authority. This removes history-sized copy allocation at every provider acquisition. A controlled offline sample (2026-10-08, 5,000 synthetic messages, 20,000 events, seven runs) measured median 71.136 ms / 15,404,024 peak allocated bytes for the prior full copy versus 0.003 ms / 2,792 bytes for metadata only; this excludes upstream TTFT and storage/queue delays. Measure with `python3 benchmarks/send_startup.py`; verify with `tests/context/test_context_provider_snapshot.py`. Read-only compaction hooks and context providers receive detached nested JSON containers; mutation or late completion cannot edit live history/config. The bounded continuation lifecycle is in [`STORAGE.md`](../STORAGE.md). Publication/restore report missing base, unsupported/resume mode, absent checkpoint, failed settlement, changed content and unavailable storage. Preparation logs `checkpoint_prepared`; only the terminal storage receipt logs `checkpoint_settled`, with conversation/Turn/attempt identity, so a candidate is never reported as a committed continuation. Tests: `tests/model_routing/test_model_replay_stability.py`, `tests/model_routing/test_model_continuation_policy.py`, `tests/model_routing/test_model_continuation_store.py`. The orchestrator deep-copies its nested projection before context composition, cache ordering, compaction, sanitization or provider conversion. Durable Turns stay immutable; automatic Layer 1 never reads or writes conversation authority. Dynamic guidance—MCP deltas, skills/memory, environment, lifecycle recovery, URL prefetch, branch context, Paper gateway routing, local PTC, and native multi-agent hints—is an appended tail `user` carrier. Provider converters must not add ephemeral per-round copies: stable PTC and multi-agent policy is composer-owned, while dynamic stage/capacity stays in execution telemetry rather than prompt bytes.

Tool inclusion may still change at an explicit capability/permission boundary,
but an included first-party tool keeps its canonical name, description, and
schema; runtime project roots, remote bindings, memory scope, filter settings,
swarm capacity, routing stage, and schema pressure do not rewrite those bytes.

The remaining transformations are narrow exceptions, not context injection:

- conversation projection renders structured user-owned fields (reply quotes,
  referenced conversations, plans, documents, images, and videos) into the API
  representation without changing stored Turns;
- `UserPromptSubmit` hooks may deliberately replace the newest request text
  because PII redaction and safety filtering would be defeated otherwise;
- automatic L2/advanced/reactive compaction may remove or summarize history in
  the request-local working copy to satisfy a hard provider context limit;
  manual compaction is the separately authorized persistent operation;
- provider adapters operate on fresh wire bodies and may normalize roles,
  tool-call/result blocks, images, cache-control annotations, or alternating
  message constraints required by the destination protocol;
- Claude OAuth cloaking rebuilds the provider-only system envelope but moves
  the user's system text to a stable carrier after the first user; swarm checkpoint
  recovery replaces only a stale provider-policy system carrier;
- token counters may construct synthetic counting-only messages. These never
  enter the task projection or conversation authority.

Natural assistant/tool output remains execution-owned. Loop-breaker, inbox and recovery context uses the registered injection interface, never history rewrites. Explicit user projections, tool-result envelopes, compaction and separate workflow requests retain their own owners; interruption recovery resends the interrupted request and appends nothing. Any new exception requires an executable regression and a boundary update.

Provider read ownership — Problem: a fixed process pool and pending count degraded context solely because unrelated reads were blocked; one-second joins also claimed close before reads ended. Decision: early prefetch and final acquisition share the application-observed `tofu_workers` demand kernel. Every independent read may start, with no queue/count refusal; the immutable launch probe sizes only idle reuse and scales above eight. Physical startup errors preserve their original failed Future, withdraw only unstarted work and leave accepted siblings owned. Optional acquisition deadlines, request-local cancellation, detached inputs, authorization and late-result exclusion remain separate authorities. Alternatives: larger queues preserve omission; private request pools duplicate scheduling; detached close loses physical ownership. Verify `tests/context/test_context_composer.py` and `test_context_provider_executor_lifecycle.py`: 32 blocked reads with legacy hints 1 still permit a healthy 33rd read, failed startup is retryable, races drain or refuse at close, and waiting close outlives the old one-second limit. Controlled measurement: 32 actual read workers rendezvoused in 7.02 ms; joined close leaves zero workers. This measures scheduling, not adapter latency.

Executor shutdown fences publication and wakes accepted workers. Nonwaiting close retains physical ownership for a later joined close; waiting close joins peers to completion and skips its caller. Request leases cancel only their own not-yet-started Futures and never close the shared executor; timed-out provider results cannot merge into the live request.

`ContextPlanV2` is the request-local global budget authority. It classifies objective/constraints, structured task state, evidence/recovery, hot tail, and recoverable cold history.
Required blocks lock first; permission, priority, freshness, access value, and token cost deterministically select optional blocks.
Its manifest records selection/suppression, hashes, token counts, recovery handles, stable segment hashes, and cache epoch. Per-block `max_tokens` remains a hard ceiling but cannot bypass the global budget. Multi-agent task shapes receive only a bounded tail trigger/discipline block; the canonical `spawn_agents` schema is never rewritten with per-round stage or capacity prose.

`TaskStateSnapshotV1` is rebuilt from turn/tool events: goal, hard constraints, decisions, completed work, files, tests, errors, open questions, TODOs, evidence IDs, observation time, and world version.
It is a request projection, never a second transcript authority; independent required checklist restoration follows [the continuity contract](../CONTEXT_COMPACTION.md#authoritative-checklist-restoration); specs: `tests/goals/test_checklist_context.py`.

The static prompt profile resolves once here. Kimi `auto` remains the full control contract; `lean` and named ablations require explicit experiment policy.
Every request stamps bounded `tofu.prompt-profile/v1` evidence—requested/resolved/effective profile, status/reason, model, character/token counts, SHA-256, and disabled blocks—on the task, `platform_static` provenance, and each round snapshot; replace mode records an empty effective profile.

## Automatic versus persistent compaction

Automatic, manual and provider-native compaction share token/summarization primitives, not persistence authority; preservation, cache economics, bounded receipts and owner-scoped summary routing follow [the compaction contract](../CONTEXT_COMPACTION.md). Local L2 uses a fallible user-role handoff, never a fictitious tool execution. `_handoff.py` derives pre-turn placement (before the latest human) or mid-turn placement (after retained execution/restored context) from source chronology. Later steering/replay cannot move an already-consumed boundary; manual/native authority stays separate. Specs: `tests/compaction/test_compaction_handoff.py`.

### L1 sent-prefix admission

Every api-form message the previous request sent is provider-cached. `cache_tracking.get_cache_prefix_count` returns that whole sent count (own thread, sibling thread, invocation snapshot or durable high-water mark, which records the same count), clamped to the live list after a history shrink. Default L1 (`micro_compact`) and the advanced host leave those bytes identical with one exception, the sent request's last tool step: from the last assistant message inside the sent prefix to its end, since the request before cached everything earlier. L1 runs its step list on a detached copy with only that step unprotected (dry run: no evidence, archives or telemetry). If the step would change, `sent_step_rewrite_economics` prices the very next request on the main route's verified rates. Rewriting costs `after_tokens x cacheWriteMul`; keeping costs `before_tokens x cacheReadMul`; new content after the sent prefix is written either way. Only a strictly cheaper rewrite is applied, and L1 advertises it to the cache-break detector as a bounded rewrite starting at that step. Unverified or unknown pricing keeps the step. Each decision logs one `[L1] ... sent-step edit admitted|refused` INFO line with the region's token estimates and both priced sides. On Claude routes the region estimate prices each image from its data-URL pixel size (Anthropic's documented `width x height / 750` after its downscale); other routes keep the flat estimate. `EDITABLE_TAIL_COUNT` remains only a cache-break diagnosis tolerance for provider-wire tail representation; the transport never edits api-form messages in place. Specs: `tests/compaction/test_l1_sent_step_admission.py`, `tests/cache/test_cache_prefix_cross_thread_freeze.py`.

**Problem:** the guard protected `sent count - EDITABLE_TAIL_COUNT` (2) api-form messages and never priced an edit of sent content. Counting api-form messages split parallel tool batches, whose results the Anthropic wire merges into one user message: on conversation `mvywcu5qqy5ocy` L1 replaced a ~80-token image crop with a ~60-token stub and re-wrote the whole 3,624-token merged message at 1.25x input. **Decision:** protect the whole sent request and admit an edit of its last step only under the exact next-request cost rule. A consumed 20K-token result shrunk to a 200-token stub still compacts (375 < 2,010 input-token equivalents at 1.25x/0.10x); marginal edits such as the incident are refused. **Alternatives rejected:** protecting everything sent leaves large consumed results until L2; keeping the count-2 tail splits batches and stays unpriced; a longer payback horizon would be a tuned knob, while the next request is the one cost the rule prices exactly.
## Memory boundary

Relevance uses the latest authored human turn, not machine-authored carriers, attachment helpers, quoted conversation or context markers.
Augmented projections retain `_authoredUserText` locally; plain text needs no duplicate. Same-role merges keep the newest value, text-only replay permits it, and provider sanitization removes it. `UserPromptSubmit` replacements update provenance too, preserving redaction.
An empty/image-only request cannot reuse an older topic. Each selection resets held matches and distinguishes disabled from failed status. Legacy projections strip whole composer sections and generated image hints, not arbitrary authored tool identifiers.

Project memories belong to the configured canonical project, not a task-owned execution checkout.
When workspace binding records `canonicalProjectPath` / `canonicalProjectPaths`, prefetch, search,
CRUD and merge use those roots; file execution stays in the isolated checkout. Legacy configs
without the canonical mapping retain their `projectPath` / `projectPaths` behavior. Existing
memories stranded in an old worker checkout remain there; recreating their verified lesson through
the supported `create_memory` tool writes the canonical store, with no database or file migration.
Specification: `tests/memory/test_memory_canonical_workspace.py`.

Memory retrieval is precision-first and bounded: metadata-only prefetch hydrates selected evidence only, explicit search streams one 2,000-character body prefix at a time, and BM25 retains query-term statistics rather than corpus tokens. Stored memories, My Context facts,
and session context stay distinct. After a clean interactive turn, one bounded
learner accepts at most two concise items backed by verbatim real-user evidence;
it rejects uncertain, one-off, synthetic, assistant, or tool content, and every accepted change is undoable. As reconstructible background work, it honors billing stops and active shared contention before transport, with at most one actual upstream 429; skips and failures leave the profile unchanged.

Memory is not a fallback transcript store or source of owner identity. New writes share one bounded payload contract and reject before corpus scans or mutation; one directory sidecar serializes ID selection and atomic publication across threads/cooperating POSIX processes, and suffix collisions use one directory snapshot. Direct-ID CRUD probes the canonical store/root order without enumerating the corpus; update/delete/toggle/clear/merge re-resolve under sorted directory-local mutation locks (at most 16 shards per directory), so cooperating writers are linearizable while unrelated shards remain parallel and non-cooperating edits retain revision-conflict protection.
Each corpus directory uses one closed `scandir` snapshot; retrieval retains a ten-field summary view and hydrates selected IDs by exact canonical probes rather than rebuilding the corpus.
A bounded launch-probed LRU may retain recursively frozen parsed frontmatter only; matching fingerprints newer than 2.1 seconds are conservatively re-read because valid coarse-clock filesystems can preserve every stat field across a same-size edit, and every access carries its authenticated owner.

## Failure semantics

- Exact tokenization unavailable: use the declared heuristic and expose its
  provenance; do not pretend the count is exact.
- Context over budget: compact through registered stages, then return a typed
  context-limit failure if the irreducible payload still does not fit.
- Final-admission summary failure: preserve live/transient source and refuse
  an overfull main dispatch, retaining only a current issued safe credential
  cause. Unquoted auxiliary credential outages may requalify within the existing
  owner/quote policy; explicit utility recovery remains separate. Auxiliary
  selection and dispatch never challenge a recorded billing stop
  (`docs/CONTEXT_BUDGET_POLICY.md`). Retain usable complete model text after
  shaping faults.
- Concurrent manual compaction: reject/retry from a fresh transcript revision.
- Invalid tool history: fail or repair at the single message-construction
  boundary, with diagnostics.

## Invariants

- One context-window authority and one token-counter resolution API.
- `lib/tasks_pkg/message_semantics.py` owns human-source classification and the private
  source-field allowlist. Cold history reconstruction preserves those fields
  through counting, task-goal extraction and compaction retention; protocol
  sealing strips them before provider wire output. Automatic user-role carriers
  cannot become human goals simply by passing through a rebuild. The canonical
  initiator reader returns a non-human `unknown` sentinel for malformed explicit
  values; absent/null legacy initiators remain human, and writers cannot stamp
  the unknown sentinel.
- Explicit Turn identity takes precedence over legacy timestamps during
  request-local deduplication. Equal timestamps never collapse distinct Turns
  or different source kinds; explicit Turn/source boundaries survive same-role
  merging until protocol conversion.
- Deterministic provider order and observable segment provenance.
- Durable messages are never mutated by request-local preparation; same-role diagnostics classify original pair boundaries using short-lived producer identity deleted before provider/debug wire output, so synthetic carriers, relocated objective anchors and a request whose empty interrupted/failed reply history projection dropped (`_replyDropped`) stay silent while naked real-to-real duplicates still warn.
- Human-facing delivery records are not model turns: history projection omits Coordinator report Turns (`_turnKind` `coordinator_report.*`), whose content reaches the model as the publishing reply's own `report_to_user` call/result ([`PROJECT_COORDINATION.md`](../PROJECT_COORDINATION.md), "Model view").
- Runtime facts are append-only tail user carriers; `platform_static`,
  `ui_nav_directory`, `parallel_execution`, and `delegate_mode` hold dedicated
  system-prefix carriers.
- Persistent compaction is atomic and revision-guarded.
- System, project, identity, and safety instructions retain declared priority.
- Tool call/result pairs remain adjacent and correctly identified.
- Memory retrieval is owner-scoped and bounded; My Context review uses the latest real user turn, strict optional billing/contention admission, and at most one actual upstream 429; skips and failures cannot erase base context.
- Prompt-cache decisions stay stable for the lifetime of one task. Explicit zero/floor reads replace the predecessor; missing usage does not. Owner-scoped completed-turn publication uses the task-result checkpoint, never scope teardown; children/transients cannot publish parent facts. Replay and restart semantics are in [`STORAGE.md`](../STORAGE.md).
- Economic working sets derive from provider/model price tiers without model
  name checks; explicit overrides remain authoritative.
- Turn cost aggregates per API round under that round's model/provider/tier,
  and the UI exposes total, uncached, and cache-read tokens together.
- Required `ContextPlanV2` blocks cannot be evicted by optional evidence.
- Mutable facts carry observation/world-version metadata and are revalidated
  before a claim depends on their freshness.
- Provider usage reuse requires a complete measured-prefix digest, exact model,
  and unchanged system/tool envelope; a historical count cannot downclamp a
  newly reconstructed request. Selected-route input above its hard window is
  refused locally before transport and enters the existing compaction recovery.
- Per-round measurement uses the shared local token authority. Its bounded
  digest entries carry encoding-scoped BPE and entropy counts without prompt
  text under a launch-probed 4,096-entry ceiling. Admission may hand off 4,096
  text identities, one schema integer, and one exact digest synchronously;
  identical list/model/ordered schema objects are required; none is serialized.

**Measured-prefix encoding — Problem:** identical encoder configuration was rebuilt per message. **Decision:** reuse one request-local encoder; full canonical digests, encoding failures and mutation denial remain exact.
**Alternatives:** identity checks miss edits, prompt caches retain history, and changing encoders can alter numeric/error semantics. Public usage-cache fixtures measured completion CPU 12–19% lower for short/history/mixed content.
Three 1,000-operation wide pairs passed unchanged tail/RSS guards: CPU 1–3% lower, P99 1–3% higher. `system_usage_signature_encoder_admission_20261009.json` retains earlier failures; inference and whole-context latency are excluded.

**Round-prep token counting — Problem:** every model round counts the whole append-mostly request three to five times (compaction, its gate, prompt admission). Each count derived a SHA-256 cache key for every history text of at least 512 characters and re-tokenized every shorter or uncached text through `Encoding.encode_batch`. CPython hashlib releases the GIL for each update of 2,048 bytes or more, tiktoken releases it on every `encode`, and `encode_batch` also starts an eight-thread pool per call; beside other runnable threads each release waited about a switch interval, so one counting step of a 290-message history took about 1.8 s beside two busy threads (a 240 s live py-spy profile put 62% of compaction time in cache-key hashing). **Decision:** (1) text digests come from `lib.utils.sha256_holding_gil`, byte-identical and never handing the GIL away. (2) `_encode_batch_counts` tokenizes one `<|endoftext|>`-joined batch with only that special token allowed and splits counts at its token id; this equals per-text `encode(text, disallowed_special=())` because tiktoken cuts the text at an allowed special before regex pre-tokenization. Texts that spell the separator keep their own call, and the borrowed native encoder counts its batch natively. (3) The task-owned `RequestTextCounts` memo maps `SHA-256(text) → exact count` for exactly the latest counted request. Misses try the shared digest cache, then one batch call. A hit therefore means an equal text, never a hash collision. The memo retains no text and has no capacity knob (about 130 bytes per distinct text); every count replaces it, and it dies with the task. Concurrent counts read and publish one immutable `(encoding, counts)` snapshot in a single assignment. A racing count, including one through a memo shared by a copied task dict or a detached provider snapshot, can therefore only lose reuse and never mix encodings. An encoding change discards the previous snapshot, and a failed tokenizer call publishes nothing. (4) The network-tier prefilter reuses digest-cached entropy estimates. Token counts, gate measurements and wire bytes are unchanged. **Alternatives:** keying the memo by `(len, hash(text))`, as first drafted, let two same-length texts whose 64-bit string hashes collide share one count. Keying by the `str` itself skips the digest (27 instead of 31 ms per idle round on the real history below), but it would pin the last request's text after terminal persistence releases `messages`. A larger shared LRU still tokenizes every text under its 512-character floor. Lowering the interpreter switch interval taxes every thread, counting a plain concatenation is inexact, and sequential `encode` still releases the GIL once per text. **Measurement** (`benchmarks/round_prep_body.py`, Python 3.12.13, 8 rounds, p50 per round of compaction + gate + admission, median of two alternating fresh-process runs; per-round body, row and measurement digests identical across all compared trees): on a real 290-message Request Inspector history (1.18 M text characters, 53 tools), idle time fell from 102 to 31 ms. With two CPU-bound threads it fell from 5,469 to 80 ms; the batch and digest changes alone reached 126 ms and the memo alone 1,898 ms. With four threads (one run) it fell from 9,160 to 97 ms. A synthetic 400-message history fell from 117 to 43 ms idle and from 4,467 to 96 ms with two threads. The remaining idle time is mostly the digest-cached entropy prefilter and heuristic floor (about 26 of 34 ms per round). Storage commits and the real serving thread mix are excluded. Specs: `tests/token_counter/test_token_text_cache.py`, `tests/token_counter/test_token_counter_dispatch.py`, `tests/utils/test_gil_held_digest.py`, `tests/meta/test_round_prep_body_measure.py`.

## Change routing

| Change | Start here | Verify |
|---|---|---|
| New automatic context source | `context_composer/registry.py` first, then provider/event producer via public facade | injection registry/no-bypass, ordering, provenance, budget, protected-authority and tool-batch tests |
| Global context budget or task projection | `context_composer/_render.py`, `task_state.py` | required-block, determinism, permission, cache-epoch tests |
| Model context window or tokenizer | `lib/context_limits/`, `lib/token_counter/` resolver | self-heal, model-profile, exact/fallback parity tests |
| Compaction stage or manual compaction | `lib/tasks_pkg/compaction/_steps.py`, `_manual.py`, `_persist/` | anchors, tool pairs, usage counting, route/concurrency/durable-reload tests |
| Memory selection / My Context learning | `lib/memory/prefetch/`, `relevance/`, `profile_consolidate.py` | owner isolation, grounding, specificity |

## Test map

```bash
pytest -q tests/context/test_context_composer.py tests/context/test_context_limits_selfheal.py tests/context/test_context_injection_registry.py tests/context/test_context_injection_aux.py tests/context/test_context_trace.py
pytest -q tests/tasks_pkg/test_token_budget_reminder.py tests/agent_core/test_agent_notes_journal.py tests/project_mod/test_journal_maintenance.py tests/diagnostics/test_request_inspector.py tests/token_counter/test_token_counter_heuristic.py
pytest -q tests/compaction/test_compaction_invariants.py tests/compaction/test_compaction_anchor.py tests/compaction/test_compaction_receipt.py tests/compaction/test_compaction_handoff.py tests/compaction/test_compaction_history_wire.py tests/compaction/test_compaction_history_providers.py tests/compaction/test_l1_sent_step_admission.py
pytest -q tests/compaction/test_scheduled_working_view_compaction.py tests/compaction/test_summary_coverage.py tests/tasks_pkg/test_coordinator_context_lifecycle.py
pytest -q tests/tasks_pkg/test_manual_compaction_engine.py tests/tasks_pkg/test_manual_compaction_route.py tests/memory/test_memory_prefetch_local.py tests/memory/test_memory_global_server_store.py tests/memory/test_memory_create_allocation.py tests/memory/test_memory_direct_lookup.py tests/memory/test_memory_mutation_concurrency.py
pytest -q tests/tasks_pkg/test_long_agent_v2_contracts.py -k 'context_plan or task_state or adaptive_compaction' tests/tasks_pkg/test_prompt_profile_adoption.py
```

Profile review demand — Problem: one process-wide semaphore dropped eligible reviews across unrelated owners, and file-backed context mutations lacked a complete read/mirror/history transaction. Decision: eligible reviews start independently; ordinary provider/billing policy remains while immediate-only contention admission is retired. The captured task owner reaches model dispatch. context_transaction(scope) uses the shared weak JSON-path/process locking seam, reentrant only inside one storage transaction; model calls stay outside it. Assistant updates recheck their original item snapshot under the write transaction, and new-item/tombstone checks share that transaction. Alternatives: larger semaphore counts still drop work; a global learner mutex serializes independent owners and paid calls; unlocked parallel writes lose items/undo evidence. Concurrent writes, duplicate learning, stale manual edits and per-task event tests verify the boundary.
<!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->


### Cache diagnostic module boundaries

**Problem:** a single cache detector mixed provider-byte comparisons, policy,
state mutation, durable checkpoints and human diagnostics; task startup also
carried duplicate project preparation. **Decision:** `_detect` remains the sole
locked round coordinator. `_wire` compares the prior snapshot, `_policy` owns
pure thresholds and buckets, `_advance` updates the baseline and guarded sinks,
and `_report` formats confirmed evidence. `_health` measures same-route
read-share splits across rounds; it only labels records and reports, never
buckets. Observation order and one round record remain unchanged. The shared `_startup` owns project preparation for
every role; `_vu_startup` only supplies role-specific phase feedback.

`wire_fingerprint_diff.py` owns pure canonical and raw field comparisons. The
canonical comparator skips field-set construction for equal native dictionaries;
changed fields retain ordering, truncation and missing-value semantics.
**Alternatives considered:** a second detector or independent policy per mode
would drift; moving code without naming side-effect boundaries would preserve
the original coupling; dropping evidence to save work would weaken diagnosis.
A universal comparison loop and a zip-based variant were measured and rejected:
they slowed the existing raw-field comparison. Its specialized loop is retained.

Verification: `tests/cache/`, startup/abort tests and wire fingerprint comparison
contracts. Measure component CPU, wall time and allocation with
`python3 benchmarks/wire_fingerprint_compare.py --baseline-source PATH
--output PATH`; freeze `lib/tasks_pkg/wire_fingerprint_diff.py` from Git as the
baseline source and retain the JSON source hashes and paired samples. These
clocks do not measure complete task latency.

A controlled 10,000-row unchanged-prefix comparison (11 alternating sample
pairs, ten calls per sample) measured median CPU 14.461 ms before and 2.693 ms
after, with transient traced allocations 2,028 and 148 bytes. Raw-field
comparison retains its original loop and stayed near baseline. Small, late-edit,
length-change and early-truncation cases use the same full-output parity gate;
this is a component measurement, not an end-to-end latency guarantee.

**Prefix-mutation attribution — Problem:** the Anthropic translation merges a
round's tool results into one user message whose canonical entry is keyed by
its first result, so a change in any later result was named after the first.
The prefix-mutation cause always claimed a whole-body re-bill even when most
of the prompt was still read from cache, and the inside-prior-prefix position
compared a wire-message index with the API-form message count, which also
counts the hoisted system message and every merged tool result.
**Decision:** a turn merging several results keeps one `(result key, hash)`
pair per result (`blocks`), and `diff_canonical` reports each changed result
under its own key. Fields, alignment keys, marker slots and first-changed
indices stay per wire message; a lone result stays identical to the OpenAI
`tool` message, and snapshots without `blocks` keep the message-level report.
The billing clause states the round's `cache_read`/`cache_write`: the whole
prompt only when nothing was read, and no cache write when the provider
reports none. Reason codes, evidence labels and buckets are unchanged; the
browser phrase table keeps the fixed sentence of persisted rounds. The prior
prefix boundary is the previous round's wire message count minus
`EDITABLE_TAIL_COUNT`, the same space as the canonical or raw-byte index, so
editing the previous round's final wire message without a read drop is an
editable-tail re-bill, not a prefix-mutation break.
**Alternatives considered:** one canonical entry per result would also align
separate and merged results across envelopes, but moves positions out of
wire-message space, where the raw-byte index, marker slots and the editable
tail are defined. Per-result field names would break the `key.field` format.
Mapping the API-form boundary into wire space needs a per-round index map the
detector does not keep. Raw-byte `<bytes>` culprits still name a merged turn
by its first result: they hash top-level message fields only.
