# Context engineering

This domain turns durable conversation state, project state, memory, tool history,
and model limits into the bounded message sequence sent to an LLM. It also owns
automatic and user-requested compaction. The durable transcript authority is
[`../STORAGE.md`](../STORAGE.md).

## Ownership

| Concern | Owner |
|---|---|
| Context assembly and provider ordering | `lib/tasks_pkg/context_composer/` |
| System/project/user context sources | `lib/tasks_pkg/context_composer/_providers.py` |
| Model context-window policy | `lib/context_limits/` |
| Token counting | `lib/token_counter/` |
| Automatic compaction | `lib/tasks_pkg/compaction/` |
| Rebuildable task-state projection | `lib/tasks_pkg/context_composer/task_state.py` |
| Persistent manual compaction | `lib/tasks_pkg/compaction/_manual.py`, `_persist/` |
| Long-term memory retrieval/injection | `lib/memory/` |
| Conversation message construction | `lib/tasks_pkg/conv_message_builder/` |
| Cost experiments and trace | `lib/cost_experiments.py`, context trace modules |

## Assembly flow

1. A task receives an immutable view of authenticated conversation and project state.
2. `context_composer` invokes declared providers in deterministic order, records provenance, and reuses one owner-scoped, storage-filtered snapshot across request-local views.
3. Memory prefetch selects bounded evidence; it does not mutate the transcript.
4. The token counter resolves the model-specific counter, with an explicit heuristic fallback when no exact counter exists.
5. Context-limit policy computes the usable input budget after output and safety reserves.
6. Compaction removes, folds, or summarizes only through registered steps.
7. The final request body is built once by the LLM body builder.

Tool-round continuations must preserve the provider/model binding and stable
prefix ordering from round zero. Providers do not silently reread mutable
globals on later rounds.

`ContextPlanV2` is the request-local global budget authority. It classifies blocks
as objective/constraints, structured task state, evidence/recovery, hot tail, or
recoverable cold history. Required blocks are locked first; optional blocks are
selected deterministically by permission, priority, freshness, access value, and
token cost. Its manifest records each selection, suppression, hash, token count,
recovery handle, stable segment hashes, and cache epoch. A block's own
`max_tokens` remains a hard ceiling but cannot bypass the global budget.

`TaskStateSnapshotV1` is rebuilt from turn and tool events. It contains the goal,
hard constraints, decisions, completed work, files, tests, errors, open questions,
TODOs, evidence IDs, observation time and world version. It is a request projection,
never a second transcript authority; assistant prose cannot mark work complete.

The static prompt profile is resolved once at this boundary. Kimi `auto` remains
the full control contract; `lean` and named ablations require an explicit
experiment policy. Every request stamps bounded `tofu.prompt-profile/v1`
evidence—requested, resolved and effective profile, status/reason, model,
character/token counts, SHA-256 and disabled blocks—on the task,
`platform_static` provenance and each round snapshot. Replace mode records an
empty effective profile instead of claiming the selected contract reached the model.

## Automatic versus persistent compaction

Automatic compaction is request preparation: it produces a bounded working view
without rewriting durable turns. Manual compaction is an explicit conversation
mutation: it writes a persistent summary boundary through the conversation
authority with concurrency protection against intervening turns.

The per-round L1 pass is incremental at the authority boundary. A no-op does not
load the transcript merely to rediscover compact placeholders; settled-turn
ownership resolves lazily only when a real tool or image mutation needs a durable
stamp. Image-tail and text-tool placeholders update the same settled Turn
projection, so base64 payloads do not return on the next request rebuild.

Both paths may share summarization and token-budget primitives, but they do not
share persistence semantics. A request-local summary must never be mistaken for
the stored transcript, and a persistent compaction must never bypass the
conversation command service.

Turn-native manual compaction persists one canonical public `compaction` block
through the Sidecar operation. Private runtime markers are never transcript
authority; the read-only legacy projection adapter reconstructs the v1 marker
fields for old consumers. This keeps old readers compatible without creating a
second stored representation of the summary boundary.

The first real user anchor and required system/project policy remain available
after compaction. Tool-call/result pairing stays valid; orphan results or
reordered tool IDs are contract violations.

Automatic L2 and manual `/compact` select the newest contiguous complete
tool-round suffix under the same preservation token budget used for turns. The
configured hot-round count is a maximum, not an unlimited entitlement: an
oversized recent read is folded while the newest complete call/result pair
remains recoverable. Their summary is a compact state receipt (objective,
binding constraints, verified work, current state, blockers, next steps); the
objective and a bounded recent instruction set are retained verbatim outside
that lossy receipt, and reconstructible `data/tool-results` paths are never
promoted to durable working files. The model targets 800–1,600 receipt tokens
with a hard 2,200-token dispatch ceiling.

Proactive cache economics include a conservative summary-call estimate before
dispatch. Once a summary has been generated, that cost is sunk and adoption
compares only the future prefix rewrite with future input savings. Archive and
UI token counters are explicitly heuristic (`tokenCountKind=estimated`), not
provider-billed usage.

The local token authority reuses exact-ish counts for byte-identical text above
4 KiB. Cache keys contain only tokenizer encoding, character length, and a
SHA-256 digest; prompt/schema text is never retained. Capacity comes from the
launch resource profile (`TOFU_TOKEN_COUNT_CACHE_CAPACITY`) and is hard-capped
at 4,096 entries. On an 80,099-byte production tool catalog, a warmed tokenizer
took 6.13 ms per uncached count versus 0.059 ms for a digest hit (about 104×),
while short or changing text stays uncached.

Each automatic L2 preflight produces two deliberately distinct measurements:
the request gate includes tool schemas, while the message-only estimate retains
the retry, archive, analytics, and reminder contract. The message estimate is
reused through the synchronous no-mutation path; any L2/advanced mutation
invalidates it and forces a count of the resulting context. Cache economics use
the durable previous-turn warm-read baseline across one or two cold observations.
At the existing third consecutive verifiably-cold round, current-task evidence
supersedes that stale fallback so a dead cache cannot permanently block useful
compaction.

Within the request gate, the broad heuristic prefilter is lazy. A usage-cache
hit or successful local tokenizer returns without paying for a discarded
request-wide heuristic scan. Network counters still use that prefilter at the
same threshold, and a later heuristic fallback reuses the already-computed
value. A usage-cache hit verifies only the bounded recorded-prefix tail instead
of copying the complete historical message list.

Adaptive L2 compaction is opt-in. It compares projected Kimi cache-adjusted input
savings with compaction-call and evidence-loss cost; context-window safety remains
an unconditional hard gate. Generated state is checked against pending work and
the evidence ledger; failed validation retains a deterministic bounded view.
`remainingRoundsMedian` is also the horizon for exact pre/post-summary checks.
Fixed compaction starts at one round, then earns horizons 2/3/4/5/6 after
completing 4/8/16/32/64 rounds, capped by the remaining API-round budget.
Adaptive candidates are no longer admitted then silently vetoed by a fixed gate.

An automatic economic decline records the optimistic token-growth lower bound
at which the candidate could first repay its cache rewrite or meet the minimum
reduction. Until that bound, later rounds reuse the preflight veto instead of
rebuilding the same fold candidate. A lower warm-cache witness or a newly
earned fixed-policy horizon invalidates the veto immediately; explicit/reactive
compaction and the hard window gate always bypass it.

Pre-compaction transcript archives remain exact audit snapshots. Archive
metadata/summary is fetched independently of the potentially multi-megabyte
message payload; clients load raw messages only on explicit inspection, copy,
or download. A separate bounded `tofu.compaction-receipt/v1` describes what the
compactor actually did: strategy and fallback, preserved anchors/turns/tool
rounds/files, summary time and normalized token usage, cache payback, evidence
counts, and recovery truncation. This user-facing receipt is stored with the
archive and is never injected into model context; enriching inspection cannot
make the next provider request larger.

## Memory boundary

Memory retrieval is precision-first and bounded; retrieval returns evidence to
the composer and injection owns formatting. Stored memories, My Context facts,
and session context stay distinct. After a clean interactive turn, one bounded
learner accepts at most two concise items backed by verbatim real-user evidence.
It rejects uncertain, one-off, synthetic, assistant, or tool content; every accepted change is undoable.

Memory is not a fallback transcript store. It may summarize reusable facts, but
must not become an alternate source of conversation truth or owner identity.
Every persisted memory access carries the authenticated owner boundary.

## Failure semantics

- Exact tokenization unavailable: use the declared heuristic and expose its
  provenance; do not pretend the count is exact.
- Context over budget: compact through registered stages, then return a typed
  context-limit failure if the irreducible payload still does not fit.
- Summarizer/provider failure: retain a safe non-summarized tail or fail the
  request explicitly; never persist a partial manual compaction.
- Concurrent manual compaction: reject/retry from a fresh transcript revision.
- Invalid tool history: fail or repair at the single message-construction
  boundary, with diagnostics.

## Invariants

- One context-window authority and one token-counter resolution API.
- Deterministic provider order and observable segment provenance.
- Durable messages are never mutated by request-local preparation.
- Persistent compaction is atomic and revision-guarded.
- Turn-native compaction authority stores the public block once; private v1
  markers exist only in the compatibility projection.
- System, project, identity, and safety instructions retain declared priority.
- Tool call/result pairs remain adjacent and correctly identified.
- Memory retrieval is owner-scoped, bounded, and optional; failure cannot erase
  the base conversation.
- Prompt-cache decisions stay stable for the lifetime of one task.
- Required `ContextPlanV2` blocks cannot be evicted by optional evidence.
- Mutable facts carry observation/world-version metadata and are revalidated
  before a claim depends on their freshness.

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
pytest -q tests/test_token_counter_heuristic.py \
  tests/test_compaction_invariants.py tests/test_compaction_anchor.py \
  tests/test_compaction_receipt.py
pytest -q tests/test_manual_compaction_engine.py \
  tests/test_manual_compaction_route.py
pytest -q tests/test_memory_prefetch_local.py \
  tests/test_memory_global_server_store.py
pytest -q tests/test_long_agent_v2_contracts.py -k 'context_plan or task_state or adaptive_compaction'
pytest -q tests/test_prompt_profile_adoption.py
```
