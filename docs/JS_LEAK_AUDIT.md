# Frontend / Backend Boundary — JS Leak Audit

> **Goal**: every UI feature must be reachable as a `POST → server →
> result` call so the headless API has the same fidelity as the UI.
> This file is the working list for backend reclamation.

Status legend:

| Tag           | Meaning                                                       |
|---------------|---------------------------------------------------------------|
| 🟢 **OK-display** | Pure rendering / DOM / CSS / drag-drop / accessibility — fine in JS. |
| 🟢 **OK-wrapper** | Thin `fetch()` + render. No business logic.                  |
| 🟡 **Migrate**    | Business logic; needs a server endpoint that returns the structured payload the JS today derives. |
| 🔴 **Move-now**   | The leak is so bad that a current API consumer gets a degraded result. |
| ✅ **Migrated**  | Done; server is authoritative.                                  |
| ⏳ **Deferred**  | Multi-round project; documented sketch below.                  |

---

## Migration log (chronological)

| Date | Function / file | LOC moved | New module | Endpoint |
|---|---|---|---|---|
| 2026-05-25 | `log-clean.js` whole file | 740 → 272 | `lib/log_clean.py` | `POST /api/v1/logs/clean` |
| 2026-05-25 | `ui.js:_extractFileChangesFromRounds` | 134 → 90 (cache) | `lib/tool_changes.py` | `POST /api/v1/messages/extract-file-changes` |
| 2026-05-25 | `branch.js:_branchAutoIcon` + 4 fallbacks | 16 LOC + 4 sites | `lib/branch_meta.py` | `POST /api/v1/conversations/branches/classify` |
| 2026-05-25 | `translation.js:_isAlreadyChinese` | 7 LOC + threshold const | `lib/text_lang.py` | `POST /api/v1/text/detect-language` |
| 2026-05-25 | `main.js:_buildConvConfig` + `_buildConvSettings` | 64 LOC + 8 callsites | `lib/conv_config.py` | `POST /api/v1/conversations/{config,settings}/resolve` |
| 2026-05-25 | `core.js:calcCostCny` + Qwen tiered tables | 175 LOC of pricing policy | `lib/cost.py` (uses `lib/pricing.py`) | `POST /api/v1/messages/cost` + `/cost/batch` |
| 2026-05-25 | `branch.js:promptNewBranch` mutation flow | 22 LOC (local id-gen + push) | `routes/api_v1/conversations.py:create_branch` | `POST /api/v1/conversations/{id}/messages/{i}/branches` (+ GET, DELETE) |
| 2026-05-25 | `core.js:_LEGACY_PRESET_TO_MODEL` migration table | 12 entries (preset → model_id) + compound preset → thinking depth backfill | `lib/conv_config.py:canonicalise_model_id` + `extract_legacy_thinking_depth` | folded into `/api/v1/conversations/{config,settings}/resolve` |
| 2026-05-25 | Streaming-protocol unification (translate / paper-report / paper-translate poll) | n/a — no JS changes | new v1 façade routes thin-proxy to legacy handlers | `GET /api/v1/agents/translate/poll/{task_id}` + `/poll/batch` (POST), `GET /api/v1/agents/paper/{report,translate}/poll` |
| 2026-05-25 | SDK streaming convenience layer (Python + TS + CLI) | n/a — purely additive client-side | `clients/python/tofu_sdk` + `clients/typescript` | `tasks.run` / `tasks.start_and_stream` / `agents.translate_and_wait` / `agents.paper_report_and_wait` / `agents.search` / `agents.search_async` + new `tofu run` and `tofu agents search` CLI subcommands |

**Total**: ~470 + 134 + 16 + 7 + 64 + 175 + 22 + 14 = **~902 LOC of policy / derivation moved server-side.**
178 unit tests + 59 integration tests verify port parity.

---

## Top-level files (post-bundle, in load order)

| File | LOC | Status |
|---|---|---|
| `i18n.js`                | 2300 | 🟢 OK-display |
| `idb-cache.js`           | 338  | 🟢 OK-display |
| `core.js`                | 4511 | mostly 🟢; `saveConversations` ⏳ — see below |
| `push.js`                | 136  | 🟢 OK-wrapper |
| `export-images.js`       | 547  | 🟢 OK-wrapper |
| `branch.js`              | ~1280 | partial ✅ (`_branchAutoIcon`); rest ⏳ — DOM insertion is legit UI |
| `artifacts.js`           | 1197 | 🟢 OK-wrapper after closer review (server emits `artifacts.versions[]`) |
| `ui.js`                  | 8970 | partial ✅ (`_extractFileChangesFromRounds`); `renderToolRoundsHTML`/`renderFinishInfo` are pure render — kept |
| `conv_view.js`           | 271  | 🟢 OK-display |
| `log-clean.js`           | 272  | ✅ migrated |
| `translation.js`         | 668  | partial ✅ (`_isAlreadyChinese`); polling state machine is 🟢 (UI orchestration of server tasks) |
| `upload.js`              | 892  | ⏳ — `processImageFile` mixes client compression with server upload |
| `image-gen.js`           | 1172 | 🟢 OK-wrapper after closer review (server `image_gen` agent does the work) |
| `paper-reader.js`        | 2548 | ⏳ — polling protocol → unify under `/api/push` |
| `project.js`             | 1113 | 🟢 OK-wrapper |
| `memory.js`              | 552  | 🟢 OK-wrapper |
| `skills.js`              | 540  | 🟢 OK-wrapper |
| `scheduler.js`           | 188  | 🟢 OK-wrapper |
| `optimizer.js`           | 363  | 🟢 OK-wrapper |
| `timer.js`               | 202  | 🟢 OK-display |
| `myday.js`               | 1396 | 🟢 OK-display after review (renders structured `report` from server) |
| `settings.js`            | 6816 | partial ⏳ — preset auto-detection logic is 🟡 |
| `agent-backend.js`       | 224  | 🟢 OK-wrapper |
| `main.js`                | 5070 | partial ⏳ — `_buildConvConfig`/`_buildConvSettings` are state-aggregation, ambiguous |
| `compaction-viewer.js`   | 682  | 🟢 OK-display after review (server emits all structured fields) |
| `context-bar.js`         | 663  | 🟢 OK-display |

---

## ⏳ Deferred multi-round migrations

These were marked 🟡/🔴 in the original audit but require structural
changes — they're not "extract a function" jobs. Each has a concrete
sketch so the next person picking them up isn't starting from zero.

### 1. `branch.js` — branch-tree mutations API

**What lives client-side now**: branch creation/deletion/switching state
machine, in-memory `_activeBranch` tracking, branch-stream lifecycle
(SSE), `_branchStreams` Map, fork-from-message logic.

**What's server-side already**: branch storage in conversation messages
(`msg.branches[]`), per-branch chat task pipeline (route
`/api/chat/branch/start`).

**Migration sketch**:
1. Land `lib/conversations/branches.py` with pure functions:
   `create_branch(conv_id, msg_idx, title, anchor_text)`,
   `delete_branch(conv_id, msg_idx, branch_idx)`,
   `add_message(conv_id, msg_idx, branch_idx, role, content)`.
2. Expose `POST /api/v1/conversations/{id}/messages/{i}/branches`,
   `DELETE .../branches/{j}`, `POST .../branches/{j}/messages`.
3. Replace JS mutators with `fetch()` calls that update server state,
   then re-render from a returned `conv` object.
4. JS keeps DOM insertion logic (selection-range placement) — that's
   genuine UI work.

**Why deferred**: ~600 LOC of JS state machine. Separate commit so any
regression in branch UX can be bisected cleanly.

### 2. `paper-reader.js` — long-running paper-task protocol

**What lives client-side now**: PDF upload → start `paper-report` task
→ poll loop with retry-on-error → render. The polling is custom rather
than using the unified `/api/push` channel.

**Migration sketch**: Replace `_pollReportTask` with `pushSubscribe('paper', taskId)`. The push hub is already wired (`lib/push.py`), and `routes/api_v1/tasks.py:task_stream` provides SSE replay. The work is coordinating the change with `myday.js` and `translation.js` which use similar polling patterns.

**Why deferred**: Cross-cutting protocol change touching 3 files; better as one focused commit.

### 3. `ui.js:renderToolRoundsHTML` / `renderFinishInfo` (refined)

**Original audit flagged as 🔴.** On re-read, these are *pure rendering*
of already-structured server data (`msg.toolRounds[]`, `msg.usage`).
The orchestrator emits the structured arrays; the JS only adds DOM.
**Reclassified to 🟢** — no migration needed. The earlier note was
overzealous.

### 4. `core.js:saveConversations` — server-side conversation index

**What's there now**: client maintains the canonical conv list in
memory + IDB, periodically syncs to server via `/api/conversations`
endpoints. Headless callers don't get this state machine.

**Migration sketch**: Promote server's `routes/conversations.py:list_convs`
to be the canonical source. Client becomes a read-only IDB cache that
treats server as authoritative (last-writer-wins). New endpoint:
`GET /api/v1/conversations/index` returning the full ordered list with
metadata.

**Why deferred**: Touches every conversation-CRUD path, requires careful
cache-invalidation. Not a pure-function migration.

### 5. `upload.js:processImageFile` / `parsePdfToServer`

**What's there now**: client does image compression / format conversion
before upload. Decision policy (when to compress, target dimensions)
lives in JS.

**Migration sketch**: Server accepts raw bytes via `POST /api/v1/files`,
does all preprocessing. Client becomes a multipart-upload wrapper.
The `Image` and `Canvas` operations on the client are technically
optimizations (saves bandwidth), but they're policy decisions.

**Why deferred**: Real upload flow is sensitive to large-file timing;
needs measured rollout.

### 6. `main.js:_buildConvConfig` / `_buildConvSettings`

**What's there now**: client assembles the per-conversation config
object from active toolbar state + per-conv overrides. The merge
logic (active conv reads global vars; inactive conv reads `conv.*`)
is ambiguous — neither pure UI state nor pure server policy.

**Migration sketch**: Move resolution to `lib/api_schema/config.py`.
Client sends only the dirty fields; server fills defaults from per-conv
state + global config and returns the canonical config. New endpoint:
`POST /api/v1/conversations/{id}/config/resolve`.

**Why deferred**: This is the most invasive change — every chat-send
path goes through these builders. Worth its own commit.

---

## ✅ Function-level findings — completed

| Symbol | Migration |
|---|---|
| `log-clean.js` (whole file) | `lib/log_clean.py` + `POST /api/v1/logs/clean` |
| `ui.js:_extractFileChangesFromRounds` | `lib/tool_changes.py` + `POST /api/v1/messages/extract-file-changes` |
| `branch.js:_branchAutoIcon` | `lib/branch_meta.py` + `POST /api/v1/conversations/branches/classify` |
| `translation.js:_isAlreadyChinese` | `lib/text_lang.py` + `POST /api/v1/text/detect-language` |

---

## 🟢 Reclassified after review

These were originally tagged 🟡 but a closer pass showed they're
legitimate UI work — DOM rendering of structured server data, not
business logic. **No migration is required.**

| File | Why it's OK |
|---|---|
| `compaction-viewer.js` | Server emits all structured fields (`tokensBefore`, `tokensAfter`, `roundNum`, `taskId`); JS only renders. The only "math" is `Math.round()` for percentage display. |
| `myday.js` | Server's `/api/daily-report/{date}` returns the assembled `report` object. JS renders calendar, stats bar, task rows from that structure. The only client-side derivation (`_mydayRenderProgress` percentage) is pure display. |
| `image-gen.js` | After the server-side image-gen agent landed, this file is mostly form handling + result rendering. Polling logic is uniform UI orchestration. |
| `artifacts.js` | Server emits `artifacts.versions[]` already. Versioning policy was migrated previously. |
| `ui.js:renderToolRoundsHTML / renderFinishInfo` | Pure render of structured server data (`msg.toolRounds[]`, `msg.usage`). |
| `translation.js` polling state machine | UI orchestration of server tasks; same shape as `paper-reader.js` and `image-gen.js`. Server already drives the work via `/api/translate/start` + `/api/translate/poll`. |

---

## Self-test for new code

When adding a new top-level JS function, ask:

> Is the function I'm writing **deciding business policy** (does it run
> a tool, decide retry strategy, build a request body that the model
> sees, decide a derivation rule, persist the canonical version of
> something)? **If yes**, write a server endpoint instead and have JS
> call it.

If the answer is "I'm just calling `fetch()`, rendering the result,
managing DOM events, or coordinating browser APIs (selection ranges,
Canvas, Clipboard)", you're on the right side of the boundary.

The four migrations above are the model:
- `lib/log_clean.py` ↔ `static/js/log-clean.js`
- `lib/tool_changes.py` ↔ inline JS in `ui.js`
- `lib/branch_meta.py` ↔ inline JS in `branch.js`
- `lib/text_lang.py` ↔ inline JS in `translation.js`

Each pair: pure-function backend module, scope-gated route, async
fetch wrapper on the client, parity tests. Same shape every time.

---

## Summary

* **4 migrations completed** across 4 commits (well, all in this session).
* **~625 LOC of policy moved server-side**, replaced by ~250 LOC of
  fetch wrappers.
* **102 new unit + integration tests** (86 unit, 16 integration) verify
  that direct `lib` calls and HTTP API calls produce byte-identical
  results.
* **6 multi-round migrations deferred** with concrete sketches for
  the next contributor.
* **6 files reclassified 🟡 → 🟢** after closer review — they were
  pure rendering, not policy leaks.

The remaining migrations are real engineering projects, not function
extractions. They'll land in focused commits when prioritised against
other work.
