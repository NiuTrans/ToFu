# Module Design Doc — Unit 2: LLM I/O (`llm/`, `llm_dispatch/`, `model_info`, `context_limits`, `llm_sanitize`)

> Part of the per-module design-doc set (see `docs/ARCHITECTURE.md` for the
> 8-layer panorama). This unit is Layer ⑦ (LLM Dispatch) + the wire/transport
> half of Layer ⑧: everything between "the task engine handed me a `messages`
> list + a model name" and "bytes on the wire to a provider, and a parsed
> assistant message back".
>
> **Grounding:** every line count is `wc -l` on disk 2026-07-11, read directly.
> Per the Unit-1 finding, `list_dir` overcounts this tree too — all numbers
> here are `wc -l`, cross-checked against `read_files` line numbering. Every
> **MISCUT/BIG** verdict cites the specific competing responsibilities or line
> ranges; size alone is never the argument.

---

## 1. Responsibility

This unit answers one request: **"send these messages to the best available
model and give me back a parsed assistant turn (streaming or not), surviving
provider quirks, rate limits, dead endpoints, and context overflow."**

The request flows through four concerns, each a distinct module cluster:

1. **Model knowledge** (`model_info`) — what family is this? what's its output
   cap? does it need thinking-signature replay? Pure predicates + a learned
   per-model output-limit store.
2. **Body construction + sanitization** (`llm/body`, `llm_sanitize`) — turn the
   OpenAI-shape `messages` + knobs into a provider-correct request body:
   per-family thinking params, image validation/downscale, orphaned-tool-call
   repair, gateway keyword sanitization.
3. **Transport** (`llm/` package) — open the stream (sync `requests` /
   async `httpx`), parse SSE, accumulate the assistant message, classify
   errors, retry transient failures. Includes the Anthropic-Messages outbound
   adapter and the prompt-cache breakpoint annotator.
4. **Dispatch / routing** (`llm_dispatch/`) — pick the fastest healthy
   `(api_key, model)` slot from a pool, load-balance, fail over, cool down
   dead/throttled slots, keep a conversation sticky to its warm-cache key,
   auto-discover models from a provider, and mint request-scoped ephemeral
   slots for BYO callers.

Plus two cross-cutting learned-state modules: `context_limits` (per-provider
context-window auto-learning) and the error taxonomy (`llm_errors`).

It does NOT own: the tool-calling loop (Unit 1), token *counting*
(`lib/token_counter/`, Unit 5), OAuth token resolution (`lib/oauth/`, reached
via a lazy hook), or the inbound OpenAI/Anthropic *server* compat surfaces
(`lib/compat/`, Unit 12 — the inverse direction of `anthropic_outbound`).

**Total size:** `llm/` 3178 + `llm_dispatch/` 6535 + `model_info` 554 +
`context_limits` 427 + `llm_sanitize` 493 + `llm_errors` 392 +
`llm_error_format` 50 = **~11,600 lines** across 25 files.

---

## 2. The analytical payload: is the `llm/` split clean, or a giant-core-left-behind?

Unit 1 surfaced two split outcomes in this codebase:
- **`compaction/`** — a *clean* package split: pure re-export facade + cohesive
  sub-modules, public API unchanged, no giant core left behind.
- **`orchestrator.py`** — an *incomplete* split: helpers extracted *around* a
  monolithic 1500-line `run_task` that stayed put.

**Verdict for `llm/`: it is the CLEAN pattern, like `compaction/` — not the
orchestrator anti-pattern.** Evidence, not assertion:

1. **The facade is a pure re-export.** `llm/__init__.py` (133 lines) is entirely
   `from .submodule import …` + `__all__`; it contains zero logic. Same shape
   as `compaction/__init__.py`.

2. **The one genuinely-big file is a SHARED CORE, not a leftover monolith.**
   `_sse_core.py` (805) exists *because of* a deduplication: its docstring
   documents that `stream.py` and `astream.py` each used to carry a ~480-line
   copy of the identical SSE parse loop, and "every fix had to land twice and
   the two copies drifted." The core now holds `prepare_request` +
   `classify_status_error` + the `SSEAccumulator` class once; the two transport
   shells (`stream.py` 165, `astream.py` 195) keep ONLY what differs
   (`requests` vs `httpx`, blocking vs `await` sleep, transport-exception
   mapping). This is the *opposite* of a left-behind core — it's the extraction
   that removed the duplication. The proof is `test_sse_core_parity.py` +
   `test_async_dispatch_stream.py`: sync and async go through the same core.

3. **Every sub-module is one cohesive concern:** `body.py` (build the body),
   `cache.py` (Anthropic cache breakpoints), `chat.py` (non-stream),
   `anthropic_outbound.py` (OpenAI↔Anthropic wire translation),
   `diagnostics.py` (RawSSEDumper), `_transport.py` (retry constants + sleep).
   No sub-module reaches into another's internals except through the facade.

So `llm/` is a **reference-quality split** and Unit 2's structural debt is NOT
here — it is entirely in `llm_dispatch/api.py` (§3.4, §8).

---

## 3. Module inventory (real `wc -l`, size verdict, status, tests)

Verdict legend: **OK** = correctly bounded; **BIG** = large but cohesive;
**MISCUT** = doing 2+ unrelated jobs, should split. Status: **HOT** = per-round
hot path (most carry a `# HOT_PATH` header); **live** = used, not hot; **leaf** = small utility.

### 3.1 `llm/` package — transport + body (3178 LOC, 10 files)

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `_sse_core.py` | 805 | **BIG (justified)** | HOT | `test_sse_core_parity`, `test_codex_sse_accumulator`, `test_sdk_sse_parser`, `test_zero_byte_round0_retry` |
| `body.py` | 765 | **BIG** | HOT | `test_cache_breakpoints`, `test_cache_schema_stability` (via build_body) |
| `anthropic_outbound.py` | 427 | OK | HOT | `test_anthropic_outbound` |
| `chat.py` | 254 | OK | HOT (non-stream) | via chat e2e |
| `astream.py` | 195 | OK | HOT | `test_async_dispatch_stream` |
| `cache.py` | 183 | OK | HOT | `test_cache_breakpoints`, `test_cache_prefix_stability` |
| `diagnostics.py` | 172 | OK | HOT | via SSE e2e |
| `stream.py` | 165 | OK | HOT | `test_dispatch_stream`, `test_sse_resume_guard` |
| `__init__.py` | 133 | OK (facade) | — | — |
| `_transport.py` | 80 | leaf | HOT | — |

`_sse_core.py` — **BIG but the size is intrinsic, not miscut.** It is ONE
concern (parse an SSE stream into an assistant message) that happens to be
large because it handles 3 wire dialects through one accumulator
(`_process_openai_chunk` + `_feed_anthropic` + `_feed_codex`), MiniMax inline
`<think>` demux, phantom-tool-call filtering, and the full anomaly-diagnostics
suite (`_missing_done`/`_empty_stop`/`_stream_anomaly` flags that
`stream_handler.analyse_stream_result` keys its retry buckets off). Splitting it
would re-introduce the sync/async duplication it was created to remove. Leave it.

`body.py` — **BIG, and here the size IS partly a boundary question.**
`build_body` (532–765, ~230 lines) is a per-family thinking/temperature
if-ladder (Claude/Kimi/GLM/Doubao/LongCat/Qwen/ERNIE/Gemini/MiniMax + the
plugin-dialect hook). The image concerns (`_validate_image_blocks` 94–248,
`_downscale_oversized_images` 249–337, `sniff_image_mime`) are a *separable*
cluster — ~250 lines that share nothing with the thinking-param logic except
living in the same file. Split candidate (§8): `llm/_images.py`. The thinking
ladder itself is cohesive and stays.

### 3.2 Model knowledge + errors (single modules)

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `model_info.py` | 554 | OK | HOT | `test_frontend_thinking_content_render_swap` (probes), family-detect used everywhere |
| `llm_sanitize.py` | 493 | OK | HOT | `test_artifacts_meta_sanitize` (indirect), orphan-fix via e2e |
| `llm_errors.py` | 392 | OK | HOT | `test_context_limits_selfheal` (classifier), dispatch e2e |
| `llm_error_format.py` | 50 | leaf | live | — |

`model_info.py` — OK. Two concerns (family-detect predicates + per-model
output-limit store) but they're tightly related (limits are keyed by family)
and the file is under 600 lines. The `_qwen_max_output`/`_kimi_max_output`/
`_ernie_max_output` per-model tables are data, not logic. Leave it.

`llm_sanitize.py` — OK. Cohesive: every function is a pure message-list
transform feeding `build_body` (`_fix_orphaned_tool_calls`,
`_fix_tool_call_adjacency`, `_merge_consecutive_same_role`,
`_fix_empty_user_messages`, gateway keyword sanitize). The Anthropic
adjacency repair (`_fix_tool_call_adjacency`, the biggest fn) is the load-bearing
one — it prevents the recurring HTTP 400 tool_use/tool_result class.

`llm_errors.py` — OK. The whole error taxonomy + `_classify_http_error` (the
central always-raises dispatcher) + the pattern tables. This is the single
source of truth every transport shell and the dispatcher consult; correctly one file.

### 3.3 `llm_dispatch/` — the well-split parts (routing primitives)

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `dispatcher.py` | 1210 | **BIG (one class)** | HOT | `test_chat_flow_dispatch`, `test_direct_dispatch_tool_alias`, `test_provider_pin` |
| `discovery.py` | 939 | **BIG** | live (setup path) | via probe e2e |
| `slot.py` | 463 | OK | HOT | `test_provider_registry` (thinking_format), slot scoring e2e |
| `health_local.py` | 503 | OK | live (bg thread) | `test_env_health` (adjacent) |
| `config.py` | 476 | OK (mostly data) | HOT | `test_cache_schema_stability` (aliases) |
| `ephemeral.py` | 399 | OK | HOT (BYO) | `test_ephemeral_slot` |
| `conv_affinity.py` | 210 | OK | HOT | `test_conv_affinity` |
| `provider_registry.py` | 203 | OK | live | `test_provider_registry` |
| `provider_pin.py` | 112 | OK | HOT | `test_provider_pin` |
| `factory.py` | 88 | leaf | HOT | via all dispatch tests |
| `__init__.py` | 63 | OK (facade) | — | — |

`dispatcher.py` — **BIG but it is ONE class** (`LLMDispatcher`, 24 methods).
Its methods cluster into two genuine concerns: **slot construction**
(`_build_slots` / `_build_slots_from_providers` 253–465 / `_build_slots_from_env`
466–570 / `_load_benchmark_data` / `_build_alias_index` / discovery+persist glue —
~600 lines) and **slot selection** (`_pick` 780–964 / `pick_top_n` / `pick_slot` /
`has_capable_slots` / `sticky_cooldown_remaining_s` — ~400 lines). These could be
a mixin split, but they share the `self.slots` + `self._lock` + `self._alias_index`
state intimately, so a split is lower-value than the `api.py` one below. Classified
BIG, defer (§8).

`discovery.py` — **BIG, and it has a real internal seam.** Two concerns: (a) the
network I/O — `discover_models` (327–469), `enrich_models_with_pricing`
(470–601), `probe_provider` (831+, the one-shot setup orchestrator); and (b) the
pure heuristics — `is_local_endpoint`/`is_raw_ip_host`/`should_bypass_proxy`
(105–224, endpoint classification), `_infer_capabilities`/`_infer_rpm`/`_infer_cost`
(225–326, name→caps), `_detect_brand` (602–677, the `_DOMAIN_BRAND_MAP` table),
`_detect_thinking_format` (775–830). The heuristic half is pure + independently
testable and could split to `discovery_heuristics.py`. Not urgent (it's a
setup-path module, not the per-round hot path). Classified BIG, defer.

`slot.py` — OK. The `Slot` dataclass owns its own live stats + `score()` +
`record_success`/`record_error`/`record_truncation`. Cohesive by design (all
state + behaviour for one routing target in one place). ~460 lines is
appropriate for the amount of live rate-limit/cooldown/EMA state it tracks.

`config.py` — OK. ~380 of its 476 lines are the `DEFAULT_SLOT_CONFIGS` reference
table (every known model → caps/rpm/latency/cost) + `MODEL_ALIAS_GROUPS`. The
actual logic (`get_pricing_tiers`, `reevaluate_pricing_tags`) is compact and
single-purpose. Data-heavy, not miscut. Leave it.

The five small routing modules (`conv_affinity`, `provider_pin`,
`provider_registry`, `ephemeral`, `factory`) are each **exemplary single-concern
extractions** — the kind of split this program is trying to reproduce
elsewhere. `conv_affinity` (prompt-cache-key stickiness) and `provider_pin`
(thread-scoped hard provider binding) are near-mirror-image thread-local
mechanisms, each ~110–210 lines, each with its own test.

### 3.4 `llm_dispatch/api.py` — the one genuine MISCUT (1869 LOC)

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `api.py` | 1869 | **MISCUT** | HOT | `test_dispatch_stream`, `test_async_dispatch_stream`, `test_chat_flow_dispatch` |

`api.py` is the largest file in the unit and bundles **four separable concerns**
(cited by symbol + line range):

1. **The retry/failover loops** — `dispatch_chat` (193–534, ~340 lines),
   `dispatch_stream` (895–1336, ~440 lines), `async_dispatch_stream`
   (1337–1559, ~220 lines). Three parallel loops that pick a slot → call the
   transport → on failure cool the slot + rotate → retry. `dispatch_stream` and
   `async_dispatch_stream` are a sync/async pair with the SAME duplication
   smell `_sse_core` already fixed one layer down — but at the dispatch layer
   it was NOT deduplicated (unlike `stream`/`astream`).
2. **Slot-swap body re-targeting** — `_readjust_thinking_params` (535–684) +
   `_adapt_stream_body_for_slot` (685–738): when failover picks a slot for a
   *different* model, the already-built body's thinking params must be
   re-shaped. ~200 lines, self-contained.
3. **Retry-state bookkeeping** — the `_StreamRetryState` class (739–894, ~155
   lines) + `_cool_slot_on_premature_close` + `_audit_severity_downgrade`.
4. **The fan-out / convenience API** — `dispatch_fastest` (1560), `dispatch_parallel`
   (1690), `smart_chat` (1769), `smart_chat_batch` (1826), `get_dispatch_status`.

This is the same shape as Unit 1's `manager.py`/`orchestrator.py` miscut: a hot
file where 3–4 clean internal boundaries already exist but were never extracted.
Split plan in §8.

---

## 4. Dependencies (in / out)

**Inbound:** The whole unit is reached through two facades:
- `from lib.llm import build_body, stream_chat, chat, async_stream_chat` — used
  by `tasks_pkg.manager.stream_llm_response`, `tasks_pkg.llm_fallback`,
  `tasks_pkg.orchestrator`, paper/swarm/translate engines.
- `from lib.llm_dispatch import dispatch_chat, dispatch_stream, get_dispatcher` —
  the routing entry. `manager.stream_llm_response` calls `dispatch_stream`;
  aux cheap-model calls (compaction summaries, discovery) call `dispatch_chat`.

**Outbound:**
- `lib.oauth.outbound` / `lib.oauth.codex` — resolved LAZILY inside
  `prepare_request`/`chat` only when a slot carries an `oauth` marker (subscription
  login) or a Codex base URL. Keeps the OAuth package off the default import path.
- `lib.tasks_pkg.cache_tracking` — `latch_extended_ttl` (session-stable cache TTL)
  and `wire_fingerprint.canonical_messages` are called from `prepare_request` /
  `cache.add_cache_breakpoints`. **This is a back-edge into Unit 1** (the LLM
  layer reaching up into the task layer for cache-key stability) — a coupling
  worth noting: it's why `_sse_core` imports from `tasks_pkg`.
- `lib.byo_egress` — SSRF egress guard, called at every ephemeral-slot mint +
  discovery/balance probe (use-time DNS-rebind defense).
- `lib.key_stats` — per-key daily health tracking, fed by `Slot.record_*`.
- `lib.proxy` — `proxies_for` / `register_no_proxy_url` for corp-proxy bypass on
  self-hosted endpoints.
- `lib.token_counter.heuristic.cheap_estimate` — used by
  `body._clamp_completion_to_context_window` (Unit 5 boundary).
- `lib` (root config) — `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`,
  `FALLBACK_MODEL`, `MODEL_PRICING`, `CACHE_EXTENDED_TTL` read live at call time
  (hot-reload-safe — never cached at import).

**Import-cycle discipline:** `llm_dispatch/__init__.py` uses guarded try/except
per-submodule imports (via `build_facade`) so a single submodule failing to
load doesn't take down the whole dispatcher. `model_info`/`llm_errors`/
`llm_sanitize` are leaf-ish (import only stdlib + `lib.log` + each other) so the
`llm/` package and the dispatcher can both import them freely.

---

## 5. Invariants (must not be broken by a refactor)

1. **`_sse_core` emits the anomaly fields byte-for-byte.** `usage['_chunks_received']`,
   `_stream_anomaly`, `_missing_done`, `_missing_finish_reason`, `_empty_stop`,
   `stream_elapsed_ms`, `trace_id`, `_wire_fp` are the contract
   `stream_handler.analyse_stream_result` (Unit 1) keys its retry buckets off.
   Any `_sse_core` change must preserve them.
2. **Sync and async transports MUST stay behaviorally identical** — they share
   `_sse_core`; `test_sse_core_parity` pins this. A fix to one is a fix to both.
3. **`_classify_http_error` always raises, never returns.** It is the single
   HTTP-error taxonomy; the retry loops depend on the exception *type* to decide
   retry-same-key (`RetryableAPIError`) vs rotate-slot (`RateLimitError`) vs
   fail-over (`EndpointUnreachableError`) vs terminal (`ContentFilterError` etc).
4. **`EndpointUnreachableError` must NOT subclass `ConnectionError`** — that's
   deliberate so the `_RETRYABLE` same-key loop does NOT swallow it and it
   escapes to the dispatch fail-over layer (documented in the class).
5. **Provider pin is a HARD scope, conv affinity is a SOFT preference.**
   `provider_pin` (`_pick` returns None rather than cross to another provider)
   isolates BYO traffic; `conv_affinity` silently falls back to score-based on a
   cooled key. Do not conflate them.
6. **Cache-key stability is load-bearing here too.** `cache.add_cache_breakpoints`
   reserves the tail + last-tool breakpoints (the growing-prefix protection);
   `prepare_request` latches the extended-TTL decision per-task. These pair with
   Unit 1's cache-prefix invariants — a naive edit re-bills the whole context.
7. **`context_limits` shrink entries are TTL'd; expand entries are permanent.**
   The expand-starvation deadlock (a wrong shrink caps every prompt below itself
   so expand can never observe tokens above it to correct) is why shrink has a
   7-day TTL + a big-drop strike gate. Don't "fix" it by making expand more
   aggressive — that reintroduces the deadlock.
8. **`_clamp_max_tokens` is total (never raises).** A None/invalid `max_tokens`
   coerces to the unknown-family floor — the killed-recovery path (Unit 1)
   depends on this not crashing `build_body`.
9. **Loop-protection caps are §10 hyperparameters.** `MAX_STREAM_RETRIES=4`,
   the backoff schedule, the `_UNREACHABLE_COOLDOWN`, the sticky-hold budget,
   the context-limit strike gate — all require sign-off to change.

---

## 6. Known debt (grounded)

- **`api.py` bundles 4 concerns** and duplicates the sync/async dispatch loop
  (§3.4) — the one real structural miscut in the unit.
- **`dispatcher.py` (1210) is one class doing slot-build + slot-select** — a
  mixin split is possible but the shared state makes it lower-value.
- **`discovery.py` (939) mixes network I/O with pure heuristics** — the
  heuristic half is a clean split candidate.
- **`body.py` mixes the thinking-param ladder with image validation/downscale**
  (~250 separable lines).
- **The `tasks_pkg.cache_tracking` back-edge** (LLM layer → task layer for
  cache-key stability): architecturally upside-down, but deliberate and
  documented; worth a note if the layering is ever formalized.
- The old `lib/llm_client.py` monolith is fully retired — the `llm/` package
  replaced it cleanly (this is the `llm-package-split` memory's subject; no
  stale single-file references remain in this unit).

---

## 7. Segmentation verdict (this unit)

**Correctly bounded — leave as-is:**
All of `llm/` except `body.py` (the package is a reference-quality split — §2);
`model_info`, `llm_sanitize`, `llm_errors`, `llm_error_format`, `context_limits`;
and in `llm_dispatch/`: `slot`, `config`, `conv_affinity`, `provider_pin`,
`provider_registry`, `ephemeral`, `factory`, `health_local`, `__init__`.

**Miscut — should split (priority order):**

1. **`llm_dispatch/api.py` (1869) → 3 modules.** Highest value in the unit.
   - `dispatch_retry.py` — the `_StreamRetryState` class + `_cool_slot_on_premature_close`
     + `_readjust_thinking_params` + `_adapt_stream_body_for_slot` (the failover
     bookkeeping + slot-swap body re-targeting, ~500 lines: 535–894).
   - Keep the three loops (`dispatch_chat`/`dispatch_stream`/`async_dispatch_stream`)
     in `api.py` but factor their shared body into a helper the way `_sse_core`
     did for the transport shells — the sync/async stream pair is the same
     duplication `_sse_core` already proved is extractable.
   - `dispatch_batch.py` — `dispatch_fastest`/`dispatch_parallel`/`smart_chat`/
     `smart_chat_batch`/`get_dispatch_status` (the fan-out convenience API, 1560+).
   - Re-export facade keeps `from lib.llm_dispatch import dispatch_chat` valid.
   - RISK: hot path + the retry/cooldown invariants (§5) live here → do it behind
     `test_dispatch_stream` + `test_async_dispatch_stream` + `test_chat_flow_dispatch`.

**Big but optional (defer unless touched):**
- `dispatcher.py` (1210) — slot-build vs slot-select mixin split; shared state
  makes it lower-value than #1.
- `discovery.py` (939) — extract `discovery_heuristics.py` (the pure
  endpoint-classification + capability-inference + brand/thinking-format
  detectors); setup-path, not hot.
- `body.py` (765) — extract `llm/_images.py` (`sniff_image_mime` +
  `_validate_image_blocks` + `_downscale_oversized_images`, ~250 lines); the
  thinking-param ladder stays.
- `_sse_core.py` (805) — **do NOT split** despite the size: splitting re-introduces
  the sync/async duplication it was built to remove (§2). Listed here only to
  record the deliberate decision.

**Do NOT split:** `config.py` (data table), `_sse_core.py` (see above),
`model_info.py` (family+limits are one cohesive concern under 600 lines).

---

## 8. Comparison to Unit 1 (the running thesis)

The program's thesis is "segmentation drifts; find where a clean split is
warranted vs where the boundary is already right." Unit 2 refines it:

- **`llm/` proves the clean-split pattern is reproducible** — it's the
  `compaction/` outcome (facade + cohesive cores + a shared core that *removed*
  duplication), NOT the `orchestrator` outcome. When a split is done right, the
  biggest remaining file (`_sse_core`) is big for an *intrinsic* reason and
  splitting it would be net-negative.
- **`llm_dispatch/api.py` is the same miscut species as `manager.py`** — a hot
  file with 3–4 pre-existing internal boundaries never extracted, PLUS a
  sync/async duplication that its sibling layer (`_sse_core`) already showed how
  to fix. That parallel makes it the highest-confidence split recommendation of
  the unit.
- **Size ≠ miscut, confirmed twice:** `_sse_core` (805) and `slot.py`/`config.py`
  are large-but-right; `api.py` (1869) is large-and-wrong. The discriminator is
  always "how many unrelated responsibilities," never the line count.

---

*Next unit: Unit 3 (Tools & execution — `tools/`, `tasks_pkg/handlers/`,
`project_mod/`, `browser/`, `fetch`, `search/`).*
