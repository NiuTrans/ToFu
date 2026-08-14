# Changelog

All notable changes to tofu-open are documented in this file.

## [Unreleased]

## [0.16.0] - 2026-07-31

> **Versions 0.11.0 – 0.15.2 were never released.** They exist as `VERSION`
> bumps (and, for 0.15.0 / 0.15.2, as orphan git tags) but no GitHub Release
> was ever published behind them — the last published release was v0.14.2, and
> the macOS build leg starved on a retired runner label before the release job
> could run. Rather than reconstruct changelog entries for releases that never
> shipped, their content is folded into this entry.
>
> This is a **minor** bump, not a patch: `VERSION` sat at 0.15.2 from
> 2026-07-23 while ~1250 further commits landed, adding six new top-level
> capability packages — four of which expose their own HTTP surface
> (`routes/api_v1/research.py`, `motion.py`, `skills.py`, `private_hosts.py`).

### Added
- **Auto-research pipeline (`lib/research/`, `routes/api_v1/research.py`).**
  Give it a research direction and it harvests recent literature into a local
  paper corpus (parsed once, then reused), surveys it to map what has already
  been done, and proposes scored ideas screened against that corpus so they
  are genuinely new rather than A+B recombinations. Rejected ideas are
  reported with the reason. Exposed as the `produce_research` tool.
- **Long-form research reports (`lib/longform/`).** research → outline →
  sections(×N) → assemble, published as a cited markdown artifact. The stage
  list is data-dependent (one stage per outline section), which the static
  video stage list never exercised.
- **Motion-graphics video pipeline (`lib/motion_video/`, `routes/api_v1/motion.py`).**
  Topic → researched script → real-TTS-timed storyboard → per-scene composed
  MP4 → concat → narration mux. Every fact card carries a real source URL and
  is credited on an end card. Per-scene authoring degrades to a template floor
  so a single bad scene can never fail the film.
- **Production Substrate (`lib/production/`).** The horizontal layer under
  every "one sentence → finished product" capability: a checkpointed stage
  graph where a stage's artifact is committed as soon as its gate passes, so a
  killed process resumes at the first unfinished stage. Crash-resume is a
  correctness contract here, not an optimisation.
- **Text-to-speech / narration (`lib/tts/`, `routes/api_v1/audio.py`)** and
  voice input (speech-to-text) with a mic button in the composer.
- **Skills as a first-class noun (`lib/skills/`, `routes/api_v1/skills.py`).**
  User-installed skill packages are now decoupled from model-authored
  memories: an always-visible `<available_skills>` index plus an
  `load_skill` progressive-disclosure loader. The model channel is
  read-only; install/uninstall/toggle are user-only.
- **Project Brain — cross-conversation coordination (`lib/conversations/`).**
  Charter with human-reviewed decisions, an epic board with claims and leases,
  an activity feed, direct peer messaging, path leases, and a "Needs you"
  attention surface that aggregates everything genuinely waiting on a human.
- **Request Inspector / debug panel.** Per-request snapshot store with
  server-side folding, a `</>` affordance on bubbles and tool rows, and
  incremental retention — so "what exactly went on the wire?" is answerable
  without a debugger.
- **Manual compaction (`/compact`).** Context compaction is no longer only
  automatic: an explicit command with a REST endpoint, a frontend card, and a
  live streaming summary.
- **Air / Pro / Studio capability dial.** One toolbar control replaces the
  separate Enhance/Tools/Mode toggles, selecting a coherent capability profile
  per turn.
- **Remote Worktree / Desktop Agent.** Run a project on a remote machine from
  the desktop app — per-user bridge tokens, a "Remote devices" picker group,
  tray connect, and `run_command` parity with the local path.
- **Auto-escaping `safeHtml` tagged template (`static/js/core/safe_html.js`).**
  The chat-render path built HTML by string concatenation, hand-wrapping every
  interpolation in `escapeHtml()` — correct but fragile (one forgotten wrap is
  an XSS hole). `safeHtml\`...\`` escapes EVERY interpolation by default, with
  an explicit `raw(x)` opt-out for trusted HTML. A lint rule in
  `tests/test_frontend_safe_html.py` blocks new bare template-string HTML
  sinks in adopted render files.
- **Frontend type-check harness (`tsc --checkJs`, no build step).** Root
  `tsconfig.json` + `static/js/globals.d.ts` catch cross-file global misuse —
  typos, stale renames, dead `typeof` guards — that the shared-`window`-scope
  design otherwise fails silently at runtime. Wired as `make typecheck` and
  enforced by a monotonically-decreasing error-budget ratchet.
- **Release gates.** `scripts/release_assets.py` (is the release complete? —
  per-platform assets plus a size floor that catches a hollow build) and
  `scripts/changelog_gate.py` (is this VERSION documented? — this file is now
  a build gate, which is why the nine-version gap above can never recur).

### Changed
- **SQLAlchemy Core table-definition layer (`lib/database/_core_schema.py`).**
  Tables are defined ONCE as Core `Table` objects and compiled to correct DDL
  and DML for BOTH backends (PG `JSONB`/`IDENTITY` ↔ SQLite `JSON`/autoinc,
  paramstyle, dialect-correct upserts), retiring the hand-maintained twin-DDL
  path. Compile-only: no SQLAlchemy Engine is opened; execution stays on the
  existing connection. Generated DDL is byte-equivalent to the legacy hand-DDL
  (`tests/test_core_schema_parity.py`). Adds `sqlalchemy>=2.0`.
- **Unified the LLM SSE streaming core.** `lib/llm/stream.py` (sync) and
  `lib/llm/astream.py` (async) each carried a ~480-line copy of the identical
  SSE parsing loop, so every fix had to land twice and the copies drifted.
  That logic now lives once in `lib/llm/_sse_core.py`; the two modules are thin
  transport shells keeping only retry/backoff and transport-native handling.
  Pure code-motion — anomaly fields are emitted byte-for-byte as before, locked
  by `tests/test_sse_core_parity.py`. Net −432 lines.
- **Account ↔ wire-face separation in provider config**, so one account can
  serve both OpenAI- and Anthropic-shaped endpoints without duplicate entries.
- **Web search and fetch extracted** to the standalone `tofu_search` package;
  the app seams via `lib/search_bridge.py`. `lib/fetch/` and `lib/search/` no
  longer exist in-tree.
- **Desktop release workflow is VERSION-driven, not tag-driven.** A tag is a
  product of releasing, not evidence of it; the gate now asks the Releases API
  whether a complete asset set exists.

### Fixed
- **Weak image-caption escaping in `renderMessage`.** The image tile's `title`
  tooltip escaped only double-quotes, leaving `<`/`>`/`&` unescaped. Now uses
  the full `escapeHtml()`.
- **Chat didn't re-render on language switch / debug-mode toggle.**
  `i18n.js::_onLanguageChange` and `settings/save_export.js::saveSettings`
  called `renderMessages()` behind a `typeof … === 'function'` guard, but that
  function never existed (the real repaint is `renderChat(conv)`), so the guard
  silently swallowed the no-op. Caught by the new `tsc --checkJs` harness.
- **Duplicate `common.close` i18n key** in `static/js/i18n.js` removed.
- **Desktop downloads 404'd during a release window.** URLs were built as
  `/releases/latest/download/<cached filename>`, whose two halves have
  different lifetimes. They now come from one API payload with the tag pinned.
- **Intel Macs could not install Tofu.** Only the arm64 DMG shipped; the macOS
  build is now a per-architecture matrix on live runner labels, and the release
  refuses to publish a partial asset set.
- Numerous fixes across tool lifecycle (per-tool completion events rather than
  round-barrier), streaming transport, cache accounting, MCP launching,
  scheduler, and the paper Reading Mode pipeline.

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
