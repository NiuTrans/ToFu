# Codex repository guidance

Keep task quality as the primary objective, then minimize model turns and raw
context growth without weakening verification.

- Use isolated worktrees for sustained changes in a shared checkout. Never
  reset, clean, or replace the shared tree to prepare a task. When other
  (uncommitted or in-flight) work exists in the workspace, NEVER run
  workspace-destroying commands such as `git checkout --`, `git checkout
  <branch>`, `git switch`, `git restore`, `git stash`, `git reset --hard`,
  or `git clean`: they can silently discard concurrent edits. Commits made
  in an isolated worktree never reach the current branch by themselves: the
  executing agent (the model — never the human user) must merge them back
  itself, and must do so immediately once the worktree work is finished and
  verified, not later and never as a hand-off — `git merge`/`git cherry-pick`
  of the worktree branch (both refuse to overwrite uncommitted concurrent
  edits, so they are safe by construction) — or integrate only reviewed
  paths after checking their current content, then remove the worktree with
  `git worktree remove`. A task is not complete until its worktree commits
  are merged into the current branch. Preserve concurrent edits and keep an
  isolated commit plus a pre-integration backup.
- Start unfamiliar work with one bounded discovery batch: use `rg`/`rg --files`
  and targeted ranges instead of repeatedly reading one file at a time. Do not
  dump the whole dirty worktree, full diffs, generated bundles, or large logs.
  On this host's FUSE mount, prefer `git ls-files` and `git grep` over `find`/
  `grep -r`: deep `find` walks intermittently return empty.
- Use `docs/README.md` as the first-hop map. Edit retained browser code in
  `frontend/src/runtime/sections/`, never the generated `app-runtime.js`. Edit
  styles in `frontend/src/styles/{application,settings}/`, never generated
  files under `static/`.
- Batch independent read-only inspections with the live tools' documented
  batching or parallel-call interface. Tool schemas, not repository examples,
  define the available names, arguments, and execution semantics. Return only
  evidence needed for the next judgment.
- Background long commands with `run_command(background=true)` when useful.
  Completion is delivered automatically; use `poll` with the returned handle
  and a bounded wait only when no useful work remains. Never busy-poll or infer
  success from an absent session. Keep user progress updates within 60 seconds.
- Use a test ladder: smallest relevant tests first, then neighboring contracts,
  then broad gates once the worktree is stable. Do not rerun an unchanged suite.
  With concurrent writers, report that a full-suite result is a moving target.
- Batch mechanical edits, but inspect and test semantic boundaries separately.
  Preserve fault injection, rollback, authority, and user-visible error behavior.
- Before finishing a tool-heavy task, run
  `python3 audit_codex_session.py <rollout.jsonl>` when a session path is
  available. Treat fewer calls as a win only when the same correctness checks
  and required evidence still pass.

# Registering models

- Adding or changing a model must follow `docs/MODEL_REGISTRATION.md` and the
  shared `lib/model_routing/registration.py` admission boundary. Use maintained
  exact catalog rules first; if facts are missing, search official creator and
  provider documentation and submit evidence, release date, prices/units,
  context, capabilities, and explicit reasoning support/efforts/default/off
  behavior. If evidence is unavailable, report the gap; never invent values
  or bypass admission by writing runtime storage. Incomplete facts may be saved
  only as a non-callable draft through the shared admission boundary.

# Debugging with a conversation ID

When the user pastes a conversation ID (e.g. `mt18xr3wfs0rbq`, copied via the
sidebar copy-ID button / any `data-conv-id` attribute), the FIRST step is:

    python3 debug/inspect_conversation.py <conv_id>

Do NOT hand-query sqlite or guess table names. The script is read-only and
reports in one pass the stores referencing the ID, full metadata, the
transcript (projected exactly like the server renders it), compaction
summaries/receipts, and matching `logs/app.log` / `logs/access.log` lines.
Flags: `--full`, `--raw`, `--logs N` / `--no-logs`, `--db PATH`, `--user-id N`,
`--api-url ORIGIN` / `--api-token-env NAME`, `--logs-dir PATH`,
`--tools`, `--no-anomalies`.

Storage has one selected authority: native TofuDB or the SQL Sidecar;
`TOFU_STORAGE_MODE` is retired. Preserve the configured native backend; never
switch stores to work around a failure. With no source flags the inspector
follows the configured backend: for SQLite it resolves the live fastpath; for
TofuDB it auto-discovers the running application's loopback API (configured
`PORT`, default 15000) and reads through the v4 conversation endpoints. Use
`--db` only for an explicitly verified offline SQLite override, `--api-url` to
target a different application. Backend selection and diagnostic limits live
in `docs/STORAGE.md`.

# Design principles (standing constraints)

Three long-term product constraints govern every change in this repo.

1. **Single-user performance now, enterprise multi-user later.** Do not build
   multi-tenancy features yet, but never hardcode single-user assumptions into
   core logic. Preserve evolution seams:
   - Data access goes through a repository/ownership layer; user/tenant
     identity is an explicit parameter, not a module-level global.
   - Authn/authz decisions belong at one middleware boundary, default deny.
   - Storage stays swappable (TofuDB / SQLite / Postgres); no SQL dialect or filesystem
     path assumptions leak outside the storage layer.
   - Request handlers stay stateless; session state lives in declared stores
     with explicit lifecycles.
   - Any new "there is exactly one user" shortcut is architecture debt: avoid
     it, or flag it explicitly in the change with a TODO(enterprise) marker.

2. **Code is written for language models to read, not humans.**
   - Explicit over implicit: self-describing names, no magic, no hidden
     conventions; module boundaries stay sharp.
   - Single source of truth: contracts and schemas are defined once and are
     machine-readable (docs/contracts/API_CONTRACT.md, docs/contracts/EVENTS.md); everything else
     is derived or generated.
   - Discoverability: stable directory semantics; each module header states
     its responsibility, entry points, and dependencies so a model can locate
     the right context in one hop.
   - Decisions leave traces: record the "why" in the owning authority doc
     under `docs/`; completed work lives in Git history (see `docs/README.md`).
     A non-trivial decision names its Problem, Decision, and Alternatives
     considered; a record with no defeated alternative invites re-litigation.
     Tests serve as executable specifications.
   - Lifecycle, concurrency, subprocess, and teardown work follows
     `docs/DEFENSIVE_PATTERNS.md`.
   - Small, verifiable increments following the test ladder above.

3. **Performance scales to the user's machine.** Zero-configuration Tofu
   uses the resources the user actually has; it never assumes a fixed low-end
   host, and it never caps performance below what the machine can deliver.
   Given the same resources, squeeze out the maximum performance.
   - Defaults derive from one observable launch-time probe of affinity/cgroup
     CPU, physical/cgroup memory capacity and headroom, and volume free space,
     and scale with what is probed — up as well as down. Probe failure falls
     back lean; explicit overrides remain.
   - Pools, queues, caches, logs, replay streams and temporary artifacts are
     sized from the probed capacity and have lifecycles tied to user-visible
     value; bounds exist to prevent leaks and runaway growth, not to starve
     performance.
   - Durable user state is distinct from reconstructible transport/cache data;
     reclaim the latter, never weaken durability or silently delete the former.
   - Heavy optional capabilities load lazily or run behind a worker boundary
     so they never tax startup or idle; their concurrency scales with the
     probed capacity, not a hardcoded single-user assumption.
   - A change that affects resident concurrency or persistent growth includes
     a measurement and updates the owning operations contract.

Keep this file short and stable; detailed rationale and audit results live in
docs/ (see docs/ENTERPRISE_EVOLUTION.md).

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
