# Changelog

All notable changes to tofu-open are documented in this file.

## [Unreleased]

### Added
- **Auto-escaping `safeHtml` tagged template (`static/js/core/safe_html.js`).**
  The chat-render path builds HTML by string concatenation, hand-wrapping
  every user/model interpolation in `escapeHtml()` — correct but fragile
  (one forgotten wrap is an XSS hole). `safeHtml\`...\`` escapes EVERY
  interpolation by default and returns a plain string (drop-in for the
  existing `outerHTML =` / `insertAdjacentHTML` sinks), with an explicit
  `raw(x)` opt-out for trusted HTML (DOMPurify-sanitized markdown, hardcoded
  SVG). Arrays are auto-joined and nested `safeHtml` results compose without
  double-escaping. No runtime framework, no build step. Wired into
  `lib/js_bundler.py` `_BUNDLE_FILES` (after `escape_html.js`) and the
  dev-mode `<script>` tags. Piloted on `_streamingBubbleHTML`
  (`static/js/ui/streaming_render.js`) with verified byte-identical output.
  A lint rule in `tests/test_frontend_safe_html.py` blocks new bare
  template-string HTML sinks in adopted render files so future edits can't
  bypass the auto-escaping (opt-out marker: `safe-html-lint-ok`). Adopted in
  `ui/streaming_render.js` (`_streamingBubbleHTML`) and `ui/chat_render.js`
  (`renderMessage` final assembly + welcome/loading screens), each verified
  byte-identical to the prior output.

### Fixed
- **Weak image-caption escaping in `renderMessage`.** The chat image tile's
  `title` tooltip escaped only double-quotes (`caption.replace(/"/g,'&quot;')`),
  leaving `<`/`>`/`&` unescaped. Now uses the full `escapeHtml()`
  (`static/js/ui/chat_render.js`). Surfaced while adopting `safeHtml`.
- **Frontend type-check harness (`tsc --checkJs`, no build step).** New root
  `tsconfig.json` + `static/js/globals.d.ts` run TypeScript in check-only
  mode over the vanilla-JS frontend (`static/js/*.js`), catching cross-file
  global misuse — typos, stale renames, dead `typeof` guards — that the
  shared-`window`-scope design otherwise fails silently at runtime. Wired as
  `make typecheck` and enforced by a monotonically-decreasing error-budget
  ratchet in `tests/test_frontend_typecheck.py` (mirrors the
  `test_frontend_api_isolation.py` model). Does NOT introduce a build step:
  `tsc` only reads the existing `.js` files; the runtime bundle is still
  produced by `lib/js_bundler.py`.

### Added (groundwork — inert, not yet wired)
- **SQLAlchemy Core table-definition layer (`lib/database/_core_schema.py`).**
  Lets NEW tables be defined ONCE as Core `Table` objects and compiled to
  correct DDL + DML for BOTH backends (PG `JSONB`/`IDENTITY` ↔ SQLite
  `JSON`/autoinc, paramstyle `%(x)s` ↔ `?`, dialect-correct
  `INSERT … ON CONFLICT … DO UPDATE` upserts) — the single source of truth
  that retires the hand-maintained twin-DDL + regex `_sql_translate` path
  *for those tables*. **Compile-only**: opens no SQLAlchemy Engine; execution
  stays on the existing `get_db()` connection. **Not yet wired into
  `init_db()` and runs no DDL** — registering a live table is a §10.3 schema
  change pending sign-off. Proven by `tests/test_core_schema_groundwork.py`
  (dual-dialect DDL/DML + module-inertness). Adds `sqlalchemy>=2.0`.

### Changed
- **Unified the LLM SSE streaming core.** `lib/llm/stream.py` (sync,
  `requests`) and `lib/llm/astream.py` (async, `httpx`) each carried a
  ~480-line copy of the identical SSE chunk-parsing loop — error
  classification, MiniMax `<think>` demux, tool-call accumulation,
  premature-close / empty-stop anomaly diagnostics, and `usage` metadata
  injection — so every fix had to land twice and the copies drifted. That
  logic now lives once in the new transport-agnostic `lib/llm/_sse_core.py`
  (`SSEAccumulator` + `prepare_request` + `classify_status_error`); the two
  modules are now thin transport shells (~150 lines each) that keep only
  their retry/backoff wrapper and transport-native open/iterate/exception
  handling. Pure code-motion: no retry cap, timeout, backoff, or cache
  constant changed; the `usage` anomaly fields that
  `lib/tasks_pkg/stream_handler.py` keys its retry buckets off
  (`_chunks_received`, `_stream_anomaly`, `_missing_done`,
  `_missing_finish_reason`, `_empty_stop`, …) are emitted byte-for-byte as
  before. Locked by `tests/test_sse_core_parity.py` (sync-vs-async parity +
  characterization of every anomaly fingerprint). Net −432 lines.
  - Minor: the async path's SSE-error handling now produces the same
    richer `<empty error body> …` diagnostic string the sync path always
    had for empty SSE error objects (error *message* text only — no control-
    flow or `usage`-field change).

### Fixed
- **Chat didn't re-render on language switch / debug-mode toggle.**
  `i18n.js::_onLanguageChange` and `settings/save_export.js::saveSettings`
  called `renderMessages()` behind a `typeof … === 'function'` guard, but
  that function never existed (the real whole-chat repaint is
  `renderChat(conv)`), so the guard silently swallowed the no-op. Now both
  call `renderChat(getActiveConv())`. Caught by the new `tsc --checkJs` harness.
- **Duplicate `common.close` i18n key** in `static/js/i18n.js` removed
  (TS1117) — a misplaced copy in the skills block shadowed by the canonical
  entry in the Common section.

## [0.10.0] - 2026-05-09

### Added
- **Daily Optimizer (self-tuning loop).** New `lib/optimizer/` package mines the
  prior day's logs, audit events, and daily reports, asks an LLM for
  optimisation proposals, and either auto-applies whitelisted low-risk
  actions (currently `block_search_domain`, with TTL-based auto-revert) or
  stages everything else as `pending_review` for human approval. Runs nightly
  at 03:30 via the scheduler (`Daily Optimizer` task, auto-registered on
  boot). REST API in `routes/optimizer.py`; review UI in `static/js/optimizer.js`.
  Gated by `OPTIMIZER_ENABLED` setting.
- **Skills Store (curated catalogue + drag-and-drop installer).** Settings →
  Skills tab now has an App-Store-style layout (search + Catalogue/Installed
  scope tabs + category pills) backed by `lib/memory/catalog.py`. One-click
  install downloads a `.zip` over HTTPS (≤ 50 MB) and unpacks it via
  `lib/memory/installer.py`. Anthropic / OpenClaw / AgentSkills `.zip`
  packages can also be drag-dropped onto the tab; bundled `install.sh`
  scripts are surfaced as hints, never auto-executed.
- **Pluggable token counter.** New `lib/token_counter/` package routes token
  counting through provider-specific backends (Anthropic / Gemini / DeepSeek
  / HuggingFace / tiktoken / heuristic) with a usage cache, replacing the
  scattered ad-hoc estimators.
- **File-history store.** New `lib/file_history/` records per-file edit
  history so write tools and the diff viewer can show a coherent timeline
  of changes across a session.
- **Memory prefetch.** `lib/memory/prefetch.py` surfaces likely-relevant
  memories at turn start via the `<relevant_memories>` block, so the model
  doesn't have to call `search_memories` as a generic discovery step.
- **Compaction archive viewer.** New `routes/conversations_compaction.py`
  + `static/js/compaction-viewer.js` let you inspect the archived layers
  produced by 3-layer context compaction.
- **Conversation full-text search endpoints** moved into a dedicated
  `routes/conversations_search.py` Blueprint (extracted from
  `routes/conversations.py`).
- **Provider templates.** Added Meituan and Tencent provider one-click
  templates in Settings → Providers.

### Improved
- **`routes/chat.py` decomposition.** Extracted `chat_human_io.py`,
  `chat_queue.py`, and `chat_tool_state.py` so the chat blueprint is
  smaller and individual concerns (stdin/human-guidance responses,
  server-side message queue, tool-toggle PATCH) live in their own
  modules.
- **PDF parsing.** Added `lib/pdf_parser/docling.py` as an additional
  backend alongside the existing text/VLM/math paths.
- **Project tools.** New `lib/project_mod/gitignore_suggest.py` proposes
  `.gitignore` entries for files the indexer keeps re-scanning.
- **Multi-root workspace robustness.** Extra roots now persist across
  conversation switches (frontend sends `projectPaths`; backend
  `ensure_project_state()` accepts `extra_paths`). The system prompt's
  multi-root section explicitly warns about new-file creation in
  non-primary roots, since there is no auto-detection until the file
  exists.
- **`requirements.txt`.** Pin `lxml_html_clean>=0.4` so trafilatura keeps
  working on lxml 5.2+ where `lxml.html.clean` was extracted.

### Fixed
- Numerous small fixes in browser dispatch, conv_ref handling, image
  generation, LLM sanitisation, scheduler timer/manager, and trading
  decision routes (see file-level diffs).

## [0.9.3] - 2026-04-22

### Fixed
- **MCP launcher pre-flight check.** When an MCP server is configured with a
  `command` that is not on PATH (e.g. `uvx` without uv installed, `npx` without
  Node), we now emit a clear, actionable install hint instead of a cryptic
  `FileNotFoundError`. Covers uvx / npx / pipx / node / python3.

### Improved
- **Overleaf MCP auto-install resilience.** The catalog entry and migration
  rules now pin `overleaf-mcp-plus[compile]>=0.1.3`, the slimmer release that
  drops the unused playwright dependency (~100 MB faster first-run install).
- **Auto-migration upgraded.** Stale server entries from prior versions are
  rewritten on load even when only the args list differs — user-supplied env
  vars and credentials are always preserved.

## [0.9.2] - 2026-04-20

### Fixed
- Fixed Overleaf MCP server failing to launch with `FileNotFoundError: 'overleaf-mcp'`
  on machines where the package was not pre-installed. The curated registry entry
  now uses `uvx --from overleaf-mcp-plus[compile]` so the server is auto-fetched
  from PyPI on first launch, matching the behavior of the other MCP cards.

## [0.9.1] - 2026-04-20

### Improved
- Further optimized support for Claude Opus 4.7.

### Added
- Added support for the Overleaf MCP server in the curated registry
  (edit/read/compile/history on Overleaf LaTeX projects).

### Fixed
- Fixed incorrect retry behavior of the model when invoked by tools.

## [0.9.0]

- Previous release.
