# Repository guidance

This file is the single source of repository-wide rules for every coding agent;
read it completely before changing code. Keep task quality as the primary
objective, then minimize model turns and raw context growth without weakening
verification.

- Use isolated worktrees for sustained changes in a shared checkout. Never
  reset, clean, or replace the shared tree to prepare a task. When other
  (uncommitted or in-flight) work exists in the workspace, NEVER run
  workspace-destroying commands such as `git checkout --`, `git checkout
  <branch>`, `git switch`, `git restore`, `git stash`, `git reset --hard`,
  or `git clean`: they can silently discard concurrent edits. Create each worktree
  with `python3 scripts/lane.py new <branch>` (never from the platform task
  workspace's `HEAD`, which may be a task-input snapshot): it creates the
  branch on a fast local disk and recycles a landed lane's checkout, so a new
  lane costs about a second rather than minutes on the network volume. Resume
  a lane whose checkout is gone with `python3 scripts/lane.py restore <branch>`;
  settlement saves its uncommitted work at `refs/tofu/lanes/<branch>`.
  Commits made in an isolated worktree
  never reach the current branch by themselves: the
  executing agent (the model — never the human user) must land them itself,
  immediately once the worktree work is finished and verified, not later and
  never as a hand-off: run `python3 scripts/git_land.py <branch>` from the
  shared checkout. Unlike `git merge`, it does not refuse when other writers
  have uncommitted edits in the same files; it carries those edits forward
  with a three-way merge, recycles a lane's clean checkout for the next lane
  and deletes the branch. Known generated outputs are rebuilt from merged
  sources; uncommitted sources and their outputs stay uncommitted. Do not
  reset generated files to unblock it. If it refuses, nothing was changed;
  follow its printed instruction (merge the current branch into the worktree
  branch to resolve a real conflict, or rerun after another writer's
  overlapping lines are committed). It runs no tests (owner decision
  2026-11-08): the verification done in the lane is the landing's evidence,
  so land verified work promptly instead of rerunning a test ladder.
  A `[Concurrent change]` notice means another conversation's unlanded work
  or a landing touches your files: read their change (the notice prints the
  command) and merge the current branch before verifying. A task is
  not complete until its worktree commits are merged into the current
  branch. The merge commit's first parent is the pre-integration backup; the
  tool prints it, so record that SHA in the final answer instead of creating
  `backup/*` branches.
- Start unfamiliar work with one bounded discovery batch over the owning
  modules, contracts, tests, and current changes: use `rg`/`rg --files` and
  targeted ranges instead of repeatedly reading one file at a time. Do not
  dump the whole dirty worktree, full diffs, generated bundles, or large logs.
  On this host's FUSE mount, prefer `git ls-files` and `git grep` over `find`/
  `grep -r`: deep `find` walks intermittently return empty.
- Worktree environment: symlink `node_modules` from the shared checkout (it is
  gitignored); never run `npm install` in a worktree or under `data/`, which
  re-resolves the shared tree. Run Python tests with the project
  `.venv/bin/python3` explicitly whenever another environment (e.g. conda for
  `node`) is on `PATH`; its `python3` makes results invalid evidence. The
  command environment exports `PYTHONPATH=<shared checkout>`, so in a worktree
  a `/tmp` probe (or any script that does not put the worktree first on
  `sys.path`) imports the shared `lib`: prefix `PYTHONPATH=$PWD` and print
  `lib.__file__` before trusting the result. Under
  heavy host load, fixed test deadlines (test bundling, bundle canary) can
  expire: rerun those failures serially before attributing them to a change.
- Use `docs/README.md` as the first-hop map to the owning module and contract;
  `docs/catalog.json` is the machine-readable documentation inventory. Change
  the authority first, then derive generated clients and bundles. Edit
  retained browser code in `frontend/src/runtime/sections/`, never the
  generated `app-runtime.js`. Edit styles in
  `frontend/src/styles/{application,settings}/`, never generated files under
  `static/`.
- Repo-root local trees that are untracked on purpose (gitignored): `promo/`
  (marketing HTML/PNGs) and `codex/` (a local vendored Codex CLI checkout used
  for comparison/evaluation). Neither is a runtime dependency of tracked code:
  `lib/oauth/codex*` and `lib/skills/paths.py` spawn a `codex` binary and scan
  `.codex/skills` by name only. Do not track or delete these trees.
- Generated Python modules follow the package-local `generated_*.py` (or
  `*_generated.py` where the name predates the convention) prefix, never loose
  hand-mixed names in a package root. See CONTRIBUTING.md “Generated files”.
- Batch independent read-only inspections with the live tools' documented
  batching or parallel-call interface. Tool schemas, not repository examples,
  define the available names, arguments, and execution semantics. Return only
  evidence needed for the next judgment.
- Background long commands with `run_command(background=true)` when useful.
  The receipt and the tool description say whether the completion reaches you
  automatically: only a task that owns its conversation turn is woken with it.
  A swarm worker or other delegated run collects its handle with `poll` before
  it finishes; a leaf worker's `poll` and `kill` reach only the `bg_*` commands
  it started. A headless API request runs every command in the foreground. Use
  `poll` with a bounded wait only when no useful work remains. Never busy-poll
  or infer success from an absent session. Keep user progress updates within
  60 seconds.
- Add or update the smallest executable specification for the behavior, then
  use a test ladder: smallest relevant tests first, then neighboring contracts,
  then broad gates once the worktree is stable. Do not rerun an unchanged suite.
  Default Python development checks use `make test TESTS="tests/<owner>/..."`
  (or `.venv/bin/python3 scripts/verify.py run <paths>`); bare `make test` selects current changes
  and `make test-plan` explains them. Use direct pytest for case-level debugging
  or specialized markers; domain, coverage and release gates remain required.
  In a clean, committed lane, run final test files through
  `python3 -m lib.project_mod.landing_verification record <test files>` from
  that lane; it checks the committed revision and records the runs, so later
  verification of unchanged inputs reuses them.
  With concurrent writers, report that a full-suite result is a moving target.
- Update the owning current-state document with the change; delete superseded
  plans instead of leaving an archive beside authoritative documentation.
- Batch mechanical edits, but inspect and test semantic boundaries separately.
  Preserve fault injection, rollback, authority, and user-visible error behavior.
- Before finishing a tool-heavy task, run
  `python3 audit_codex_session.py <rollout.jsonl>` when a session path is
  available. Treat fewer calls as a win only when the same correctness checks
  and required evidence still pass.

# High-value entry points

- Application assembly: `lib/app/app_assembly.py`
- HTTP blueprints: `routes/__init__.py`
- Conversation protocol: `contracts/conversation_sync_v3.yaml`
- API and event contracts: `docs/contracts/API_CONTRACT.md`,
  `docs/contracts/EVENTS.md`
- Durable state: `lib/storage/` and `lib/storage_authority_sql/`
- Task execution: `lib/tasks_pkg/` and `lib/agent_core/`
- Browser source: `frontend/src/`
- Architecture: `docs/ARCHITECTURE.md`

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
Flags: `--full`, `--raw`, `--json`, `--context`, `--logs N` / `--no-logs`,
`--log-match REGEX`, `--db PATH`, `--user-id N`, `--api-url ORIGIN` /
`--api-token-env NAME`, `--logs-dir PATH`, `--tools`, `--no-anomalies`.

Storage has one selected authority: native TofuDB or the SQL Sidecar;
`TOFU_STORAGE_MODE` is retired. Preserve the configured native backend; never
switch stores to work around a failure. With no source flags the inspector
follows the configured backend: for SQLite it resolves the live fastpath; for
TofuDB it auto-discovers the running application's loopback API (configured
`PORT`, default 15000) and reads through the v4 conversation endpoints. Use
`--db` only for an explicitly verified offline SQLite override, `--api-url` to
target a different application. Backend selection and diagnostic limits live
in `docs/STORAGE.md`.

Runtime facts that shortcut diagnosis:

- Tasks run in two processes: the API host (`server.py`) and the resident
  execution host (`serverctl_pkg/execution_host.py`). Process-local state
  (steers, background-command registries, idle checks, swarm sessions) does
  not cross them; an API route that must reach it uses the private execution
  control (`lib/tasks_pkg/command_control_runtime.py`). Locate a task's
  process (`py-spy dump --nonblocking <pid>`) and read both processes' logs
  before blaming a tool. Search logs with `rg -a`.
- Restart and lifecycle outcomes are recorded only in
  `logs/server-manager.log`; a browser-initiated restart cannot report them.
- A merged fix is live only after the owning process restarts; storage also
  needs a matching engine build (`serverctl_pkg.native_engine.source_fingerprint`).
  Compare the running build with the merged source before concluding a fix
  failed.

# Tool and harness quality

The harness — model-facing tools, the scripts under `debug/` and `scripts/`, MCP
servers, tool schemas and descriptions, and this guidance — is part of the
product, not a fixed backdrop. A defect you hit while doing a task is evidence
about that harness, and fixing it is part of the job. Same principle as
`docs/AGENT_CAPABILITY_GUIDE.md`: fix the chassis, not the caller.

- Track incidental findings while you work: tool, exact invocation, expected vs
  observed, and whether the task result was affected. Never silently route
  around a defect — name the workaround and its residual risk in the final
  answer.
- Reproduce it without the task's context before blaming the tool. Misuse, a
  stale assumption, or a documented limitation is not a defect; say which it
  was and continue.
- Fix it before finishing the task when the defect can corrupt a task result,
  or when the fix is local, testable in the existing ladder, and changes no
  public contract or schema. Add the failing specification first, keep the
  harness fix as its own commit, and state the fix plus its verification in the
  final answer.
- Fix the mechanism, not only the instance: the missing validation, vague
  error, misleading tool description, or absent guardrail that allowed the
  defect to happen.
- Never encode a workaround as the contract: no caller-side fallback for a tool
  that should be fixed, no test asserting the buggy behavior, no document
  sentence that makes the defect the intended API.
- Report instead of fixing when the change crosses an authority or contract,
  spans owners, or would displace the user's task. Give the finding, the
  smallest proposed fix, and what you did not verify.
- The task stays primary. Do not turn a delivery into a harness project, do not
  "optimize" tools you merely found inelegant, and stop when a fix outgrows the
  small-local rule above. A defect reported once and hit again is no longer
  report-only: record it as a project memory and fix it or escalate with a
  concrete proposal.

# Design principles (standing constraints)

Five long-term product constraints govern every change in this repo.

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
   - All Tofu-owned outbound networking uses the shared network authority
     (`docs/NETWORK.md`). New subscriptions, SDK clients and integrations
     inherit its proxy/bypass, route-health and recovery policy; never add
     another domain-local route selector or blind request retry loop.

3. **Hardware never limits work.** Owner decision 2026-10-24; a fixed policy,
   not a tuning opportunity (`docs/RESOURCE_CONTROL.md`, "Hardware never
   limits work"). CPU count, affinity/cgroup CPU quota, memory capacity or
   headroom, process RSS and cgroup pressure never size, cap, queue, refuse,
   shrink, recycle or kill user work. Do not reintroduce probe-derived worker
   or task ceilings, memory-headroom or RSS refusals, worker recycling,
   memory-driven capacity shrinking, per-profile worker formulas, or "server
   busy / at capacity" errors for local capacity, however well-intentioned.
   - Every launch setting declares a class in
     `contracts/resource_policy_v1.json`: `unlimited` (admission, concurrency
     and fan-out; always `runtime_guards.UNLIMITED`); `fixed` (idle-reuse
     hints of demand-driven pools, retry budgets, deadlines), a
     hardware-independent constant; only evict-only `retention` (caches, TTLs,
     logs, replay windows, disk budgets) may follow the launch probe.
   - Executors start every accepted job. Memory exhaustion is left to the
     operating system; best-effort cache relief never blocks work.
   - Durable user state is distinct from reconstructible transport/cache data;
     reclaim the latter, never weaken durability or silently delete the former.
   - Heavy optional capabilities load lazily or run behind a worker boundary
     so they never tax startup or idle.
   - A change that affects persistent growth or retention includes a
     measurement and updates the owning operations contract.
   - `tests/meta/test_hardware_never_limits_work.py` is the executable form of
     this rule. Changing it requires a new explicit owner decision recorded in
     the authority doc, never an agent's judgment.

4. **Find commonality before adding machinery.** Chat, Goal, Evolution and
   specialized deliverables reuse the existing invocation, coordination, Turn
   projection and rendering owners. Modes vary prompts, capabilities, actor
   policy and graph paths, not model/tool loops or presentation state machines.
   Inspect the common owner first; extend it with explicit policy ports where
   needed, and remove redundant paths with behavioral parity tests. Synthetic
   users never inherit human authorization, except through an explicit,
   revocable, audited delegation the owner grants (owner decision 2026-11-06:
   the Global Router acts, never speaks, for the owner; `docs/ROUTER.md` R9),
   and domain-specific verification,
   learning and durable settlement remain explicit rather than flattened away.

5. **Optimizations add no tuning hyperparameters.** Owner decision
   2026-10-28. An optimization wins by doing less or smarter work: a better
   algorithm or data structure, batching, exact caching with explicit
   invalidation, lazy loading, or deleting redundant paths. It never adds a
   knob that constrains what the product does or how well it does it: no
   magic thresholds, caps, quotas, top-k or result counts, truncation sizes,
   sampling rates, heuristic cutoffs, batch/window/debounce intervals, extra
   timeouts or retry counts, and no new env var, setting or feature flag
   introduced to tune, gate or "safely" limit the change.
   - A constant is acceptable only when something outside the optimization
     defines it: an external protocol, provider or API limit, or a
     correctness/liveness contract (deadline, retry budget) recorded in its
     owning contract. Define it once, named after that source.
   - If the gain exists only at a tuned value, it is not an optimization:
     find the structural fix or leave the code unchanged.
   - Removing an existing arbitrary knob is itself a valid optimization.

Keep this file short and stable; detailed rationale and audit results live in
docs/ (see docs/ENTERPRISE_EVOLUTION.md).

# Directory guidance

- A nested `AGENTS.md` augments this file for its subtree; the closest file is
  the operational map for that code. Do not repeat root-level invariants in
  child files.
- Add or revise a child guide when a directory gains a distinct owner,
  source-of-truth, generated boundary, or verification path. Keep durable
  product behavior in the referenced contract or domain document, not here.
- The canonical filename is uppercase `AGENTS.md`; do not add per-agent copies
  such as `CLAUDE.md`.
- Generated, vendored, runtime, and user-data trees do not receive local
  guidance. In particular, do not add guides under `static/`, `dist/`,
  `build/`, `uploads/`, `data/`, `logs/`, dependency caches, `.tofu*`, or
  evaluation workdirs; edit and document their source owner instead.
