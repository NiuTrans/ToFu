# Module Design Doc — Unit 5: Context Engineering (`system_context`, `compaction/`, `memory/`, `conv_message_builder`, `token_counter/`, `context_limits`)

> Part of the per-module design-doc set (see `docs/ARCHITECTURE.md`). This unit
> is Layer ⑤ (Context): everything that decides WHAT goes into the prompt and
> WHETHER it fits — assembly, injection, memory, token counting, context-window
> limits, and compaction.
>
> **Grounding:** every line count is `wc -l` on disk 2026-07-11. `list_dir`
> overcounts — all numbers are `wc -l`. Every MISCUT/BIG verdict cites competing
> responsibilities or line ranges; size alone is never the argument.
>
> **Scope note:** `system_context.py`, `conv_message_builder.py`, and the
> `compaction/` package physically live in `lib/tasks_pkg/` and their SIZE
> verdicts were introduced in Unit 1. This doc does not re-litigate those; it
> treats them as the context-engineering subsystem and answers the token-budget
> authority question that spans them + `token_counter/` + `context_limits`.

---

## 1. The analytical payload: is there ONE token-budget authority?

The load-bearing question: "how many tokens do we have, and what gets cut" —
is there a single source of truth, or do `system_context`, `compaction`,
`conv_message_builder`, and `token_counter/` each carry their own counting/limit
logic that can silently disagree? A mismatch between the assembler and the
compactor is exactly the class of bug that silently re-bills the whole context
or truncates the wrong thing.

**Verdict: there IS a single authority for each of the two axes, and the two
axes are correctly separated. No silent-disagreement defect.** Traced by
grepping every module that counts tokens or looks up a context-window limit:

### 1a. Token COUNTING — one funnel: `lib/token_counter/`

`token_counter/` is the sole "how many tokens will this cost?" authority. Its
own docstring declares it "Authoritative token counting" and documents the
2026-05-04 post-mortem that motivated retiring the naïve `chars/4` estimator.
Consumers (grepped): `llm/body.py`, `compaction/_layer1`, `compaction/_reactive`,
`compaction/_tokens`, `manager.py`, `tool_dispatch.py`. Its internal design is a
clean resolver: `resolver.resolve(model)` returns an *ordered* backend list
(usage_cache → exact tokenizer → upstream API → tiktoken → heuristic) and
`api.count_tokens` returns the first that succeeds. There is exactly ONE counting
entry point (`count_tokens`) and ONE heuristic (`heuristic.cheap_estimate_text`).

**Critical: the assemblers do NOT count tokens.** Grepping `system_context.py`
and `conv_message_builder.py` for token/estimate/budget logic returns only
`chars` (for the debug-panel trace) — NEITHER computes a token count or a budget.
They assemble; they do not budget. This is the key finding: assembly and
budgeting are *separated by construction*, so they cannot disagree — the
assembler literally has no counting logic to drift from the compactor's.

### 1b. Context-WINDOW LIMIT — one funnel: `compaction/_tokens._get_context_limit` → `context_limits`

The "how big is the window" authority is layered but single-threaded:
- **Static presets** live in ONE place: `compaction/_tokens._get_static_context_limit`
  (the name→limit table: claude 200k/1M, gpt-4 128k, gemini 1M, …).
- **Learned overrides** live in ONE place: `lib/context_limits.py`
  (`lookup_learned_context_limit`, `learn_shrink_from_error`,
  `learn_expand_from_success`) — auto-learned per-`(provider,model)` from real
  overflow errors / accepted prompts, persisted to `server_config.json`.
- **The operational lookup** `_get_context_limit(task)` composes them: static
  preset, then `context_limits.lookup_learned_context_limit` layered on top
  (verified at `_tokens.py:321`). `context_limits` does NOT import `_tokens` —
  the edge is one-way (`_tokens → context_limits`), no cycle.
- **`_clamp_max_tokens`** (Unit 2, `model_info`) clamps the OUTPUT budget; it is
  a distinct concern from the input-window limit and does not compete with it.

So both the compactor's force-compact gate AND the frontend Context Health Bar
read the SAME numbers: `build_context_policy()` (`_tokens.py`) exists precisely
so `static/js/context-bar.js` reads the policy over `/api/v1/server-config`
rather than hard-coding a copy that would drift. The docstring says so verbatim:
"makes this module the single source of truth: the gauge reads numbers, never
re-derives them." That is the anti-duplication mechanism working.

### 1c. The one subtlety worth recording — the heuristic FLOOR on the gate

There is a *deliberate* second counting path, and it is correct, not a defect:
`_count_tokens_authoritative` takes `max(authoritative_count, heuristic_count)`
for the COMPACTION GATE only (not the UI counter). Rationale (in-code): tiktoken's
cl100k under-counts Claude's tokenizer on high-entropy base64/minified content
(observed 0.66× on a real conv), and a gate trusting the lower number can let an
oversized prompt slip past the trigger into the fatal reactive path. So the gate
takes the safe (higher) side while the UI shows the accuracy-optimized count.
This is TWO uses of ONE counter with an explicit safety floor — not two
disagreeing counters. Worth a note only because it looks like a discrepancy until
you read the rationale.

### 1d. The context_limits self-heal invariant (do not "fix")

`context_limits` shrink entries are TTL'd (7 days), expand entries are permanent.
The reason is the **expand-starvation deadlock** documented in the module: once a
wrong shrink caps every prompt below itself, the expand path can never observe
tokens above the wrong ceiling to correct it — only the shrink-side TTL can heal
it. A naive "make expand more aggressive" reintroduces the deadlock. This is the
subtlest correctness invariant in the unit.

---

## 2. Close-read of the `compaction/` split (is it clean beyond the facade?)

Unit 1 called `compaction/` a reference-quality split at the facade level. Close
read confirms it is clean at the SUBMODULE level too, not just the facade:

- **`_tokens.py` (413) imports "nothing from sibling sub-modules except
  `_constants`"** (its own docstring, verified — its only lib imports are
  `_constants` + lazy `token_counter`/`context_limits`/`model_info`). It is pure
  functions of `(messages, task)`, no side effects, no DB, no LLM — "the cleanest
  target for unit tests." That is a genuinely decoupled leaf, not a tangled core.
- **`_budget.py` (185) is a distinct concern from `_tokens.py`** — despite the
  similar names. `_budget` = per-tool-RESULT byte budgeting (`clamp_tool_result_text`,
  `budget_tool_result`, `enforce_round_aggregate_budget`, `mark_empty_result`);
  `_tokens` = whole-prompt token counting + context-limit decisions. They do NOT
  overlap: one budgets a single tool result's size, the other decides if the
  entire context overflows. Correctly separate files.
- **The layering is clean:** `_pipeline` orchestrates; `_layer1`/`_layer2`/`_reactive`
  are the three strategies; `_tokens`/`_budget`/`_constants` are the shared
  primitives they all consume; `_archive`/`_persist` are the durability tail;
  `_steps`/`_builtin_steps`/`_methods`/`_faithful_methods`/`_advanced` are the
  pluggable strategy steps. No submodule reaches into another's internals except
  through `_constants` (shared config) — the same discipline `_sse_core` showed in
  Unit 2. **Confirmed clean beyond the facade.**

The only large sub-file, `_layer2.py` (913), is cohesive (the LLM smart-summary
layer with the objective-anchor logic) — BIG-but-right, as Unit 1 noted.

---

## 3. Module inventory (real `wc -l`, size verdict, status, tests)

### 3.1 Assembly (physically in `tasks_pkg/`)

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `system_context.py` | 1088 | **BIG** | HOT | `test_cc_alignment`, `test_context_trace`, `test_compaction_improvements` |
| `conv_message_builder.py` | 795 | **BIG** | HOT (turn boundary) | `test_conv_message_builder`, `test_conv_message_builder_async_safety` |

`system_context.py` — BIG, and it interleaves two concerns (as Unit 1 flagged):
(a) `_inject_system_contexts` orchestration (project / memory / search-addendum /
swarm blocks wrapped in `<system-reminder>`), (b) the delta-attachment hash/skip
cache + timestamp-stripping for cache-prefix stability. The two share only the
`_trace`/`_ctx_injected` telemetry. Split candidate: separate the delta-attachment
cache. Does NOT count tokens (confirmed §1a).

`conv_message_builder.py` — BIG but ONE cohesive concern: the server-side
`buildApiMessages()` — load raw DB messages → transform (system prompt, endpoint
collapse, dedup, reply-quotes, conv-refs, PDF inline, multimodal image blocks,
`toolRounds`→structured tool_call/tool sequence, merge-consecutive). It is a long
linear pipeline, not multiple unrelated jobs. The `toolRounds` reconstruction is
the heaviest part and is now segment-aware (drives from `segments` when present,
falls back to `toolRounds`, byte-identical by parity gate — the Unit-1
strangler-fig). Does NOT budget tokens (confirmed §1a). BIG-but-cohesive; defer.

### 3.2 Compaction (`tasks_pkg/compaction/`, 16 files, ~7300 LOC)

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `_layer2.py` | 913 | **BIG (cohesive)** | HOT | `test_compaction_methods`, `test_compaction_invariants`, `test_longhorizon_durability` |
| `_persist.py` | 705 | OK | HOT | `test_compaction_improvements` |
| `_faithful_methods.py` | 520 | OK | live | `test_compaction_faithful_methods` |
| `_constants.py` | 290 | OK (config) | HOT | `test_compaction_config_threading` |
| `_reactive.py` | 441 | OK | HOT | `test_compaction_invariants` |
| `_tokens.py` | 413 | OK (pure, decoupled) | HOT | `test_compaction_gate_tokens`, `test_first_run_token_lifecycle` |
| `_steps.py` | 412 | OK | HOT | `test_compaction_step_refactor` |
| `_methods.py` | 540 | OK | HOT | `test_compaction_methods`, `test_compaction_opencode_methods` |
| `_layer1.py` | 324 | OK | HOT | `test_compaction_invariants` |
| `_pipeline.py` | 244 | OK | HOT | `test_compaction_improvements` |
| `_builtin_steps.py` | 458 | OK | HOT | `test_compaction_step_refactor` |
| `_archive.py` | 217 | OK | live (durable) | `test_file_history_compaction` |
| `_advanced.py` | 162 | OK | live | `test_compaction_advanced` |
| `_budget.py` | 185 | OK | HOT | `test_compaction_usage_counting` |
| `_compaction_usage.py` | 112 | OK | HOT | `test_compaction_usage_counting` |
| `__init__.py` | 222 | OK (facade) | — | — |

Whole package: **correctly split, clean beyond the facade (§2).** No action.

### 3.3 Token counting (`lib/token_counter/`, 12 files, 1659 LOC)

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `usage_cache.py` | 233 | OK | HOT | via counter e2e |
| `base.py` | 195 | OK | HOT | — |
| `anthropic_api.py` | 179 | OK | HOT | — |
| `hf_counter.py` | 167 | OK | live | — |
| `api.py` | 152 | OK (entry) | HOT | `test_token_counter_heuristic` |
| `gemini_api.py` | 137 | OK | live | — |
| `resolver.py` | 135 | OK | HOT | — |
| `heuristic.py` | 119 | OK | HOT | `test_token_counter_heuristic` |
| `tiktoken_counter.py` | 117 | OK | HOT | — |
| `deepseek_counter.py` | 83 | OK | live | — |
| `__init__.py` | 77 | OK (facade) | — | — |
| `config.py` | 65 | OK | HOT | — |

**Reference-quality package.** One backend per file, `resolver.py` decides
priority per model, `api.count_tokens` is the single entry. Adding a provider is
one file + one resolver line (the plugin pattern done right). NO module exceeds
233 lines. Nothing to split — this is what good decomposition looks like.

### 3.4 Context limits (`lib/context_limits.py`, 427 LOC)

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `context_limits.py` | 427 | OK | HOT | `test_context_limits_selfheal` |

OK. One cohesive concern (auto-learned per-`(provider,model)` window with the
shrink-TTL / expand-permanent self-heal + big-drop strike gate). Reads/writes only
`server_config.json` via `update_json_atomic` (serialized against the other
concurrent writers). Well-bounded; the complexity is intrinsic to the self-heal
correctness (§1d), not miscut.

### 3.5 Memory (`lib/memory/`, 10 files, 3833 LOC)

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `storage.py` | 827 | **BIG** | HOT | `test_memory_global_server_store` |
| `prefetch.py` | 814 | **BIG** | HOT | `test_prefetch_path_reconcile`, `test_streaming_and_prefetch` |
| `user_profile.py` | 656 | **BIG** | live | `test_user_profile` |
| `installer.py` | 398 | OK | live | via skill-install e2e |
| `relevance.py` | 353 | OK | HOT | `test_relevance_cjk` |
| `profile_consolidate.py` | 217 | OK | live | `test_user_profile` |
| `catalog.py` | 205 | OK | live | via installer e2e |
| `tools.py` | 190 | OK | HOT | via memory-tool e2e |
| `injection.py` | 122 | OK | HOT | `test_cc_alignment` |
| `__init__.py` | 51 | OK (facade) | — | — |

`storage.py` — **BIG, bundles 2 concerns:** the memory CRUD + file I/O AND the
BM25 index (`search_memories`). The search/index cluster is separable from the
persistence cluster. Split candidate: `memory/search.py`.

`prefetch.py` — **BIG, but cohesive:** the per-turn BM25→cheap-LLM→inject
pipeline (`<relevant_memories>` block). One flow, heavily commented (the
path-reconcile + relevance-filter logic). BIG-but-cohesive; defer.

`user_profile.py` — **BIG.** The durable user-preference profile (the
`[USER PREFERENCE PROFILE]` block). Bundles profile CRUD + the LLM-driven
extraction/update + serving. `profile_consolidate.py` (the merge pass) was already
extracted. The extraction concern could split from the storage/serve concern.
Split candidate; defer.

`injection.py` — OK, and notable: it holds the `build_memory_context` that is
**cache-critical** — the memory-count hint is deliberately count-FREE so a
create/delete_memory mid-turn doesn't rewrite the cached system-prompt prefix
(heavily commented; pairs with Units 1/2/3 cache invariants).

**Memory is a well-decomposed package** — only `storage.py` has a genuine 2-concern
split; the rest are single-purpose.

---

## 4. Dependencies (in / out)

**Assembly inbound:** `routes/chat.py` → `conv_message_builder.build_api_messages_from_db`
(the primary turn-start path — POST body is just `{convId, config}`);
`orchestrator`/`endpoint` → `system_context._inject_system_contexts` per round.

**Token/limit inbound:** `compaction` (gate), `llm/body` (output clamp),
`manager`/`tool_dispatch` (accounting) → `token_counter.count_tokens`;
`compaction/_tokens._get_context_limit` → `context_limits.lookup_learned_context_limit`;
`llm_dispatch` (Unit 2) → `context_limits.learn_shrink/expand` on overflow/success.

**Memory inbound:** `system_context._inject_system_contexts` → `memory.build_memory_context`
(the hint) + `memory.prefetch` (the `<relevant_memories>` block); the memory tools
(Unit 3 handlers) → `memory.storage` CRUD.

**Outbound / key edges:**
- `_tokens → context_limits` (one-way; no cycle).
- `token_counter` → provider SDKs (tiktoken/deepseek/HF/anthropic count API),
  all guarded imports with heuristic fallback.
- `context_limits` → `server_config.json` via `json_store.update_json_atomic`
  (serialized against routes/config.py, model_info, dispatcher, health_local).
- Memory → `lib/database` (global store) + project `.tofu/` (project store).

**No back-edges up into routes/tasks orchestration from token_counter/context_limits/
memory** — they are leaf-ish services consumed by the assembler + compactor.

---

## 5. Invariants (must not be broken by a refactor)

1. **`token_counter.count_tokens` is the SINGLE counting authority.** Do not
   reintroduce a `chars/4` estimate anywhere (the 2026-05-04 post-mortem). New
   counting needs go through the resolver (one backend file + one resolver line).
2. **`_get_context_limit` is the SINGLE window-limit lookup** (static preset +
   learned override). Assemblers must NOT compute their own limit.
3. **The compaction GATE uses `max(authoritative, heuristic)`; the UI counter does
   not** (§1c). Preserve the floor — a gate trusting the lower count lets oversized
   prompts into the reactive path.
4. **`context_limits`: shrink is TTL'd, expand is permanent** (§1d — the
   expand-starvation deadlock). Do not make expand more aggressive to "fix" a
   stuck limit; only the shrink TTL heals it.
5. **Cache-prefix stability spans this whole unit:** `system_context` timestamp
   strip, `memory/injection` count-free hint, `attachments` delta-skip,
   `server_message_store` tool-result truncation all avoid mutating bytes inside
   the cached prefix. Pairs with Units 1/2/3.
6. **The OBJECTIVE ANCHOR (first real user msg) is preserved verbatim across
   compaction** (`_layer2._objective_anchor_index`; `_reactive._head_truncate`
   skips it). Same definition as autopilot `_extract_objective` — ONE notion of
   objective. Idempotent (reuses the genuine message, never a synthesized prepend).
7. **`conv_message_builder` NEVER merges structured tool-call sequences**
   (tool_calls / tool_call_id) — they must stay intact for call↔result
   correlation; only plain same-role text merges.
8. **Compaction retention caps are §10 hyperparameters** (`TOFU_COMPACTION_ARCHIVE_RETENTION`,
   summary-trigger ratio, reserves, TTL days).

---

## 6. Known debt (grounded)

- **`system_context.py` (1088) interleaves injection orchestration + the
  delta-attachment cache** (§3.1) — a clean split seam.
- **`memory/storage.py` (827) bundles CRUD + BM25 search** (§3.5).
- **`memory/user_profile.py` (656) bundles extraction + storage/serve** (§3.5).
- **`conv_message_builder.py` (795) is BIG** but one cohesive pipeline — the
  `toolRounds`/segment reconstruction is the heaviest sub-concern.
- No cross-subsystem token-authority duplication (the thing this unit was tasked
  to find) — that class of defect is ABSENT here.

---

## 7. Segmentation verdict (this unit)

**Correctly bounded — leave as-is:**
The entire `token_counter/` package (reference-quality, one-backend-per-file);
`context_limits.py`; the entire `compaction/` package (clean beyond the facade,
§2); memory `injection`, `relevance`, `tools`, `catalog`, `installer`,
`profile_consolidate`.

**Miscut — should split (priority order):**

1. **`memory/storage.py` (827) → extract `memory/search.py`** for the BM25
   index/search cluster, leaving CRUD + file I/O in `storage.py`. Behind
   `test_memory_global_server_store` + `test_relevance_cjk`.
2. **`system_context.py` (1088) → extract the delta-attachment cache** (the
   hash/skip machinery) from the `_inject_system_contexts` orchestration. Shared
   only via `_trace` telemetry. Behind `test_cc_alignment` + `test_context_trace`.
   (This is the same file Unit 1 flagged; recorded here in its context-engineering
   role.)

**Big but optional (defer unless touched):**
`conv_message_builder.py` (795 — one cohesive pipeline), `memory/prefetch.py`
(814 — cohesive), `memory/user_profile.py` (656 — extraction/storage seam),
`compaction/_layer2.py` (913 — cohesive summary layer).

**Do NOT split:** `token_counter/` modules, `compaction/_tokens.py` (pure +
decoupled by design), `_constants.py`, `context_limits.py` (intrinsic self-heal
complexity), `memory/injection.py` (cache-critical, small).

---

## 8. Comparison to Units 1–4 (the running thesis)

- **The feared duplication is ABSENT.** Unlike Unit 4's live endpoint/autopilot
  dual-implementation, there is NO competing token-counter or context-limit
  logic: counting funnels through `token_counter.count_tokens`, the window limit
  through `_get_context_limit → context_limits`, and — the decisive fact — the
  assemblers (`system_context`, `conv_message_builder`) carry NO counting/budgeting
  logic at all. Assembly and budgeting are separated by construction, so they
  cannot silently disagree.
- **`token_counter/` and `compaction/` are the two best-decomposed packages in the
  whole survey so far** — cleaner than `tasks_pkg` (Unit 1), on par with `swarm/`
  (Unit 4). Both prove the clean-split pattern is the codebase norm when a
  subsystem gets deliberate attention.
- **The subtle risks here are CORRECTNESS invariants, not segmentation** (the
  gate's heuristic floor §1c, the expand-starvation TTL §1d, the objective anchor
  §5.6). A refactor that respects module boundaries could still break these — so
  this unit's "known debt" is thin on structure but the invariants are load-bearing.
- The only structural finds are two clean single-file 2-concern splits
  (`memory/storage`, `system_context`) — small, low-risk, not the file-scale
  miscuts of `manager.py`/`api.py`.

---

*Next unit: Unit 6 (Conversations & Project Brain — `conversations/` charter,
board, dispatch, peer, status, watch, reconcile). NOTE: heavily contested by
uncommitted sibling epics per the board — document, do not refactor.*
