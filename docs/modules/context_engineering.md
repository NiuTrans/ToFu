# Context engineering

This domain turns durable conversation/project state, memory, tool history, and model limits into bounded LLM messages, and owns automatic/user-requested compaction. Durable transcript authority is [`../STORAGE.md`](../STORAGE.md).

## Ownership

| Concern | Owner |
|---|---|
| Context assembly and provider ordering | `lib/tasks_pkg/context_composer/` |
| System/project/user context sources | `lib/tasks_pkg/context_composer/_providers.py` |
| Bounded provider read execution | `lib/tasks_pkg/context_composer/_provider_executor.py` |
| Model context-window policy | `lib/context_limits/` |
| Token counting | `lib/token_counter/` |
| Automatic compaction | `lib/tasks_pkg/compaction/` |
| Rebuildable task-state projection | `lib/tasks_pkg/context_composer/task_state.py` |
| Persistent manual compaction | `lib/tasks_pkg/compaction/_manual.py`, `_persist/` |
| Long-term memory retrieval/injection; My Context proposal/undo UI | `lib/memory/`; `frontend/src/features/memory/preference-actions.ts` |
| Conversation message construction | `lib/tasks_pkg/conv_message_builder/` |
| Cost experiments and trace | `lib/cost_experiments.py`, context trace modules |
| Price tiers and per-round cost aggregation | `lib/pricing/`, `lib/cost.py` |

## Assembly flow

1. A task receives an immutable view of authenticated conversation and project state.
2. `context_composer` invokes declared providers in deterministic order behind
   one 15-second request deadline, records per-provider status/timing, and
   reuses the exact task-owned project-prefetch future rather than launching a
   duplicate storage read. Timed-out providers operate on a detached carrier,
   so late completion cannot mutate the live task or its prompt.
3. Memory prefetch selects bounded evidence; it does not mutate the transcript.
4. The token counter resolves the model-specific counter, with an explicit heuristic fallback when no exact counter exists.
5. Context-limit policy computes the usable input budget after output and safety reserves.
6. Compaction removes, folds, or summarizes only through registered steps.
7. The final request body is built once by the LLM body builder.

Tool-round continuations preserve round-zero provider/model binding and stable prefix order; providers do not silently reread mutable globals later.

Cache-stable prefix layout: the operator/user-authored base system message and
canonical included tool declarations are never rewritten by runtime context.
Every composer-owned block—including `platform_static`, project rules,
preferences, memory/skill/vault guidance, and orchestration guidance—first
renders in an appended user-role TAIL carrier. Subsequent composition is
content-addressed: an identical block stays at its existing byte position; a
changed block appends only its new version, and an explicit retraction appends
a tombstone. No composer pass deletes and rebuilds surviving carriers.

- `environment` (cwd / is-git / platform / model) ships as a tail block, never inside `platform_static`, so changing the project path does not rewrite the cached prefix. The model perceives the path from that block each turn; a move appends a separately addressed one-shot event (old → new, earlier absolute paths may be stale) and the turn projection gains a `projectPathChange` provenance chip. The next steady turn leaves the new environment bytes untouched. First sight and steady state never fire; baselines are in-memory, conversation-scoped, and bounded (256 scopes), so a restart re-baselines without false transitions.
- Environment-specific multi-root, remote-worktree, programmatic, and
  multi-agent routing guidance also lives in the tail. Included tool schemas
  retain their canonical descriptions; a schema budget may omit an optional
  tool but never strip annotations from an included tool.
- The wire `tools` array freezes at the session's first tool selection: MCP server disconnects/reconnects and newly discovered tools no longer mutate it. Schemas of tools that appear after the freeze are injected at the tail `mcp_tools_delta` block (name + description + input schema, bounded) and are callable through `execute_tools`. An unchanged reachable set reuses the existing bytes; a changed set appends a new version, and losing the last delta appends one retraction tombstone. Turns where the reachable set actually changed vs the frozen wire carry a `mcpToolsDelta` provenance chip (added/removed names, capped at 8 each).
- Programmatic and multi-agent wire projection is latched from the task's first orchestration decision. Observed fan-out, later round numbers, retries, serial-gateway experiments, and ToolScript fallback state may update telemetry or execution behavior, but cannot add/remove tools or switch an existing tool schema mid-task. Local programmatic projection remains additive.
Both transition chips ride the existing task-sidecar → turn-projection `provenance` lane (declared in `contracts/conversation_sync_v3.yaml`) and render in the turn-provenance strip (`frontend/src/conversation/presentation/turn-provenance.ts`); they appear only on the turn that observed the change.

### Runtime message-mutation boundary

The durable Turn transcript is immutable during automatic execution. The
orchestrator deep-copies its nested message projection before context
composition, cache ordering, compaction, sanitization, or provider conversion.
Automatic Layer 1 never reads or writes conversation authority. Dynamic
guidance—including MCP deltas, skills/memory, environment, lifecycle recovery,
URL prefetch, branch context, Paper gateway routing, local PTC, and native
multi-agent hints—is represented by an appended tail `user` carrier. Provider
converters must not add ephemeral per-round copies: stable PTC and multi-agent
policy is composer-owned, while dynamic stage/capacity stays in execution
telemetry rather than prompt bytes.
The former cache-oriented `tool_call_id` sorter is a compatibility no-op;
produced tool-result order is preserved.

Tool inclusion may still change at an explicit capability/permission boundary,
but an included first-party tool keeps its canonical name, description, and
schema. Runtime project roots, remote bindings, memory scope, filter settings,
swarm capacity, routing stage, and schema pressure do not rewrite those bytes.
MCP definitions are copied from the task's frozen bridge snapshot; later MCP
reachability changes are described by the tail delta carrier rather than by
editing the wire prefix.

The remaining transformations are narrow exceptions, not context injection:

- conversation projection renders structured user-owned fields (reply quotes,
  referenced conversations, plans, documents, images, and videos) into the API
  representation without changing stored Turns;
- `UserPromptSubmit` hooks may deliberately replace the newest request text
  because PII redaction and safety filtering would be defeated if the original
  text remained provider-visible;
- automatic L2/advanced/reactive compaction may remove or summarize history in
  the request-local working copy to satisfy a hard provider context limit;
  manual compaction is the separately authorized persistent operation;
- provider adapters operate on fresh wire bodies and may normalize roles,
  tool-call/result blocks, images, cache-control annotations, or alternating
  message constraints required by the destination protocol;
- Claude OAuth cloaking rebuilds the provider-only system envelope but moves
  the user's system text to a stable carrier after the first user; and swarm checkpoint
  recovery replaces only a stale provider-policy system carrier so withdrawn
  tool authority cannot survive a process restart;
- token counters may construct synthetic counting-only messages. These never
  enter the task projection or conversation authority.

Natural execution appends—assistant output, tool calls/results, loop-breaker
prompts, inbox messages, and user-visible error recovery—are new events, not
rewrites of an earlier message. Any new automatic context feature must use the
tail composer lane; any new exception requires an executable regression test
and an update to this boundary.

Provider reads share one lazy process-wide daemon pool sized from the launch-probed Agent worker budget (`min(8, 2 × agent workers)`). Its pending queue is `max(8, 3 × provider workers)`; saturation degrades optional context with typed timing evidence instead of creating another thread or unbounded queue entry. A permanently wedged adapter can therefore consume only this fixed budget, not one new pool per request.

`ContextPlanV2` is the request-local global budget authority. It classifies objective/constraints, structured task state, evidence/recovery, hot tail, and recoverable cold history.
Required blocks lock first; permission, priority, freshness, access value, and token cost deterministically select optional blocks.
Its manifest records selection/suppression, hashes, token counts, recovery handles, stable segment hashes, and cache epoch. Per-block `max_tokens` remains a hard ceiling but cannot bypass the global budget. Multi-agent task shapes receive only a bounded tail trigger/discipline block; the canonical `spawn_agents` schema is never rewritten with per-round stage or capacity prose.

`TaskStateSnapshotV1` is rebuilt from turn/tool events: goal, hard constraints, decisions, completed work, files, tests, errors, open questions, TODOs, evidence IDs, observation time, and world version.
It is a request projection, never a second transcript authority; assistant prose cannot mark work complete.

The static prompt profile resolves once here. Kimi `auto` remains the full control contract; `lean` and named ablations require explicit experiment policy.
Every request stamps bounded `tofu.prompt-profile/v1` evidence—requested/resolved/effective profile, status/reason, model, character/token counts, SHA-256, and disabled blocks—on the task, `platform_static` provenance, and each round snapshot.
Replace mode records an empty effective profile instead of claiming the selected contract reached the model.

## Automatic versus persistent compaction

Automatic, manual, and provider-native compaction share token and summarization
primitives but not persistence authority. Their preservation rules, cache
economics, bounded receipts, and owner-scoped summary routing are specified in
[`../CONTEXT_COMPACTION.md`](../CONTEXT_COMPACTION.md).

## Memory boundary

Memory retrieval is precision-first and bounded: metadata-only prefetch hydrates selected evidence only, explicit search streams one 2,000-character body prefix at a time, and BM25 retains query-term statistics rather than corpus tokens. It returns evidence to
the composer and injection owns formatting. Stored memories, My Context facts,
and session context stay distinct. After a clean interactive turn, one bounded
learner accepts at most two concise items backed by verbatim real-user evidence.
It rejects uncertain, one-off, synthetic, assistant, or tool content; every accepted change is undoable. As reconstructible background work, it honors billing stops and active shared contention before transport, with at most one actual upstream 429 for an admitted request; skips and failures leave the profile unchanged, and only a later eligible turn retries.

Memory is not a fallback transcript store or source of owner identity. New writes share one bounded payload contract and reject before corpus scans or mutation; one directory sidecar serializes ID selection and atomic publication across threads/cooperating POSIX processes, and suffix collisions use one directory snapshot. Direct-ID CRUD probes the canonical store/root order without enumerating the corpus and hydrates only the target when required. Update/delete/toggle/clear/merge re-resolve under sorted directory-local mutation locks (at most 16 shards per directory), so cooperating writers are linearizable while unrelated shards remain parallel; non-cooperating edits retain revision-conflict protection. Bounded merge probes only its sources. Existing durable files remain readable and are never pruned by this policy.
Each corpus directory uses one closed `scandir` snapshot; flat-entry stat revisions are reused and packages are not enumerated a second time. Retrieval retains a ten-field summary view, hydrates selected IDs by exact canonical probes, and reuses the current prefetch's known availability for Composer guidance rather than rebuilding the corpus.
A bounded launch-probed LRU may retain recursively frozen parsed frontmatter only; matching fingerprints newer than 2.1 seconds are conservatively re-read because valid coarse-clock filesystems can preserve every stat field across a same-size edit. It exposes unstable misses and unfreezable bypasses separately, while each list rebuilds provenance and eligibility and every access carries its authenticated owner.

## Failure semantics

- Exact tokenization unavailable: use the declared heuristic and expose its
  provenance; do not pretend the count is exact.
- Context over budget: compact through registered stages, then return a typed
  context-limit failure if the irreducible payload still does not fit.
- Final-admission summary failure: use a bounded transcript-derived recovery
  receipt; retain usable model text after shaping faults. Manual compaction stays atomic.
- Concurrent manual compaction: reject/retry from a fresh transcript revision.
- Invalid tool history: fail or repair at the single message-construction
  boundary, with diagnostics.

## Invariants

- One context-window authority and one token-counter resolution API.
- Deterministic provider order and observable segment provenance.
- Durable messages are never mutated by request-local preparation; same-role diagnostics classify original pair boundaries using short-lived producer identity that is deleted before provider/debug wire output, so synthetic carriers and relocated objective anchors stay silent while naked real-to-real duplicates still warn.
- Runtime context is append-only at the message level: composer-owned guidance
  uses a trailing user carrier and never extends system content or inserts a
  historical-head user message.
- Persistent compaction is atomic and revision-guarded.
- Turn-native compaction authority stores the public block once; private v1
  markers exist only in the compatibility projection.
- System, project, identity, and safety instructions retain declared priority.
- Tool call/result pairs remain adjacent and correctly identified.
- Memory retrieval is owner-scoped and bounded; My Context review uses the latest real user turn, strict optional billing/contention admission, and at most one actual upstream 429; skips and failures cannot erase base context.
- Prompt-cache decisions stay stable for the lifetime of one task.
- Economic working sets derive from provider/model price tiers without model
  name checks; explicit overrides remain authoritative.
- Turn cost aggregates per API round under that round's model/provider/tier,
  and the UI exposes total, uncached, and cache-read tokens together.
- Required `ContextPlanV2` blocks cannot be evicted by optional evidence.
- Mutable facts carry observation/world-version metadata and are revalidated
  before a claim depends on their freshness.
- Per-round measurement uses the shared local token authority. Its bounded
  digest entries carry encoding-scoped BPE and entropy counts without prompt
  text under a launch-probed 4,096-entry ceiling. Admission may hand off 4,096
  text identities, one schema integer, and one exact digest synchronously;
  identical list/model/ordered schema objects are required; none is serialized.

## Change routing

| Change | Start here | Verify |
|---|---|---|
| New context source | `context_composer/_providers.py` and model | ordering, provenance, budget tests |
| Global context budget or task projection | `context_composer/_render.py`, `task_state.py` | required-block, determinism, permission, cache-epoch tests |
| Model context window | `lib/context_limits/` | self-heal and model-profile tests |
| Tokenizer | `lib/token_counter/` resolver | exact/fallback parity |
| Compaction stage | `lib/tasks_pkg/compaction/_steps.py` | anchors, tool pairs, usage counting |
| Manual compaction | `_manual.py`, `_persist/` | route, concurrency, durable reload |
| Memory selection / My Context learning | `lib/memory/prefetch/`, `relevance/`, `profile_consolidate.py` | owner isolation, grounding, specificity |

## Test map

```bash
pytest -q tests/test_context_composer.py tests/test_context_limits_selfheal.py
pytest -q tests/test_token_counter_heuristic.py tests/test_compaction_invariants.py tests/test_compaction_anchor.py tests/test_compaction_receipt.py
pytest -q tests/test_manual_compaction_engine.py tests/test_manual_compaction_route.py
pytest -q tests/test_memory_prefetch_local.py tests/test_memory_global_server_store.py tests/test_memory_create_allocation.py tests/test_memory_direct_lookup.py tests/test_memory_mutation_concurrency.py
pytest -q tests/test_long_agent_v2_contracts.py -k 'context_plan or task_state or adaptive_compaction'
pytest -q tests/test_prompt_profile_adoption.py
```
