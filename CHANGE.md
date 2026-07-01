# CHANGE.md


## 2026-06-30 — Per-turn injector audit: fixed B4 (preference detail cache-busts the prefix), B1 (dead delta-cache + leak), B2 (modified-files reminder is a dead feature across tasks)

Audited every per-turn / per-task context injector (9 mechanisms, all competing
for the ≤4 Anthropic cache breakpoints). Three real bugs fixed; Step 3 (system
block stable→volatile reordering) deferred pending readDrop data.

### B4 — preference DETAIL block was on the index-1 `_isMeta` carrier, not the tail: Done
- **Bug.** `_refresh_detail_block` (`lib/tasks_pkg/system_context.py`) targeted the
  first `_isMeta` user message (index 1, the CLAUDE.md carrier, prefix-resident),
  while its own comment claimed it "rides the BP4 5m tail". The detail tier is
  relevance-gated PER TURN, so a cross-turn selection flip rewrote the carrier
  bytes → the whole prefix from `messages[1]` (incl. large CLAUDE.md + tools +
  history) re-billed within the 5m TTL window. `<relevant_memories>`
  (`inject_relevant_memories`) correctly rides the LAST user message — the author
  conflated the two.
- **Fix.** `_refresh_detail_block` now targets the LAST user message (true tail),
  mirroring `inject_relevant_memories`; the byte-stable CORE still rides the
  carrier. In-task endpoint re-entry on the same tail message is strip-then-append
  idempotent; a prior turn's frozen detail on a now-historical message is left
  untouched (the same accepted `<relevant_memories>` tradeoff — stripping a
  prefix-resident block would itself be a prefix mutation). Comment + INFO log
  corrected.
- **Verification (byte-stability, not notify_compaction masking).** NEW
  `test_detail_block_rides_true_tail_not_isMeta_carrier`: two turns, different
  detail selections → asserts `car1 == car2` (carrier byte-identical = readDrop=0
  equivalent) AND detail on the last user message. This FAILED on the old code
  (reproduction: "detail block is on the index-1 _isMeta carrier") and passes
  after the fix. Two old tests that encoded the buggy carrier-detail contract
  rewritten to the corrected contract. **44/44 `test_user_profile` + 192 across
  cache suites green.**

### B1 — `_get_cached_or_compute` / `_last_context_cache` deleted (dead code + lying docstring + unbounded leak): Done
- The "delta cache" called `compute_fn()` UNCONDITIONALLY then hashed the result —
  it could never skip the compute it claimed to skip, and the module-level dict
  was keyed by conv_id with zero eviction (leak). The real FUSE-slow bound is the
  `_prefetch_*` future. Deleted `_last_context_cache`, `_get_cached_or_compute`,
  `_context_hash`, the `hashlib` import; both call sites (project ctx, memory
  hint) now call `compute_fn()` directly. Deleted the dead-API tests
  (`TestDeltaAttachments` in two files) + the lying docstring/table in
  `docs/agentic-development-experience.md`. grep confirms zero residual refs in
  `lib/`. **232 across affected suites green.**

### B2 — modified-files reminder: dead feature across tasks + leak, now a pure message scan: Done
- **Bug.** `_attachment_state` persisted per-conv across tasks but `round_num`
  resets to 0 each task. A stale `last_reminder_round` (high from task 1) made
  `(round_num - last_reminder_round) < 5` permanently true in task 2 → the
  reminder NEVER fired again. Plus the `round_num > 5` caller gate. The state dict
  also leaked (unbounded, no eviction).
- **Fix (not a TTLCache migration — the state is GONE).** Rewrote the trigger as a
  pure message-list scan: fire when (1) a write tool was called, (2) the most
  recent write is ≥ `_MIN_GAP_MESSAGES` (6) back from the tail, (3) no reminder
  was injected SINCE that write (dedup → fixes B3 stacking too). No `round_num`,
  no per-conv dict — the messages ARE the state, so nothing to desync or leak.
  Deleted `_attachment_state` / `_get_state`; dropped the `round_num > 5` gate.
- **Verification.** 4 NEW repro tests FAILED on the old impl (incl. the headline
  `test_reminder_fires_on_message_scan_low_round_num`: write+gap at round_num=3
  fired nothing) and pass after the fix; `test_no_module_level_round_state`
  asserts the leaking dict is gone; dedup + re-arm-after-new-write covered.
  **10/10 `TestAttachments` green; 317 across all affected suites green.**

### Deviations flagged
1. B1 went (b) delete (owner-approved) — not migrate to TTLCache.
2. B2 did NOT migrate `_attachment_state` to TTLCache: the message-scan trigger
   makes per-conv state unnecessary, so deletion is strictly better than bounding
   a dict that no longer needs to exist (kills both the dead-feature AND the leak
   at the root). Flagged because the brief said "migrate to TTLCache".

### Verification summary (actual)
- ruff + AST clean on all touched files.
- 317 passed across `test_new_features` + `test_user_profile` +
  `test_streaming_and_prefetch` + `test_compaction_improvements` +
  `test_cc_alignment` + `test_cache_improvements` + `test_server_message_store`.
- Broad `-k` sweep: 573 passed; the lone failure + error
  (`test_artifacts_api::...injects_katex`, `test_conversations_async::...meta_prefetch`)
  are the known unbootstrapped-SQLite "no such table" harness noise — neither
  imports the changed modules (verified).

### Guardrail / lesson
- A per-turn injector whose payload varies per turn MUST ride the TRUE tail (last
  user message), never the index-1 `_isMeta` carrier; prove it with a carrier
  byte-stability assertion (`car1 == car2`), NOT `detect_cache_break` (which
  `notify_compaction` can mask).
- A trigger that must survive across tasks must NOT key on `round_num` (resets to
  0 each task). Derive it from the message list, which already carries full
  cross-task history — then there's no counter to desync and no state dict to leak.

---

## 2026-06-30 — Debug panel now FAITHFUL: single-source-of-truth wire-form view (cold endpoint + 3 live snapshots) instead of two divergent pre-wire states

The debug panel exists to diagnose context drift, so a panel that "lies" is worse
than an ugly one. Measurement (the staged read-only `debug/debug_panel_fidelity.py`
on an adversarial fixture) proved TWO real divergences: (1) the `/debug-messages`
endpoint ran NO `_inject_system_contexts` → cold system = **0 chars** vs hot
**15593**; (2) the live snapshot was captured BEFORE `sort_tool_results` and
BEFORE `build_body` sanitization → showed an earlier intermediate, not the wire.

### Step 1 — Single source of truth: Done
- NEW `lib/tasks_pkg/wire_messages.py`:
  - `apply_wire_sanitize(messages, *, conv_id='', provider_id='')` — the
    model-agnostic, IO-free tail of `build_body` on an independent copy:
    `sort_tool_results → _strip_non_api_fields → (GATED) _sanitize_messages →
    _fix_orphaned_tool_calls → _merge_consecutive_same_role →
    _fix_empty_user_messages`. DELIBERATELY omits transport-layer steps
    (image disk-resolve, downscale, vision-strip, gemini/claude reasoning
    injection, provider body fields).
  - `build_wire_messages(raw, config, *, mode, …)` — full cold pipeline:
    `_transform_messages → _inject_system_contexts → apply_wire_sanitize`.
    `mode='snapshot'` runs inject with a throwaway task + empty conv_id →
    side-effect-free, never pollutes the live conv-keyed caches; memory/date
    reconstructed as a hypothetical first-round (labelled `approx`).
- **Gateway-sanitize parity (verified fact):** the chat main loop builds its
  body with `provider_id=''` (orchestrator.py:1533) and the pre-built-body
  dispatch branch never re-runs `_sanitize_messages`, so the real outbound
  gateway step is decided by `build_body`'s auto-detect gate (body.py:511:
  `_pid=='sankuai' or (not _pid and 'sankuai' in LLM_BASE_URL)`).
  `_gateway_sanitize_enabled()` REPLICATES that gate verbatim so the preview is
  never MORE aggressive than reality, and cold/hot are symmetric at
  `provider_id=''` (the earlier "live gets real slot provider clean-up"
  asymmetry was FALSIFIED by tracing the dispatch path).

### Step 2 — Take over all emission points: Done
- `routes/conversations.py::debug_messages` → `build_wire_messages(mode='snapshot')`;
  response carries `approx: true`.
- `lib/tasks_pkg/orchestrator.py`: pre-LLM snapshot MOVED to AFTER
  `sort_tool_results` and now emits `apply_wire_sanitize(copy)`; final snapshot
  likewise. `lib/tasks_pkg/tool_dispatch.py`: post-tool snapshot likewise.
  All three produce the wire-form on an INDEPENDENT copy (build_body re-runs the
  same transforms on its own copy at request time — `messages` is untouched).

### Verification — dual-revert negative control with TEETH (actual output)
`tests/test_wire_messages_fidelity.py`, driven by `TOFU_WIRE_REVERT`:
- `TOFU_WIRE_REVERT=inject` (cold skips inject) → **1 failed**
  (`cold system_chars=0` vs hot full system block).
- `TOFU_WIRE_REVERT=sort` (hot emits before sort) → **1 failed**
  (`cold tool_order=[call_aaa,call_zzz]` vs `hot=[call_zzz,call_aaa]`).
- clean (no revert) → **3 passed** (byte-identity + carrier-transforms-fire +
  provider-symmetry). Byte-identity rests on REAL transforms (tool reorder +
  empty-user rewrite + system inject), not an empty no-op run.
- `ruff` + AST clean on all 4 production files; runtime import OK (with the
  flask→quart shim). My net edits are tiny (7 `apply_wire_sanitize` refs + 1
  import in orchestrator/tool_dispatch; +18 in the endpoint) — the large
  `git diff --stat` on orchestrator.py is PRE-EXISTING uncommitted work in that
  file, not this change. The 4 `test_chat_manager_migration` failures are the
  known unbootstrapped-SQLite "no such table" harness noise (no wire/snapshot
  reference), not a regression.

### Phase 2 — frontend amber "reconstructed approximation" chip: Done
The backend emitted `approx:true` but `debug_panel.js` ignored it — a quieter
version of the same "panel lies" disease (a reconstruction shown as if it were a
precise capture). Fixed:
- `static/js/core/debug_panel.js`: `showMessagesInDebug(...)` gained an `approx`
  param; when true (and ONLY then) it renders a `.debug-approx-chip` at the top
  disclosing (a) memory `<relevant_memories>` / date are a hypothetical
  first-round, (b) transport-layer transforms not expanded. Threaded through the
  `/debug-messages` fetch (`!!data.approx`) and the conv-switch cache restore.
  The live SSE snapshot callers (`sse_handlers_lifecycle.js`,
  `sse_handlers_tool.js`) pass NO `approx` → no chip on the real wire form.
- SVG glyph only (`Icon('alertTriangle')`, §3.4 — no emoji); i18n keys
  `debug.approxTitle/approxMemDate/approxTransport` (zh+en); CSS `.debug-approx-*`
  (3 themes). Existing file → no `_BUNDLE_FILES` change; bundle rebuilt.
- **Test `tests/test_frontend_debug_approx_chip.py` (jsdom, 10 checks):** chip
  present + both disclosures + SVG glyph + escaped `<relevant_memories>` on
  approx=true; **NO chip on the live-snapshot shape (negative control)**; chip
  removed when approx toggles back false. **Negative control PROVEN with teeth:**
  reverting the `approx &&` gate (render unconditionally) makes
  `no_chip_on_live_snapshot` FAIL (chip leaks onto the real wire form); intact
  gate PASSES — production file untouched (reverted only a scratch copy).
- `node --check` clean; `test_frontend_sse_dispatch` + `test_frontend_api_isolation`
  = 5 passed (no regression, no raw fetch introduced).

### Provider-gateway note (resolved, not deferred)
Cold/hot are symmetric at `provider_id=''`, so the chip does NOT claim a
provider-gateway difference — only memory/date + transport-layer.

### Guardrail / lesson
A diagnostic panel must show the WIRE form (post sort+sanitize), captured on an
INDEPENDENT copy AFTER the cache-reorder — never an earlier intermediate. When
unifying two divergent paths onto one function, the decisive test is a
dual-revert negative control: each revert must make the byte-identity assertion
FAIL for the RIGHT reason (system-chars→0; tool-order un-sorted), proving the
test has teeth, not just that equality holds today.

---

## 2026-06-30 — Follow-up: consolidate the paper agents onto chat's canonical tool seams (kill the fork I'd just introduced)

The prior fix added `parse_and_repair_tool_args` + `display_query_for` INSIDE
`lib/paper/tools.py` — but those were a 2nd/4th reimplementation of logic chat
already owns. Per the "reuse tofu/tofu-search, don't define paper-specific
tools" directive, moved both to the canonical homes and made the paper module a
thin re-export, so a future fix lands on chat AND paper at once.

### Step 1 — parse+repair promoted to the shared module: Done
- `lib/tool_input_repair.py`: new `parse_and_repair_tool_args(tool_name, args_raw, *, model='')`
  (decode + `validate_then_repair`; never raises → `({}, [])`). Exported in `__all__`.
- This is the one front door for ANY non-chat harness; the chat dispatcher keeps
  its richer inline path (malformed-JSON badge + model retry message).

### Step 2 — label builder promoted to chat's display module: Done
- `lib/tasks_pkg/tool_display.py`: new public `tool_round_label(fn_name, fn_args)`
  over the same `_TOOL_DISPLAY_DISPATCH` chat uses — string/dict-safe, multi-line
  batch form, empty-list guards. Was never vulnerable to the 507-char bug.
- Also fixed a shared cosmetic bug for BOTH surfaces: a single-element
  `queries`/`urls` list now renders as the clean single form (was `1 searches:`).

### Step 3 — paper module is now a re-export, not a fork: Done
- `lib/paper/tools.py`: deleted the local `parse_and_repair_tool_args`,
  `display_query_for`, `_q_text`, `_u_text` (~120 lines). Now:
  `from lib.tool_input_repair import parse_and_repair_tool_args` +
  `from lib.tasks_pkg.tool_display import tool_round_label as display_query_for`
  (both `__all__`, re-exported for the report/QA engines, which import them
  unchanged FROM `lib.paper.tools`).

### What stays paper-specific (correctly NOT shared)
- `_execute_report_tool`: chat's `_handle_web_search_batch`/`_handle_fetch_url`
  are bound to chat's `task` runtime (`append_event`, `_finalize_tool_round`,
  the `tool_result` SSE shape). Paper has its own event model (`tool_start`/
  `tool_done`, 5-tuple). The SHARED layer is the leaf helpers
  (`_web_search_one`/`_fetch_url_one`/`_format_*`/`_vertical_*`) — each harness
  keeps a thin shell. Tool DEFINITIONS were already shared
  (`_REPORT_TOOLS = [SEARCH_TOOL_MULTI, FETCH_URL_TOOL]`).

### Verification
- `ruff` + AST clean on all touched files.
- `tests/test_paper_tool_args_repair.py` (11, +1): labels updated to chat's
  canonical multi-line form; NEW `test_paper_helpers_are_chat_canonical` asserts
  `display_query_for is tool_round_label` and `parse_and_repair_tool_args is
  <canonical>` (consolidation-identity). **Negative control proven:** re-forking
  `display_query_for` fails the identity assertion.
- Suites green: paper(args_repair 11 + report_abort 3 + report_dedup 4 + qa 10)
  + tool_input_repair(27) + cache_schema_stability(23) + malformed_tool_args +
  tool_changes + tool_rounds_render + streaming + orchestration =
  **247 passed** (broad sweep), no regressions.

---

## 2026-06-30 — Reading-Mode report agent: root-cause fix for the "507 searches, mostly punctuation" bug

**Symptom (owner screenshot).** Generating a report for *"Agentic Rubrics as
Contextual Verifiers for SWE Agents"* showed a tool round labelled
**`507 searches:  +504 more`** with NO preview text, and the parser appeared to
be searching parsed content character-by-character (single letters/punctuation).

### Root cause
The model emitted `web_search` with `queries` as a **bare string** (a schema
violation — the schema wants an array of `{query}` objects). Every paper-agent
consumer then iterated that string **character-by-character**:
- `len(queries)` → the string's char count (the "507"),
- the display label's `[q… for q in queries[:3] if isinstance(q, dict)]` filtered
  out every char → empty previews (the `+504 more` with no text),
- the executor's `for qobj in queries[:5]: q = str(qobj)` ran a real web search
  on each of the first characters.

**Why chat mode is immune:** chat routes every tool call through
`lib.tool_input_repair.validate_then_repair` (`lib/tasks_pkg/tool_dispatch.py`),
which applies `bare_string_to_array` and coerces the stray string into a
single-element array BEFORE anything touches it. The paper report/Q&A pipeline
had **reimplemented arg parsing without that seam** — the divergent copy was the
bug (three hand-rolled copies: report display, QA display, executor).

### Step 1 — Single source of truth for paper-agent arg parsing: Done
- `lib/paper/tools.py`: new `parse_and_repair_tool_args(name, args_raw)` —
  JSON-decode + `validate_then_repair` in one place, mirroring the chat
  dispatcher. Never raises (bad JSON / non-dict → `({}, [])`).
- New shared `display_query_for(fn_name, fn_args)` label builder that handles
  BOTH dict entries AND coerced bare-string entries, so a repaired call renders
  as its real query text / `1 search` — never the empty `N searches:` label.

### Step 2 — Route all three consumers through the shared seam: Done
- `lib/paper/report_engine.py` and `lib/paper/qa_engine.py`: parse via
  `parse_and_repair_tool_args`, label via the shared `display_query_for`
  (QA's duplicated local `_display_query_for` deleted).
- `_execute_report_tool` now parses+repairs up front, and its per-item loops are
  hardened (non-list `queries`/`urls` → single entry; non-dict/non-string items
  skipped) so a bare string can NEVER be iterated per-character even if repair is
  unavailable (defense-in-depth).

### Step 3 — Tool description hardening (tool design): Done
- `lib/tools/search.py`: the `queries`/`urls` array descriptions now state each
  element MUST be an object `{"query": …}` / `{"url": …}`, never a bare string or
  a concatenation — for a single search use the top-level `query`/`url` field.
  (Descriptive only — no `type`/`required` change, so prompt-cache stability and
  the repair schema index are unaffected.)

### Verification
- `ruff` + AST clean on all touched files.
- New `tests/test_paper_tool_args_repair.py` (10): bare-string `queries` → 1
  search (not 507); label is the real text, not the empty multi-search label;
  mixed string+dict entries preview correctly; executor issues exactly ONE
  search for the bug payload (network stubbed) and still fans out a genuine
  batch; parse helper never raises on garbage.
- **Negative control proven:** the OLD code path (`for qobj in queries[:5]: …
  str(qobj)`) reports `len(queries)=110` and searches single chars
  `['a', ',', 'b', '.', 'c']` — exactly the screenshot symptom; the fix reduces
  it to 1 search.
- Regression suites green: `test_paper_tool_args_repair`(10) +
  `test_paper_report_abort`(3) + `test_paper_report_dedup`(4) +
  `test_paper_qa_agentic`(10) + `test_tool_input_repair`(27) +
  `test_cache_schema_stability`(23) + `test_search_marginalia_deepen`(19) =
  **96 passed**.

### Guardrail / lesson
The paper report/Q&A agents are a SECOND tool-calling harness alongside chat.
Any robustness layer chat applies at the dispatch seam (arg repair, name-alias
resolution, hallucination rejection) must be reachable by the paper agents too —
reimplementing arg parsing inline silently forks the behaviour and reintroduces
exactly the class of bug `validate_then_repair` exists to kill. When a model
emits a string where an array is declared, NEVER iterate it: coerce via the
shared repair, and as a backstop treat any non-list array-slot value as a single
element.
