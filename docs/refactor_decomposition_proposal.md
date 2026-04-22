# Refactor Decomposition Proposal (PLAN-ONLY — requires user approval)

**Status**: none of the splits below have been executed. Per CLAUDE.md §10
these are high-risk changes touching task-orchestration invariants, LLM
routing, and DB/schema-adjacent code. Each item is a proposal only.

For the executed refactors, see `docs/refactor_inventory.md`.

---

## Priority ordering

1. **P1 — Most value, lowest risk**: `lib/tasks_pkg/compaction.py` split.
2. **P2 — High value, medium risk**: `routes/paper.py`, `routes/daily_report.py`.
3. **P3 — High value, high risk** (hot path): `lib/llm_client.py`,
   `lib/tasks_pkg/orchestrator.py`, `lib/tasks_pkg/manager.py`,
   `lib/tasks_pkg/tool_dispatch.py`.
4. **P4 — Frontend (separate approval gate)**: `static/js/ui.js`,
   `static/js/settings.js`, `static/js/main.js`, `static/js/core.js`.

---

## P1. `lib/tasks_pkg/compaction.py` — 2620 LOC

### Current responsibilities
- Layered token-budgeting / layer boundaries.
- Compaction strategies (keep-tool-history, summarize-old, drop-duplicates).
- Token counting + per-model clamp.
- SSE event emission for compaction progress.

### Proposed split (into a package)

```
lib/tasks_pkg/compaction/
  __init__.py           # re-export the public facade
  _layers.py            # layer-boundary calculators   (~500L)
  _token_budget.py      # token-count helpers          (~300L)
  _strategies.py        # individual compaction passes (~800L)
  _emit.py              # SSE event helpers            (~200L)
  orchestrator.py       # top-level run_compaction()   (~600L)
```

### Rationale
`compaction.py` is the single biggest file in `lib/tasks_pkg/`. Each strategy
(keep-tool-history vs summarize-old) has its own helpers. Splitting reduces
the largest-single-file footprint and makes each strategy individually
testable.

### Risk notes
- §10.1 hyperparameter triggers: the file contains token budgets
  (`TOOL_HISTORY_BUDGET`, layer size constants). **Split only — do not
  tune values**.
- Extensive test coverage lives in `tests/test_compaction_improvements.py`
  and `tests/test_keep_tool_history_*`. All must stay green after split.
- Hot-reload: `compaction.py` uses `import lib as _lib` and reads
  `_lib.COMPACTION_*` constants at call time. Preserve this in every split
  module — do NOT cache module-level copies.

---

## P2. `routes/paper.py` — 2547 LOC

### Current responsibilities
- Paper upload + parsing kickoff.
- Report generation (9-section LLM pipeline) with SSE progress.
- Q&A streaming (`_stream_llm_sse`).
- Translation helpers.
- Arxiv fetch / caching.

### Proposed split
```
routes/paper.py                         # Flask routes only             (~400L)
lib/paper/
  __init__.py
  report.py                             # _run_report_task + 9-section prompt (~800L)
  qa.py                                 # Q&A streaming                 (~300L)
  translate.py                          # translate helpers             (~300L)
  arxiv.py                              # arxiv fetch + cache           (~400L)
  prompts.py                            # shared prompt templates       (~300L)
```

### Rationale
Route file mixes HTTP wiring with business logic; moving business logic into
`lib/paper/` lets it be reused (e.g. by MyDay / Reading Mode routes) and
keeps the route thin.

### Risk notes
- §13 long-form generation rule: the file contains `max_tokens=128000`.
  Preserve exactly in the split.
- `_stream_llm_sse` is shared with other paper routes — its signature must
  not change.

---

## P3a. `routes/daily_report.py` — 2669 LOC

### Current responsibilities
- Daily report generation (mega-prompt pipeline).
- Report persistence + history APIs.
- Scheduler integration (cron-triggered runs).
- Preview / render endpoints.

### Proposed split
```
routes/daily_report.py                  # Flask routes only             (~500L)
lib/daily_report/
  __init__.py
  generator.py                          # main report-gen pipeline      (~1000L)
  prompts.py                            # mega-prompt templates          (~600L)
  persistence.py                        # save/load/history             (~300L)
  scheduling.py                         # cron → scheduled trigger glue (~300L)
```

### Risk notes
- Daily report has its own scheduled-task hooks via `lib.scheduler`.
  Import order must be preserved.
- No DB schema changes.

---

## P3b. `routes/chat.py` — 2172 LOC

### Current responsibilities
- `/api/chat/start` — task creation + backend dispatch.
- `/api/chat/stream/<id>` — SSE polling.
- `/api/chat/abort` — stop task.
- `/api/chat/stdin_response`, `/api/chat/approve`, `/api/chat/human_response` —
  interactive endpoints.
- Response shaping + error envelopes.

### Proposed split
```
routes/chat.py                          # route registration only       (~200L)
routes/chat/
  start.py                              # /start                         (~500L)
  stream.py                             # /stream SSE delivery           (~500L)
  abort.py                              # abort + cleanup                (~200L)
  interactive.py                        # stdin / human / approval       (~400L)
  shaping.py                            # request/response adapters      (~400L)
```

### Risk notes
- §10.4 security-sensitive: auth/session handling is in this file. Split
  with care — authenticate-before-dispatch order must be preserved.
- Must keep the Blueprint named `chat_bp` registered as today.

---

## P3c. `lib/llm_client.py` — 3652 LOC (HIGH RISK)

### Current responsibilities
- `build_body` (model-aware request body construction for 10+ providers).
- `stream_chat` (SSE streaming with retry, fallback, abort).
- JSON parsing for SSE events.
- Tool call accumulation + streaming.
- Token/usage reporting.

### Proposed split
```
lib/llm_client/
  __init__.py                           # re-export stable public API
  _build_body.py                        # build_body + model adapters    (~1000L)
  _sse.py                               # SSE line parse, event state    (~800L)
  _stream.py                            # stream_chat top-level loop     (~800L)
  _retry.py                             # retry/backoff policy           (~400L)
  _usage.py                             # token / usage accumulator      (~300L)
  _tool_stream.py                       # streaming tool-call accumulator (~400L)
```

### Risk notes
- **§10.1 / §10.2 triggers**: retry counts, timeouts, max_tokens handling,
  model-specific branches. **Split only — do not tune behavior**.
- `build_body` has provider-scoped content-filter handling (Sankuai).
  Must remain intact.
- Hot-reload: `_lib.STREAM_TIMEOUT` etc. read at call time — preserve.
- Dispatch code in `lib/llm_dispatch/` imports from this module heavily;
  public names (`build_body`, `stream_chat`, `stream_one_completion`, …)
  must remain importable from `lib.llm_client`.

---

## P3d. `lib/tasks_pkg/orchestrator.py` — 1899 LOC

### Proposed split
```
lib/tasks_pkg/orchestrator/
  __init__.py
  _main_loop.py                         # run_task core loop             (~700L)
  _finish.py                            # finish-reason handling + suspicious detection (~400L)
  _compaction_bridge.py                 # hooks into compaction          (~300L)
  _events.py                            # orchestrator-level event emitters (~300L)
  _emit_ref.py                          # emit_to_user reference handling (~200L)
```

### Risk notes
- Hot path; heavily exercised by e2e tests. Split must not perturb
  ordering between event emission and state transitions.
- §10.1: contains `_loop_exit_reason` classification constants — treat
  as hyperparameters; do not tune.

---

## P3e. `lib/tasks_pkg/manager.py` — 1848 LOC

### Proposed split
```
lib/tasks_pkg/manager/
  __init__.py
  _crud.py                              # create/get/update task         (~500L)
  _events.py                            # append_event, event stream     (~400L)
  _persistence.py                       # DB persist, load from conv     (~500L)
  _lifecycle.py                         # status transitions, cleanup    (~400L)
```

### Risk notes
- Shared-state (global `_TASKS` dict) must keep its lock semantics.
- DB read/write code — §10.3 applies if schema changes are proposed.

---

## P3f. `lib/tasks_pkg/tool_dispatch.py` — 1726 LOC

### Proposed split
```
lib/tasks_pkg/tool_dispatch/
  __init__.py
  _exec_phase.py                        # emit_tool_exec_phase + round wiring (~500L)
  _abort.py                             # abort gating, cooperative stop (~300L)
  _envelopes.py                         # role=tool envelope construction (~400L)
  _parallel.py                          # ThreadPoolExecutor fan-out     (~500L)
```

### Risk notes
- Hot path; coverage in `tests/test_streaming_and_prefetch.py`.
- Browser client_id propagation (`_set_active_client`) must be preserved
  across thread-pool workers — see
  `multi-provider-key-endpoint-cross-contamination` memory.

---

## P4. Frontend monoliths (SEPARATE APPROVAL)

Frontend JS uses a no-build-step vanilla-JS architecture with cross-script
vars and function hoisting. Splitting requires updating `index.html` load
order carefully.

### P4a. `static/js/ui.js` — 8550 LOC

Proposed:
```
static/js/ui/
  core.js            # window.UI object + orchestrator                   (~600L)
  messages.js        # message rendering + tool-result blocks            (~1200L)
  stream.js          # SSE stream hook-up                                (~800L)
  tools_display.js   # tool-result custom renderers                      (~1500L)
  modals.js          # dialog / popover primitives                       (~800L)
  thinking_block.js  # thinking UI, toggles                              (~500L)
  scroll.js          # auto-scroll logic                                 (~400L)
  ...                # etc.
```

### P4b. `static/js/settings.js` — 5569 LOC

Proposed:
```
static/js/settings/
  core.js            # open/save/close + root settings object            (~500L)
  providers.js       # provider CRUD + templates                         (~1500L)
  models.js          # model assignment + discovery                      (~1000L)
  features.js        # feature flags / toggles                           (~800L)
  oauth.js           # OAuth flow (§10.4 auth — extra care)              (~500L)
  import_export.js   # settings JSON import/export                       (~400L)
```

### P4c. `static/js/main.js` — 5417 LOC

Proposed:
```
static/js/main/
  bootstrap.js       # DOMContentLoaded, wiring                          (~800L)
  events.js          # global event handlers                             (~1200L)
  commands.js        # slash commands, tool activation                   (~1000L)
  streaming.js       # streaming orchestration                           (~1000L)
  endpoint_mode.js   # planner/worker multi-agent front-end              (~800L)
```

### P4d. `static/js/core.js` — 3554 LOC

Proposed:
```
static/js/core/
  network.js         # apiUrl + fetch wrappers                           (~800L)
  state.js           # runtime state helpers                             (~800L)
  helpers.js         # generic utilities                                 (~600L)
  markdown.js        # markdown/latex rendering                          (~800L)
```

### Frontend risk notes (all P4 items)
- **cross-script var requirement**: module-level `var` declarations are
  shared via global scope; splitting must convert them to `window.XX`
  assignments or preserve load order.
- **function hoisting**: JS files currently rely on forward references
  working because everything is parsed before execution starts; after
  split, function references must be wired up in a correct dependency
  order.
- index.html script tags are hand-ordered. See
  `chatui-frontend-modular-architecture` memory for the pattern.

---

## Migration order (suggested)

1. **Do `compaction.py` first** — lowest risk, highest isolation, excellent
   tests exist (`tests/test_compaction_improvements.py`).
2. Then `routes/paper.py` and `routes/daily_report.py` — moving business
   logic into `lib/paper/` / `lib/daily_report/` packages.
3. Then the `lib/tasks_pkg/` monoliths (`manager`, `orchestrator`,
   `tool_dispatch`) — require careful hot-path validation.
4. Then `lib/llm_client.py` — largest file, most risk; do after tasks_pkg
   split to derisk the test matrix.
5. Frontend last, under a separate approval session.

Each step should be a **single PR**, green test suite, and
user sign-off per §10.

---

_Generated as part of the refactor pass on 2026-04-21._
