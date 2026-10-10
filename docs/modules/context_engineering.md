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
| Task-state projection / required checklist restoration | `lib/tasks_pkg/context_composer/task_state.py`, `checklist.py` |
| Persistent manual compaction | `lib/tasks_pkg/compaction/_manual.py`, `_persist/` |
| Long-term memory retrieval/injection; My Context proposal/undo UI | `lib/memory/`; `frontend/src/features/memory/preference-actions.ts` |
| Conversation message construction | `lib/tasks_pkg/conv_message_builder/` |
| Completed-task model view recovery | `lib/tasks_pkg/model_replay.py`, `lib/storage/model_continuation.py` |
| Cost experiments and trace | `lib/cost_experiments.py`, context trace modules |
| Price tiers and per-round cost aggregation | `lib/pricing/`, `lib/cost.py` |

## Assembly flow

1. A task receives an immutable view of authenticated conversation and project state.
2. `context_composer` invokes declared providers in deterministic order behind one 15-second request deadline, records per-provider status/timing, and reuses the task-owned project-prefetch future; timed-out providers operate on detached carriers, so late completion cannot mutate the task or prompt.
3. Memory prefetch selects bounded evidence; it does not mutate the transcript.
4. The token counter resolves the model-specific counter, with an explicit heuristic fallback when no exact counter exists.
5. Context-limit policy computes the usable input budget after output and safety reserves.
6. Compaction removes, folds, or summarizes only through registered steps.
7. The final request body is built once by the LLM body builder. Budget pressure is **telemetry only**: `context_budget` records content-free working-message estimates once per compaction window; it never appends a model message or records an intervention `system_note`. Automatic compaction and final-wire admission remain the capacity authorities.

Tool-round continuations preserve round-zero provider/model binding and stable prefix order; providers do not silently reread mutable globals later. Responses assistant phase replay and scoped task/authorization continuity follow [the context contract](../CONTEXT_COMPACTION.md#assistant-phase-and-task-continuity); regression and budget checks: `tests/llm/test_responses_message_phase.py`.

Cache-stable prefix: runtime context never rewrites operator/user-authored base policy or canonical included tool declarations. Two registry blocks own system carriers after explicit policy and before dialogue: `platform_static` (reused unchanged, preserved by local lossy compaction) and `ui_nav_directory` (browser-only, withdrawn off-surface/ablation/replace-mode without changing sibling policy).
Other composer blocks are content-addressed appended user-role TAIL carriers: identical bytes keep position; changes append versions/retractions. Exception: accepted checklist recovery is visibility-driven and standalone, withdraws superseded recovery sections, and survives compaction without new notes.

- `environment` (cwd / is-git / platform / model) ships as a tail block, never inside `platform_static`, so changing the project path does not rewrite the cached prefix. The model perceives the path from that block each turn; a move appends a separately addressed one-shot event and the turn projection gains a `projectPathChange` provenance chip. Baselines are in-memory, conversation-scoped, and bounded (256 scopes), so a restart re-baselines without false transitions.
- Environment-specific multi-root, remote-worktree, programmatic, and
  multi-agent routing guidance also lives in the tail. Included tool schemas
  retain their canonical descriptions; a schema budget may omit an optional
  tool but never strip annotations from an included tool.
- The wire `tools` array freezes at the session's first tool selection: MCP server disconnects/reconnects and newly discovered tools no longer mutate it. Tools that appear after the freeze are advertised by the tail `mcp_tools_delta` block (at most eight names and short capability hints, only when schema discovery is available). Complete schemas are obtained on demand through `search_tools` and tools are callable through `execute_tools`; truncated JSON is never presented as an executable schema. An unchanged reachable set reuses the existing bytes; a changed set appends a new version, and losing the last delta appends one retraction tombstone. Turns where the reachable set actually changed vs the frozen wire carry a `mcpToolsDelta` provenance chip (added/removed names, capped at 8 each).
- `mcpToolExposure='delegate'` (typed option, per-conversation resolvable) keeps EVERY MCP schema off the orchestrator's wire: `_build_mcp` returns an empty wire while `_mcpAllowedToolCatalog` stays the full execution authority, so `search_tools`/`execute_tools` still reach every tool and `spawn_agents` sub-agents inherit the complete catalog through the parent executable catalog (never the wire subset). The mode writes no selection freeze — it is a per-assembly decision, so an explicit mid-conversation exposure switch never fights stale freeze state. The `mcp_tools_delta` block is then the per-turn advertisement: it renders the whole connected catalog as names+hints and points at sub-agent `tools`-allowlist delegation. Specs: `tests/mcp_protocol/test_mcp_delegate_exposure.py`.
- Programmatic and multi-agent wire projection is latched from the task's first orchestration decision. Observed fan-out, later round numbers, retries, serial-gateway experiments, and program fallback state may update telemetry or execution behavior, but cannot add/remove tools or switch an existing tool schema mid-task. Local programmatic projection remains additive.

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
Text-only history may cross a model/route change when every non-route policy witness matches: old composer carriers are removed, current rules recomposed, and model-specific wire latches not restored; L2 history-carrier provenance is portable text metadata, while multimodal, reasoning-bearing and unknown private provider state remains conservatively nonportable.
Problem: replay/cache eviction used to lose frozen MCP declarations and silently reselect a different tools prefix. Decision: owner-scoped conversation settings persist the declaration wire independently of replay; restart, failed/resume/unsupported replay and memory reclaim preserve it. Only the orchestration latch is restored from compatible replay. Reject replay-owned or memory-only declaration authority: neither spans the conversation lifecycle. Current gateway permissions always govern execution.

Text tool receipts retain the exact L0/V2-bounded model text, including results over 50,000 characters. Ordinary replay and Continue retain every todo receipt. Screenshot
receipts separate display text from `toolModelContent`; images reuse the existing owner-scoped immutable media store and its per-turn count/byte budgets.
Missing/foreign/unretained images produce explicit unavailable markers; media-store failure permits at most 256 KiB of inline image text per image and 1 MiB per Turn.
MCP tool results carry ImageContent out-of-band on a `str` subclass (`MCPToolResult.image_contents`): capture is ordered before every plain-`str` re-minting transformation (content filtering, L0/aggregate budgeting) and projects through the same `toolModelContent` lane as screenshots (tests: `tests/tasks_pkg/test_fetch_url_mcp_images.py`, `tests/mcp_protocol/test_mcp_result_images.py`).
Reconstruction returns detached blocks; request-local image normalization pins the first observed
bytes, so later tool rounds do not reread mutable local paths. The image L1 step protects the entire latest unconsumed batch until a model response proves consumption.

Context providers detach metadata without traversing executor-owned `messages`, `_turn_input_messages`, `events`, `result` or `toolRounds`; the synchronous renderer and state projection keep the original message/task authority. This removes history-sized copy allocation at every provider acquisition. A controlled offline sample (2026-10-08, 5,000 synthetic messages, 20,000 events, seven runs) measured median 71.136 ms / 15,404,024 peak allocated bytes for the prior full copy versus 0.003 ms / 2,792 bytes for metadata only; this excludes upstream TTFT and storage/queue delays. Measure with `python3 benchmarks/send_startup.py`; verify with `tests/context/test_context_provider_snapshot.py`. Read-only compaction hooks
and context providers receive detached nested JSON containers; mutation or late completion cannot edit live history/config. The bounded continuation lifecycle is in [`STORAGE.md`](../STORAGE.md). Publication/restore report missing base, unsupported/resume mode, absent checkpoint, failed settlement, changed content and unavailable storage. Preparation logs `checkpoint_prepared`; only the terminal storage receipt logs `checkpoint_settled`, with conversation/Turn/attempt identity, so a candidate is never reported as a committed continuation. Tests: `tests/model_routing/test_model_replay_stability.py`, `tests/model_routing/test_model_continuation_policy.py`, `tests/model_routing/test_model_continuation_store.py`.
The orchestrator deep-copies its nested projection before context composition,
cache ordering, compaction, sanitization or provider conversion. Durable Turns
stay immutable; automatic Layer 1 never reads or writes conversation authority. Dynamic
guidance—MCP deltas, skills/memory, environment, lifecycle recovery,
URL prefetch, branch context, Paper gateway routing, local PTC, and native
multi-agent hints—is an appended tail `user` carrier. Provider
converters must not add ephemeral per-round copies: stable PTC and multi-agent
policy is composer-owned, while dynamic stage/capacity stays in execution
telemetry rather than prompt bytes.

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

Natural assistant/tool output remains execution-owned. Loop-breaker, inbox and recovery context uses the registered injection interface, never history rewrites. Explicit user projections, assistant prefill, tool-result envelopes, compaction and separate workflow requests retain their own owners. Any new exception requires an executable regression and a boundary update.

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
## Memory boundary

Relevance uses the latest authored human turn, not machine-authored carriers, attachment helpers, quoted conversation or context markers.
Augmented projections retain `_authoredUserText` locally; plain text needs no duplicate. Same-role merges keep the newest value, text-only replay permits it, and provider sanitization removes it. `UserPromptSubmit` replacements update provenance too, preserving redaction.
An empty/image-only request cannot reuse an older topic. Each selection resets held matches and distinguishes disabled from failed status. Legacy projections strip whole composer sections and generated image hints, not arbitrary authored tool identifiers.

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
- Final-admission summary failure: use a bounded transcript-derived recovery
  receipt; retain usable model text after shaping faults.
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
- Durable messages are never mutated by request-local preparation; same-role diagnostics classify original pair boundaries using short-lived producer identity deleted before provider/debug wire output, so synthetic carriers and relocated objective anchors stay silent while naked real-to-real duplicates still warn.
- Runtime facts are append-only tail user carriers; only `platform_static`
  and `ui_nav_directory` hold dedicated system-prefix carriers.
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
Three 1,000-operation wide pairs passed unchanged tail/RSS guards: CPU 1–3% lower, P99 1–3% higher. [Evidence](../evidence/system_usage_signature_encoder_admission_20261009.json) retains earlier failures; inference and whole-context latency are excluded.

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
pytest -q tests/compaction/test_compaction_invariants.py tests/compaction/test_compaction_anchor.py tests/compaction/test_compaction_receipt.py tests/compaction/test_compaction_handoff.py tests/compaction/test_compaction_history_wire.py tests/compaction/test_compaction_history_providers.py
pytest -q tests/tasks_pkg/test_manual_compaction_engine.py tests/tasks_pkg/test_manual_compaction_route.py tests/memory/test_memory_prefetch_local.py tests/memory/test_memory_global_server_store.py tests/memory/test_memory_create_allocation.py tests/memory/test_memory_direct_lookup.py tests/memory/test_memory_mutation_concurrency.py
pytest -q tests/tasks_pkg/test_long_agent_v2_contracts.py -k 'context_plan or task_state or adaptive_compaction' tests/tasks_pkg/test_prompt_profile_adoption.py
```

Profile review demand — Problem: one process-wide semaphore dropped eligible reviews across unrelated owners, and file-backed context mutations lacked a complete read/mirror/history transaction. Decision: eligible reviews start independently; ordinary provider/billing policy remains while immediate-only contention admission is retired. The captured task owner reaches model dispatch. context_transaction(scope) uses the shared weak JSON-path/process locking seam, reentrant only inside one storage transaction; model calls stay outside it. Assistant updates recheck their original item snapshot under the write transaction, and new-item/tombstone checks share that transaction. Alternatives: larger semaphore counts still drop work; a global learner mutex serializes independent owners and paid calls; unlocked parallel writes lose items/undo evidence. Concurrent writes, duplicate learning, stale manual edits and per-task event tests verify the boundary.
