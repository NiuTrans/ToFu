# Codex repository guidance

Keep task quality as the primary objective, then minimize model turns and raw
context growth without weakening verification.

- Start unfamiliar work with one bounded discovery batch: use `rg`/`rg --files`
  and targeted ranges instead of repeatedly reading one file at a time. Do not
  dump the whole dirty worktree, full diffs, generated bundles, or large logs.
- Use `docs/README.md` as the first-hop map. Edit retained browser code in
  `frontend/src/runtime/sections/`, never the generated `app-runtime.js`. Edit
  styles in `frontend/src/styles/{application,settings}/`, never generated
  files under `static/`.
- In code mode, put independent read-only inspections in one `functions.exec`
  program and run safe independent commands concurrently. Reduce intermediate
  output inside that program; return only evidence needed for the next judgment.
- For commands expected to take 10–60 seconds, start the `functions.exec` source
  with `// @exec: {"yield_time_ms": 30000}` and give the nested command a similar
  yield. If it still returns a session/cell, poll at 30–60 second intervals;
  never busy-poll. Keep user progress updates within 60 seconds.
- Use a test ladder: smallest relevant tests first, then neighboring contracts,
  then broad gates once the worktree is stable. Do not rerun an unchanged suite.
  With concurrent writers, report that a full-suite result is a moving target.
- Batch mechanical edits, but inspect and test semantic boundaries separately.
  Preserve fault injection, rollback, authority, and user-visible error behavior.
- Before finishing a tool-heavy task, run
  `python3 audit_codex_session.py <rollout.jsonl>` when a session path is
  available. Treat fewer calls as a win only when the same correctness checks
  and required evidence still pass.

# Debugging with a conversation ID

When the user pastes a conversation ID (e.g. `mt18xr3wfs0rbq`, copied via the
sidebar copy-ID button / any `data-conv-id` attribute), the FIRST step is:

    python3 debug/inspect_conversation.py <conv_id>

Do NOT hand-query sqlite or guess table names. The script is read-only and
reports, in one pass: which stores reference the ID (sidecar
`storage_conversations` / `storage_conversation_turns` vs legacy
`conversations` / `conversation_messages`), full metadata, the transcript
(rendered through the same `conversation.get` operation the running sidecar
serves, so turn-native conversations are projected exactly like the server
renders them), compaction summaries/receipts, and matching lines from
`logs/app.log` + `logs/access.log`.
Flags: `--full` (untruncated, all messages), `--raw` (messages as JSON),
`--logs N` / `--no-logs`, `--db PATH`, `--user-id N`.

Key storage facts that save exploration turns:

- The storage authority is the **sidecar** (`server.py` defaults
  `TOFU_STORAGE_MODE=sidecar`). The inspector auto-resolves the active SQLite
  authority from the live lease/open file or fastpath lineage. Never assume
  `data/tofu.db` while a fastpath shadow exists; use `--db` only for an
  explicitly verified override.
- Turn-native conversations store their transcript in
  `storage_conversation_turns`; their `messages_json` / `msg_count` in
  `storage_conversations` are intentionally empty placeholders, NOT data loss.
- The sidecar token/port exist only inside the running server process. Offline
  inspection reads the auto-resolved WAL-backed authority in query-only mode;
  never try to recover or expose the live sidecar credential.

# Design principles (standing constraints)

Three long-term product constraints govern every change in this repo.

1. **Single-user performance now, enterprise multi-user later.** Do not build
   multi-tenancy features yet, but never hardcode single-user assumptions into
   core logic. Preserve evolution seams:
   - Data access goes through a repository/ownership layer; user/tenant
     identity is an explicit parameter, not a module-level global.
   - Authn/authz decisions belong at one middleware boundary, default deny.
   - Storage stays swappable (SQLite ↔ Postgres); no SQL dialect or filesystem
     path assumptions leak outside the storage layer.
   - Request handlers stay stateless; session state lives in declared stores
     with explicit lifecycles.
   - Any new "there is exactly one user" shortcut is architecture debt: avoid
     it, or flag it explicitly in the change with a TODO(enterprise) marker.

2. **Code is written for language models to read, not humans.**
   - Explicit over implicit: self-describing names, no magic, no hidden
     conventions; module boundaries stay sharp.
   - Single source of truth: contracts and schemas are defined once and are
     machine-readable (docs/API_CONTRACT.md, docs/EVENTS.md); everything else
     is derived or generated.
   - Discoverability: stable directory semantics; each module header states
     its responsibility, entry points, and dependencies so a model can locate
     the right context in one hop.
   - Decisions leave traces: record the "why" in JOURNAL.md / CHANGELOG.md /
     docs; tests serve as executable specifications.
   - Small, verifiable increments following the test ladder above.

3. **Personal-computer resources are a product budget.** Zero-configuration
   Tofu must coexist with the OS and a browser on an 8 GiB RAM / 500 GB disk
   computer; it never assumes it owns the whole host.
   - Defaults derive from one observable launch-time probe of affinity/cgroup
     CPU, physical/cgroup memory capacity and headroom, and volume free space.
     Probe failure falls back lean; explicit overrides and hard ceilings remain.
   - Every pool, queue, cache, log, replay stream and temporary artifact is
     explicitly bounded and has a lifecycle tied to user-visible value.
   - Durable user state is distinct from reconstructible transport/cache data;
     reclaim the latter, never weaken durability or silently delete the former.
   - Heavy optional capabilities load lazily or run behind a bounded worker
     boundary. Personal and distributed budgets come from explicit profiles,
     not single-user assumptions embedded in core logic.
   - A change that affects resident concurrency or persistent growth includes
     a measurement/budget test and updates the owning operations contract.

Keep this file short and stable; detailed rationale and audit results live in
docs/ (see docs/ENTERPRISE_READINESS_AUDIT.md).

# Directory guidance

- A nested `AGENTS.md` augments this file for its subtree; the closest file is
  the operational map for that code. Do not repeat root-level invariants in
  child files.
- Add or revise a child guide when a directory gains a distinct owner,
  source-of-truth, generated boundary, or verification path. Keep durable
  product behavior in the referenced contract or domain document, not here.
- The canonical filename is uppercase `AGENTS.md`.
- Generated, vendored, runtime, and user-data trees do not receive local
  guidance. In particular, do not add guides under `static/`, `dist/`,
  `build/`, `uploads/`, `data/`, `logs/`, dependency caches, `.tofu*`, or
  evaluation workdirs; edit and document their source owner instead.
