# Testing strategy

Tests protect product behavior and architecture ownership. They are not an
archive of incidents, implementation line numbers, or past migrations.

Observation wrappers around production recorders must return their receipts.
System-note injection uses the recorder's block identity and rolls back when
recording fails; collecting call arguments must preserve that boundary.

## Agent-session audit

`python3 audit_codex_session.py <rollout.jsonl>` and its `--json` form use the
[v2 metric contract](../contracts/codex_session_audit_v2.schema.json).
`audit_session()` is the same read-only owner used by the CLI. Focused
specifications live in `tests/meta/test_audit_codex_session.py`; this application
harness remains excluded from the developer runtime distribution.

Problem: a JavaScript source-text regex reported nested tool executions without
execution evidence. String literals, unexecuted branches and shadowed `tools`
variables inflated the count, while aliases and loops could undercount it.

Decision: count tool-call response items as recorded and expose runtime
`item_completed` observations separately. Runtime items include commands, file
changes, messages, reasoning and any future item types. Deduplicate by the full
`(thread_id, turn_id, item.id)` identity; the first observed type owns the count.
Unidentifiable completion notifications and repeated identities have explicit
counters. A failed command is still an observed item, not a successful result.
These records do not supply a complete nested-invocation graph or per-function
call count, so both `nested_tool_calls` fields are `null`, with observation
status `unavailable`. Consumers must check `schema_version: 2` and must not
convert unavailable evidence into zero. The earlier unversioned numeric fields
were source-pattern counts, not execution counts.

Alternatives rejected: parsing JavaScript more accurately still cannot determine
execution, and counting every runtime item as a tool call mixes messages, UI
projections and tool activity. No producer or runtime transport is modified to
invent missing receipts. A separately labelled static estimate was also rejected
because it cannot support an execution-efficiency conclusion.

The remaining metric definitions stay explicit in the schema. Token values are
the latest recorded totals; model-turn counts use positive usage snapshots.
The legacy `avoidable_wait_rounds` field is only `min(yielded_execs, wait_calls)`;
the CLI labels it a heuristic upper bound, not a verdict about necessary waits.
Existing CLI thresholds and explicit-price shadow-cost calculations remain
available. Performance and correctness conclusions require their own evidence.

The audit streams JSONL once. Usage snapshots, tool-call/output correlation and
runtime item identities have separate invocation-local accumulators. Memory
follows the current record, latest usage totals and identity metadata, rather
than retaining every source/output payload, token sample or completed duration.
Read failures still propagate and close the input; a partial read is never a
successful report. Synthetic and frozen-input differential checks preserve all
v2 metric values, CLI thresholds and error outcomes while a payload-retention
test guards the large-history case.

The source-matched TofuDB build helper reports the first primary compiler error
and its location within its existing diagnostic budget. Warning-heavy tails
previously hid the actionable failure. The original subprocess exception retains
full output; source builds still refuse stale binaries and retire all toolchain
descendants on interruption. Verify `test_tofudb_source_build_retirement.py`.

## Test layers

Turn delivery tests import their shared fixture functions explicitly from
`tests/turn_lifecycle/test_task_event_delivery.py`. A collectable test module
cannot also supply them reliably through `pytest_plugins`: collecting that
module first scopes its fixtures to its own node, so later consumers can lose
`storage`/`turn_service` or their backend parametrization. Explicit fixture
imports preserve each consumer's SQL/native cases regardless of collection
order. Verify the provider module followed by `test_turn_event_encode_once.py`
with `-k test_snapshot_delta_is_encoded_once`, plus the batch delivery tests.

Fixtures imported only for pytest argument injection use an explicit re-export
(`from owner import fixture as fixture`, or the module's `__all__`). This keeps
pytest discovery and lint usage aligned; removing the import would break fixture
resolution. `tests/model_routing/test_context_activity.py` exercises that form.

The lean installer portability job installs every plugin explicitly loaded by
the repository's pytest configuration, even with `--noconftest` and plugin
autoload disabled. `tests/installer/test_installer_portability_workflow.py`
compares the declared tools with plugin distribution metadata. Problem: an
omitted plugin prevents collection before any installation behavior is tested.
Decision: keep the job lean while declaring its actual test dependencies;
removing required plugins would bypass the shared test contract.

| Layer | Purpose | Typical command |
|---|---|---|
| Unit | Pure policy, reducer, parser, repository and lifecycle behavior | `make test-unit` |
| API | Authenticated HTTP plus application/storage integration | `make test-api` |
| Frontend owner | TypeScript owner, generated artifact and serving contracts | `make test-frontend` |
| Browser journey | A small set of user-critical flows through the real app | `make test-e2e` |
| Visual | Layout and interaction behavior requiring a browser | `make test-visual` |
| Supply chain | CycloneDX plus source/image vulnerability, secret and deployment scans | CI `supply-chain` + `container` jobs |
| Release | All current layers | `make test-all` |

Every Makefile test entrypoint inherits plugin registration and the per-test
hang guard from `pyproject.toml`. Later command-line flags must not unload the
timeout plugin. `tests/meta/test_pytest_hang_guard.py` exercises the actual Makefile
entrypoint with a bounded sleeper, in addition to direct pytest execution.

Report worker tests share `tests/support/paper_report.py` as their lifecycle
owner. Problem: a setup failure before worker dispatch left an active execution
registered, and a fixed task ID then broke unrelated cases. Decision: each
context owns a unique execution, isolated patches and unconditional task
retirement, including failure before dispatch. Pytest fixtures yield from this
context through declared fixture dependencies. Alternatives rejected: relying
on garbage collection or random IDs alone leaves the leaked execution alive;
calling fixture `__wrapped__` bypasses pytest teardown. Lifecycle specifications
live in `tests/paper/test_paper_report_fixture.py`.

Unit and API suites are the main diagnostic layers. Browser journeys cover
cross-boundary user outcomes that cannot be proven cheaply below the browser;
they are deliberately few and high value.

Project conversation provenance has separate page-projection and assignment-history
specifications in `tests/project_mod/test_coordinator_conversations.py`. Problem:
two same-named definitions silently replaced the page specification during Python
import. Decision: give each boundary a distinct collected test; verify collection
as well as execution. Combining them was rejected because it would lose the
independent explicit-source precedence check.

Artifact projection tests inject `tests/support/artifact_record_store.py` at the
record-storage boundary. Problem: relying on an unstarted store made assertions
change when an earlier API test initialized storage. Decision: exercise alias
projection with an isolated version-CAS store and explicitly control canonical
result retention success or failure. Model pointers resolve to the same original
digest; provenance and source coverage retain their original meaning. Alternatives
rejected: forcing storage failure everywhere hides the successful projection,
and accepting either result without controlling storage leaves order dependence.
Verify long-agent result contracts and tool-result artifact-alias contracts.

Private PostgreSQL fixtures expose captured `initdb` stderr (or stdout when
stderr is absent) and retain the original exception as the cause. Problem:
capturing bootstrap output hid the reason for setup failure behind an exit
code. Decision: report the diagnostic while the owning context removes its
private files. Silent skips and retries were rejected because they conceal
missing backend evidence. Verify `tests/meta/test_postgres_cluster_fixture.py`.

Private PostgreSQL cluster lifetime. **Problem:** each postmaster creates a SysV
interlock segment that only its own shutdown removes, and the default POSIX
dynamic shared memory adds `/dev/shm/PostgreSQL.*` files. The fixture escalated
a fast shutdown to SIGKILL after 10 s and ran the postmaster in its own session,
so a slow shutdown checkpoint, a SIGKILLed pytest or a hang-guarded run leaked
both or left the cluster under PID 1. On 2026-10-31 all 4,096 SysV IDs were
taken (4,072 unattached 56-byte segments of dead creators), 9,700 DSM files held
5.15 GB of RAM-backed `/dev/shm`, one orphaned cluster had run for four days,
and every `initdb` failed with "all available shared memory IDs have been taken".
**Decision:** a cluster root is a pytest root (`tofu-test-postgres-pid-<owner>-*`,
below) and its server runs with `dynamic_shared_memory_type=mmap`, so DSM lives
in `data/pg_dynshmem` and leaves with the root. On Linux the postmaster starts
through an exec wrapper that arms `PR_SET_PDEATHSIG` with SIGINT and refuses to
start if its owner already exited: owner death, SIGKILL included, requests the
fast shutdown that removes the segment. The separate session keeps signals to
the owner's process group from SIGKILLing it. Teardown sends SIGINT and waits
for exit without its own deadline (the hang guard owns wall-clock limits) and
never SIGKILLs. The root is removed only after that exit; an interrupted wait
leaves it to the reclaim. Every fixture start runs the shared dead-owner
reclaim, which retires a postgres root through `retire_cluster_root`: a process
still working in the recorded data directory gets SIGINT and keeps the root for
a later pass. Otherwise the segment recorded in `postmaster.pid` is removed
only while it still has the recorded key and creator PID, belongs to this UID
and has nothing attached; then the directory goes. An unprovable record keeps
the root. Specs cover normal teardown, a SIGKILLed owner, a SIGKILLed
postmaster's leaked segment and the identity checks (re-keyed or re-created ID,
attached segment, live owner, serving process). **Alternatives rejected:** a
longer SIGKILL deadline or `fsync=off` (the next slower shutdown still leaks;
the deadline was the defect); a guardian process per cluster on a lifeline pipe
(a second lifecycle owner and restart protocol beside the root reclaim);
`preexec_fn` arming (Python code in a forked multi-threaded worker can
deadlock); `rootless_vm/pdeath_exec.py` (sandbox product code, and its SIGTERM
is a smart shutdown that waits for sessions); raising SHMMNI or sweeping every
unattached segment (host-global, and it cannot prove ownership); removing roots
by name alone (deletes files under a live postmaster and loses the segment
record). **Limits:** owner binding and segment proofs are Linux-only (elsewhere
a root with a `postmaster.pid` is kept); `initdb`'s own bootstrap backends keep
the default DSM type; `tofu-pg-*` directories from before this change carry no
owner and are not reclaimed.

Problem: individual SDK tests depended on extra `PYTHONPATH` settings, and an
installed editable package could supply different code. Decision: `tofu_sdk`
uses the existing declared first-party source finder in `tests/conftest.py`,
including its refusal of foreign cached modules, missing children and escaped
paths. Alternatives rejected: per-file path insertion and relying on suite
collection order cannot establish which implementation was tested. Independent
wheel/tarball smoke checks run outside pytest and the checkout to verify the
actual distributable package.

Public browser bridge checks inspect TypeScript's effective `Window.TofuModules`
type. Problem: slicing interface text from the bootstrap file broke when
application composition moved. Decision: resolve the actual typed port and
require its public capabilities before rejecting exposed Turn internals.
Reject file-relative text ranges, which confuse source layout with the public
boundary. Spec: `test_turn_store_owner_migration.py`.
Runtime composition checks bundle the real owner graph and compare canonical
function identities plus callable public operations. Direct-import text checks
were rejected: delegating commands to their own module must not require keeping
a dead facade import merely to satisfy a test.

Native maintenance functional tests retain their exact progress, repair and
durability predicates with a fixed 30-second failure watchdog. Problem: a
three-second polling deadline confused slow real fsync under concurrent test
load with a broken scheduler. Decision: separate that functional watchdog from
retry-cadence specifications, which advance an explicit clock, and from measured
latency acceptance. Alternatives rejected: weakening state assertions or removing
durability from the fixture would stop checking the behavior. The watchdog is
not a production latency guarantee. Owner: `packages/tofu-db/src/maintenance.rs`.

SIGKILL WAL-progress children hold the injected publication pause until process
exit, with lifetime owned by the parent's pipe and kill/reap guard. Problem:
the shared ten-second I/O gate expired during a large write fixture, injected a
real I/O error and correctly fenced the engine before its ACK marker. Decision:
keep a pause distinct from fault injection; strict CONTROL coverage deliberately
waits beyond that former expiry before writing. Alternatives rejected: extending
an arbitrary gate timeout or ignoring the resulting authority fence would still
confuse scheduling with storage correctness. In-process gate tests retain their
bounded failure watchdog. Owner: `packages/tofu-db/src/engine_wal_progress_tests.rs`.

## Iteration ladder

Problem: pytest consumes imported fixtures through discovery, without ordinary
Python reads of those names. Decision: fixtures reused from another test module use
an explicit local export (`from ... import fixture as fixture`), visible to both
pytest and lint. Alternatives rejected: removing the import loses the fixture;
blanket unused/redefinition suppressions also hide accidental duplicate imports.
Verify fixture providers and collected test identities when changing these exports.

Problem: the shared paper SQL fixture initialized its schema in autocommit
mode, leaving partial tables and an open connection after bootstrap failure
while paying repeated durable commits during schema creation. Decision:
`tests/support/paper_storage.py` follows the production SQLite startup boundary:
schema and operation-registry initialization complete in one transaction; failed
construction rolls back and closes the connection, including control exceptions.
Each test still owns its database. Alternatives rejected: disabling durability
or sharing a mutable database between tests would weaken the exercised contract.
`tests/paper/test_paper_storage_fixture.py` verifies failure cleanup and records
SQL transaction counts and setup duration without imposing a latency threshold.

Motion and paper progress tests share `tests/support/motion_media.py` for codec
doubles. Problem: independent probes reported a whole narrated film as a silent
fixed-duration scene. Decision: derive metadata from the owning film timeline
and audio inputs while keeping the real media gates and progress assertions.
Bypassing the gate was rejected because it would hide invalid publication.
Verify both the motion P3 and paper media-UX suites when changing these doubles.

Problem: integration wrappers delegate assertions to neighboring test drivers,
including module aliases, function-local imports and callable parameter matrices.
Decision: the health census resolves explicit test imports statically, within
their owning module/function/class and the existing two-call depth. A literal
single-callable matrix counts only if every listed driver resolves and asserts;
shadowed names, unknown cases and assertion-free drivers remain findings.
Alternatives rejected: executing imports would run fixture side effects; matching
unqualified method names could borrow an unrelated driver's assertions. Synthetic
export paths use the existing explicit fixture-path declaration, not a blanket
dead-path exemption. Verify `tests/meta/test_suite_health_ratchet.py`.

Problem: fixture repositories create realistic filenames that the dead-path
audit mistook for missing shipped files. Decision: recognize an exact path
expression after a local file-creation call, within the same lexical scope.
The same spelling through another root or test still requires a live anchor.
Alternatives rejected: per-test waivers and suppressing every occurrence of a
created filename would hide real stale reads. The audit never executes tests.

Problem: source-anchor provenance confused generated files below a test's
``tmp_path`` root with shipped files, and nested assertions were either always
counted or lost when callbacks executed through an executor. Decision: propagate
temporary roots within each test, follow explicit callback and exception-injection
edges, require pytest's ``test_`` collection spelling, and count ``approx`` only
inside a comparison. Uncalled nested helpers remain assertion-free; fault-injection
callbacks and "must not raise" contracts remain executable assertions. Exceptions
in dormant callbacks count only behind a recognized callback/fault-injection edge,
while a top-level subject call still proves a no-raise contract without importing
an unused closure's assertion. Alternatives rejected: blanket root-name exemptions
hide real source reads, and rewriting tests only to satisfy the census does not
improve the tested behavior.

Problem: media fault-injection tests read the shared observer TypeScript as
source text even though their real assertions execute under jsdom, and moving
the re-exec marker to its lifecycle owner changed a legitimate architecture
anchor's identity. Decision: prepare mutated observer sources through the
existing compiler/harness boundary and retain every executable failure probe;
refresh the source-read census for the removed read and the new marker owner.
Alternatives rejected: retaining a redundant Python source read adds
test-health debt without behavior coverage, and raising the census would hide
future regressions. Verify all six media mutation cases and the complete
suite-health ratchet.

Problem: a release test sliced shell text by log-message anchors, so the test
depended on source layout. Decision: `scripts/release_open_gates.sh` is the
executable lint/collection gate called by `release_open.sh` and exercised with
controlled tool exits and an explicit candidate directory. Alternatives
rejected: checking command strings cannot prove failure propagation, and
duplicating the gate in a fixture can diverge from the release path.

Focused tool fixtures retain real admission before the handler under test.
Fixtures with a conversation and project path supply an owner-checked project
binding through the repository read port; an ordinary conversation can declare
an unbound result. Missing fixture storage is not evidence of ordinary project
authority. Program-execution tests explicitly select
`tools.programmaticCalling=on` when their subject requires JavaScript; default
and disabled-policy cases remain separate assertions. These are required
fixture inputs, not reasons to weaken production admission or change defaults.

Final-wire lifecycle fixtures cover default batching and explicit JavaScript
separately. Problem: an old `_programmatic_tier=program` hint was treated as
opt-in evidence. Decision: exercise `auto` and `on` through real projection while
retaining canonical controls, revocation and hidden-count assertions. Reinstating
hint authority or weakening those assertions would hide a policy regression.

1. Reproduce and run the smallest test at the owning boundary.
2. Run neighboring contract tests when data crosses a module/process boundary.
3. Regenerate and check artifacts when a source contract changes.
4. Run the domain gate after focused tests pass.
5. Run broad gates once the worktree is stable.

`make test-affected` is a transparent selection hint for the inner loop. It
does not replace domain or release gates. Do not rerun an unchanged suite.

Replay drift and script exhaustion are local validation failures. The replayer
raises an assertion through the existing local request-preparation boundary, so
real dispatch releases its slot without retry, credential cooldown or model
fallback. Problem: treating stale fixtures as provider failures obscured the
first mismatch behind repeated backoff. Decision: preserve strict digest checks
and repair reviewed fixture recordings. Weakening normalization or consuming
provider retry budgets cannot repair a local recording.

Each recorded scenario owns one generated fixture. Problem: a derived
retry-exhaustion test appended its borrowed prefix to the canonical recording.
Decision: root-loop variants declare `record_fixture=False`; they still execute
the real loop against the seed during authoring and strict replay otherwise,
without writing the owning scenario's generation. Alternatives rejected:
trimming appended rows after recording hides ownership mistakes; skipping the
variant loses its lifecycle checks. Review generation lengths, provider events
and results as well as request digests before promoting `.next` files.

## Development and delivery loop

**Problem:** written policies and numerous tests do not prove the changed user
journey works. Directory migrations can silently break test discovery; local
and CI lists can diverge; results from a changing shared checkout do not certify
an integrated candidate.

**Decision:** use the existing owners in a short, evidence-producing loop:

1. State the user-visible acceptance case, owning contract and smallest
   reproducer before a substantial change. For a bug, demonstrate the reproducer
   failing for the intended reason; environment/bootstrap errors are not that proof.
2. Develop one coherent change in an isolated worktree. Preserve unrelated edits.
   Keep runtime data and test processes isolated as required below.
3. Run the iteration ladder. For orchestration/tool/streaming changes, exercise
   the real loop via the existing record/replay seam; for UI changes, verify the
   browser journey. A mock-only unit result does not certify either boundary.
4. Review the diff and integrate the verified worktree immediately. Run the
   relevant gate against the integrated candidate, recording the revision,
   command, result and remaining coverage gaps. A dirty shared tree result must
   be labelled as such; do not reuse it as clean release evidence.
5. Deliver the change with that evidence. A reproduced product failure becomes
   an owning regression case (or extends an existing one), not another permanent
   incident-specific rule in the root agent instructions.

**Alternatives considered:** adding instructions alone leaves enforcement gaps;
more mocks cannot prove the real application boundaries; running the entire suite
on every edit slows feedback without making the tested revision stable. Retain
fast local feedback and separate candidate/release validation instead.

The affected-test selector enumerates tracked and non-ignored untracked Python
suites through Git recursively, resolves mandatory guards to their current paths,
and refuses missing guards or failed/empty discovery. Removed tests cannot remain
selected from cache. NUL-delimited Git output preserves names and both endpoints
of renames; nested fixtures select their subtree. Cache replacement is atomic and
advisory. The execution tier consistently selects offline unit/API cases and
excludes visual, slow and live-model markers, even when selection is broadened.
Static imports/literal references are incomplete for dynamic and transitive
behavior: the selector remains an inner-loop hint, never a publication gate.
Use maintained domain/release commands in project gate configuration; the
[configuration example](../.env.example) names the repository entrypoints.

The browser CI lane invokes `make test-e2e`, so local and CI share the journey
list. `make ci` alone is the backend/architecture subset; frontend, browser,
native/platform and supply-chain gates still belong to their CI lanes.

The primary workflow's terminal `CI required` job depends on every lane and runs
with `always()`. Only explicit success passes; malformed/empty results,
cancellation, failure and unexpected skips fail. Nightly lanes may be skipped
only on non-scheduled events. PR jobs retain the default synthetic merge-candidate
checkout. `tests/meta/test_ci_results.py` validates dependency coverage and
executes the workflow gate with injected failure/skip outcomes.

Repository branch protection must require `CI required` for this to block remote
merges; committing the workflow does not configure or prove that hosted setting.
Separate native/release workflows keep their own required checks and admission
contracts; this primary-workflow summary does not certify those workflows.

These practices adapt public Codex guidance on
[isolated, repeatable worktree environments](https://learn.chatgpt.com/docs/environments/local-environment),
[explicit completion checks and feedback](https://developers.openai.com/blog/run-long-horizon-tasks-with-codex),
and [trace-to-evaluation improvement](https://developers.openai.com/cookbook/examples/agents_sdk/agent_improvement_loop).
They explain the adopted mechanisms, not a measured claim about relative defect
rates or OpenAI's private release process.

## What a durable test asserts

- user-visible outcome or stable domain invariant;
- owner identity and authorization;
- typed failure and actionable recovery;
- transaction rollback and atomic event capture;
- command idempotency and lost-ack behavior;
- cancellation and resource disposal;
- bounded payload or work where growth is a risk.

Prefer public functions, repository protocols, generated DTOs, and real module
graphs. Avoid private source positions, exact comment text, copied
implementations, magic output counts, and compatibility behavior already
removed.

The AST census must fail when Git cannot enumerate tracked files; an empty
result from a failed subprocess is never a clean audit. Event vocabulary scans
read retained sources directly, without compiling browser test adapters.
The native-fetch ownership guard reads those authored sections and parses
TypeScript through its AST. A typed `fetch` port or receiver-qualified call is
not a browser-global fetch; injected test graphs are not additional transport
owners. Global `fetch(url)` and `window.fetch(url)` remain violations outside
the transport and locale owners, with executable positive and negative controls.
The syntax smoke census includes tracked and non-ignored untracked Python
files through NUL-delimited Git enumeration, preserving its scratch-directory
exclusions and skipping deleted paths. It compiles source bytes in memory:
recursive scratch traversal and per-file bytecode writes can exhaust the
five-minute guard on FUSE without finding a syntax defect. Git failure and
unreadable source fail the check; neither becomes an empty successful scan.
Minimal Quart test apps install the production HTTP error handlers so parser
failures exercise the same structured 400 boundary as the running app.

When a source-level architecture ratchet is necessary, assert a durable
boundary (for example, “routes contain no SQL” or “generated output is
current”), not a historical incident ID. A ratchet without a behavioral or
ownership explanation should be deleted.

**Long-function ratchet.** Problem: on 2026-10-27
`scripts/check_function_sizes.py` held exact budgets for six named functions
while 27 other Python functions of 600 or more lines had none, so they could
grow unnoticed (`_handle_project_tool` went from 824 to 841 lines in five
days). Decision: the checker discovers every function of at least
`FUNCTION_LINE_THRESHOLD` (600) lines and requires an exact budget for each.
It audits the worktree, never `HEAD`, through the syntax census's Git
enumeration (tracked plus untracked non-ignored files; deleted paths are
skipped and a Git failure fails the audit), without the census's
scratch-directory exclusions. A function's size runs from its `def` line to
the last line of its body, decorators excluded; methods and nested functions
are named through their enclosing definitions (`Class.method`), and a
redefined name counts its longest copy.

The exact-budget rule: each `FUNCTION_LINE_BUDGETS` entry is a
`(path, dotted name, lines)` triple whose `lines` equals the function's
current size, and it changes in the same commit as the function.

- A grown function fails: split it back to at most its budget. A budget is
  never raised.
- A shrunk function fails until its budget is lowered to the new size. Below
  the threshold the entry may be deleted instead; a kept entry locks in the
  earned shrinkage.
- A new function of 600 or more lines fails until it is split. A moved or
  renamed function takes its budget to the new path or name.
- A repeated `(path, name)` is refused at import: two concurrent edits that
  budget one function merge cleanly, and a dict literal would silently keep
  the later value.

`python3 scripts/check_function_sizes.py` prints each violation with its
remedy, and `tests/meta/test_function_size_ratchet.py` runs the same audit on
the repository and on a synthetic Git tree. The audit keeps a fixed number of
reads in flight and parses only files of at least 600 lines. Alternatives
considered: scanning only `lib/` missed long functions in `packages/`,
`plugins/`, `scripts/` and `tests/`; a threshold without per-function budgets
would let earned shrinkage be spent again; serial reads took about 80 s on a
cold page cache on a busy host, against about 10 s with concurrent reads.

The exact rule costs concurrent branches. Every change to a budgeted
function's length also edits this registry, so branches that change one
function, or two functions with adjacent entries, conflict in Git; the later
branch merges `HEAD` and takes the number from the checker. A change that
lands without its budget, including a merge, turns the gate red on `HEAD` for
every session until the budget is corrected. Budgets as ceilings, where
shrinkage passes, avoid both costs but let the earned lines be spent again
unseen, which is the growth this ratchet exists to stop.

**Python typing ratchet.** `python3 scripts/check_typing_baseline.py` runs
pyright over the migration-owned include scopes and compares diagnostics with
`tests/typing_baseline.json`; counts may only shrink, and an improvement must
be locked in deliberately. The six files selected for strict checking declare
`# pyright: strict` locally. `typeCheckingMode` is not valid inside a pyright
`executionEnvironments` entry: putting it there makes the analyzer discard
configuration diagnostics and can hide import-resolution failures behind
unrelated missing-import reports. Scope attribution is first-match in
`scripts/check_typing_baseline.py`, so each diagnostic has one owner.

The valid configuration reveals existing strict-mode diagnostics; it is not
permission to regenerate the baseline upward. Fix those diagnostics or record
the gate failure, and run `tests/meta/test_typing_baseline.py` after changing
scope membership. This preserves the intended strict boundary instead of
silently reverting it to basic checking.

## Frontend policy

Frontend tests exercise the Vite/ESM sources and generated delivery graph.
They may use a real browser or an isolated module harness. Tests that rebuild
deleted classic file graphs, concatenate independently bundled registries, or
publish private owners onto `window` do not describe the shipped application.

Raw classic-section fixtures inject only runtime imports that the section
actually references. Manifest bindings marked `type` are compile-time-only and
must never become browser globals; `tests/meta/test_runtime_import_ports.py`
exercises that branch directly.

Build toolchain decision: CI previously selected Node 20 while locked Vitest
required Node 22.12 or a supported newer major. `.node-version` now owns the
Node line used by repository dependency-install jobs. `package.json` declares
accepted versions, its lockfile metadata must agree, and `.npmrc` enables
[npm engine enforcement](https://docs.npmjs.com/cli/v10/using-npm/config/#engine-strict)
before an incompatible install can appear successful. The
[setup-node version-file input](https://github.com/actions/setup-node/blob/v4/docs/advanced-usage.md#node-version-file)
keeps CI and release builds on that same selection. Alternatives rejected:
independent workflow literals drift; suppressing engine errors leaves the
declared build environment unverified. `tests/meta/test_frontend_node_toolchain.py`
checks selection, enforcement and package/lock parity; strict `npm ci` and
`npm run check:frontend` exercise the selected runtime and locked dependencies.

Node specs (`*.test.cjs`) run through `npm run check:node-tests`, part of
`check:frontend`. Problem: neither pytest nor Vitest collects them, and 24 of
52 specs were named by no script; one had kept a controller port that no
longer existed and another hid a real disclosure focus regression. Decision:
the gate owns specs by directory glob, and
`tests/meta/test_node_specs_run_in_frontend_gate.py` rejects a tracked spec
outside those globs. Alternatives rejected: one npm script per spec is the
registration step that already rotted; migrating every harness to Vitest is a
large mechanical rewrite of working specs with no behavioral gain.

Co-located TypeScript specs run through `npm run test:unit`, also owned by
`check:frontend`. Problem: the local gate previously ran only the rich-response
subset, while CI added a separate full Vitest command. Decision: local and CI
verification use the same complete owner suite; CI does not repeat it. The
focused `check:rich-response` command retains its generator and feature checks,
sharing `check:rich-response-contract` with the full gate. A second CI-only
command was rejected because it leaves local verification incomplete. The
frontend gate specification above guards both unit entrypoints.

Worker startup errors are failed verification even when completed tests pass.
On a slow shared filesystem, use an isolated local worktree with its own
`npm ci` installation and run the same gate, recording the verified commit and
dependency lock. A dependency symlink back to the shared mount does not isolate
module loading; copied dependency trees can also contain links that escape the
worktree. Keep the default concurrency, isolation and watchdogs. Do not replace
shared dependencies during concurrent work or treat a partial test count as a
complete result.

Run these before the frontend test lane:

```bash
npm run check:runtime
npm run check:styles
npm run check:conversation-sync
npm run typecheck:modules
```

## Hermeticity and markers

- `unit`: no external service and no real model.
- `api`: local test client plus controlled storage/LLM seams.
- `visual`: real browser; external services remain stubbed unless explicitly
  marked otherwise.
- `slow`: expensive but deterministic local work.
- `live_llm`: opt-in only and never a release prerequisite.

## LLM record/replay

`tests/support/llm_replay/` replays recorded or hand-authored model streams
through the REAL agent loop, tool pipeline, and storage, replacing only the
three transport functions (`lib.llm.stream_chat` / `async_stream_chat` /
`chat`). Scenario fixtures live in `tests/fixtures/llm_replay/<scenario>/`
and replay keyless in the ordinary tiers, closing the gap between scripted
mocks and `live_llm`: prompt and loop changes are validated against real
callback sequences without an API key. Requests match by normalized digest
(cwd, home, host OS release, dates, and internal fields tokenized), so a
driver drift fails loud as "re-record the scenario" rather than silently
passing. Recording requires `TOFU_LLM_REPLAY=record` plus `TOFU_PARITY_LIVE=1`
and writes `script.next.jsonl` — committed fixtures are never overwritten;
fix fixtures, not normalizers. `tests/AGENTS.md` owns the contributor policy
and the exact re-record command.

### Reviewable replay drift

**Problem:** scripts stored digests only, so a drift reported just "first
differing top-level field: 'tools'". Explaining it meant rebuilding the
recording commit, and a re-record reviewed as opaque hash changes, so an
intended wire change and a regression looked identical. A request's
environment section also embedded the host kernel release, so fixtures
recorded on a Linux 5.10 host could never replay on a 4.18 host.

**Decision:** the recorder stores the normalized value behind every field
digest in one content-addressed store (`tests/fixtures/llm_replay/requests/`,
verified by digest on load, shared by all scenarios). On a mismatch the
replayer diffs recorded against current values and reports the first
difference's JSON path and values; named list elements (tools) are paired by
name so one inserted tool is one difference. Matching is unchanged: wholesale
digest equality in strict order. The host OS name and release are host facts,
normalized like the checkout path, home directory, and date.

**Alternatives rejected:** inline requests in `script.jsonl` (repeats the
~50 KB tool catalog per step as one-line JSON nobody can review); per-scenario
copies (several times the bytes for identical catalogs); per-subtree digest
trees (they locate a path but cannot show the recorded value); fuzzy or
partial matching (it would stop guarding unintended wire changes); pinning
`platform.release` in each scenario driver (every future driver would have
to remember it, while the normalizer already owns host facts).

### Scenario timeouts expire by scenario, not by wall clock

**Problem:** `parallel_tools_timeout` decided which `read_files` call exceeded
its declared timeout by racing a 1 s sleep against a real 200 ms timer, and the
same 200 ms budget applied to the fast and follow-up reads. On a loaded host an
ordinary read took longer than 200 ms, its body became the timeout sentinel,
and the next request drifted at `$.messages[6].content`: the gate failed
intermittently without any product change.

**Decision:** the scenario owns timer expiry. It gives the pool wrapper a
`threading.Timer` that is never scheduled; the slow read expires its own timer
while in flight, and production code cancels the others as usual. The other
reads sleep past the nominal interval, so a return to wall-clock expiry fails
every run instead of intermittently. The declared-timeout unit specs
(`tests/tools/test_tool_declared_timeout.py`) follow the same rule: they raced
100 ms and 50 ms timers against 400 ms and 200 ms sleeps (and a 500 ms timer
against a 50 ms handler); each call now expires its own never-scheduled timer
in flight or outlasts it, and the specs assert the armed interval, the
cooperative cancellation request and the settled call's timer cancellation.

**Alternatives rejected:** rerunning or retrying the scenario (hides the race);
longer timeouts and sleeps (narrow the race, slow the gate, still depend on the
host); arming the timeout per call ID (the registry keys declared timeouts by
tool name, and the slow side would still race the timer thread); normalizing
the timeout sentinel out of tool messages (stops guarding real wire changes).

### Tests never encode host speed

**Problem:** the loaded full unit gate failed tests that pass alone. A writer
thread that waited at most 5 s for a snapshot's fenced read gave up while the
snapshot's optimistic git captures were still running, so the snapshot saw no
writer. An event-batcher spec asserted a 300 ms persistence lag and counted
transactions that depended on 200 threads reaching a 20 ms coalescing window.
Helper deadlines of 60 s on a jsdom node child and on the engine deployment
script turned slow but healthy runs into `TimeoutExpired`, and three
suite-health views each rescanned every test file (about 130 s per scan).
Later scans failed three more on unchanged code: a Codex app-server phase-hang
spec shrank the exchange budget to 300 ms, which also bounded spawn and every
earlier reply; a worktree-process helper ran each child under a 0.4 s idle
window that interpreter start-up alone exceeds on a loaded host; an MCP
admission-wait spec released its lease holder on a 0.3 s timer that could fire
before the call reached the lease.

**Decision:** coordinated threads release each other on events, never on a
shorter timer than the work they wait for. Unit specs state performance as
deterministic counters: the batcher spec holds the first transaction open
until all 200 streams queue, then requires at most two transactions; latency
budgets such as `event_persist_window_le_300ms` stay with certification
(`scripts/storage_certify.py`). The per-test hang guard (`timeout` in
`pyproject.toml`, signal method) owns wall-clock limits for children a test
starts: an interrupted `subprocess.run` kills its child. Helpers add no tighter
deadline; explicit deadlines remain where no hang guard runs (collection-time
compilation) or where the deadline is the behavior under test. Expensive
read-only results such as the suite-health census are computed once per module.
When the code under test owns the timer, the spec keeps its budget and
injects that module's clock instead: the clock stands still until the scenario
reaches the state under test, then advances (the synthetic app-server has
received the hung request; the materialization loop has observed an accepted
progress frame, and only then is the child released to print the next one).
A thread that must be waited on is released by the waiter's observed state,
here its refused admission probe, never by a timer. Helpers run children under
the product budget unless a test times the stall itself. A wall-clock bound on
teardown becomes a liveness proof: the escaped pipe holder is still running
when teardown returns.

**Alternatives rejected:** longer helper deadlines (still a host-speed bet that
fails on the next slower host); `serial` or retries (hide the race and slow the
gate); moving the latency assertion behind a benchmark marker (certification
already owns it, and a duplicate would still be timed on a shared host);
scaling budgets by a measured host speed (encodes the host by another name);
replacing `time` process-wide (it would also stall unrelated threads and the
hang guard).

Tests own temporary storage and processes and clean them up. Network, clock,
randomness, provider, and browser dependencies are injected or bounded.
At pytest bootstrap, inherited production lifecycle identity, manager endpoints,
ports, storage credentials, and project paths are removed before the first
project import. Lifecycle fault-injection copies additionally prove their
private pytest root, project/data containment, dynamically owned port, and exact
child-process identity before signalling; a test flag by itself never
authorizes a production lifecycle operation.
Detached lifecycle fixtures declare the pytest worker as their explicit
Supervisor owner, so an interrupted worker cannot strand a self-healing
watchdog or test server under PID 1.

### Inherited Git selection environment

**Problem:** every model command the Tofu server runs carries the automatic
Git maintenance policy `AUTOMATIC_GIT_MAINTENANCE_OFF` in
`GIT_CONFIG_PARAMETERS` (`lib/git_runtime.py`; before 2026-10-27 only
lease-holding commands carried `'gc.autoDetach=false'
'maintenance.autoDetach=false'`). Git admission treats any configuration
injection as a possible helper and widens every claim, so a pytest launched
from such a command failed about 80 lease-precision specs in
`tests/project_mod` that pass from any other shell. The failures moved with
the launch command, not the test order (2026-10-27: the same 562 specs, 36
failed with the value, 0 without). The production server process carries no
such value.

**Decision:** `tests/conftest.py` removes Git's repository-local environment
(`git rev-parse --local-env-vars`), `GIT_CONFIG_GLOBAL`, `GIT_CONFIG_SYSTEM`,
`GIT_CONFIG_KEY_n`/`GIT_CONFIG_VALUE_n` and `GIT_CEILING_DIRECTORIES` before the
first project import, beside the lifecycle scrub. A test that needs one of them
sets it with `monkeypatch` or an explicit child environment.
`tests/server/test_pytest_storage_isolation.py` specifies the set.

**Alternatives rejected:** narrowing Git admission to accept the injected values
(a product precision change in another owner's admission code, and it leaves
hook-exported `GIT_DIR`/`GIT_INDEX_FILE` leaking); per-file `delenv` fixtures
(some files already had them; every new Git spec would have to remember);
reordering or serializing the lane (the order was never the cause).

### Host scratch in isolated worktrees

**Problem:** `host_tmp_path` took its root from the host-scratch policy, whose
default is the checkout's `.eval-runs`. The policy rightly refuses anything
below shared temporary storage (`docs/ROOTLESS_VM_SANDBOX.md`), and isolated
worktrees live under `/tmp`, so all 19 guarded evaluation specs errored at setup
in every worktree.

**Decision:** the policy stays unchanged; the fixture assumption was wrong.
Without `ROOTLESS_VM_TMPDIR`, `TOFU_EVAL_ROOT` or the local-root opt-in, and
with a checkout the policy refuses as host storage, the fixture acts as the
operator. It sets `ROOTLESS_VM_TMPDIR` to a per-test private root under
`~/.cache/tofu-pytest-host-scratch`, which guarded code then resolves as well,
and removes it after the test. Its names carry the creating PID, so crashed
owners are reclaimed like the other pytest roots. Checkouts on valid host
storage, such as the shared DolphinFS checkout, keep the checkout default.
The guarded suites in `tests/evaluations`, `tests/rootless_vm` and their
neighbours (about 400 specs) now run in worktrees too. A sandbox spec may not
treat a HOME prefix on its own scratch as host-home exposure: scratch also sits
below HOME under the policy's local-root opt-in.

**Alternatives rejected:** exempting pytest paths from the `/tmp` rule (weakens
the policy the specs exist to guard); skipping the specs in worktrees (they
would never run before an integration); placing scratch in the main checkout
or below `XDG_CACHE_HOME` (both can be a shared checkout tree).

### Live-app fixture prerequisites

**Problem:** the shared `page` fixture needs a launchable browser, the built
Vite graph and a startable storage authority, but only the browser was
checked, so the interpreter decided which gap a run reported. A Playwright
whose Chromium revision is not installed skipped at `browser`. With a
launchable browser, `flask_app` started the backend inherited from the
deployment environment; a native engine built from other source (any
worktree, or engine edits not yet redeployed) raised `EnginePreparationError`,
and every browser and API test errored at setup. With SQLite but no
`static/vite`, the index answered 503 and each test waited out its own
selector timeout. Per-file `[tofudb]` parameters, the native semantic
authority and child processes start their own authorities, so after
`flask_app` was fixed a worktree still reported about 450 setup errors;
browser tests that request `browser` and `live_server` without `page` still
timed out, and suites that bundle with Vite or transpile TypeScript failed on a
bundler exit in checkouts without `node_modules`. Suites that read the
checkout's graph directly (its manifest and emitted assets) still failed with
`ViteAssetError` or `FileNotFoundError`: 23 tests in 14 files, among them
`tests/i18n/test_i18n_pack_emission.py`, plus three Vite-config or test-bundler
tests on a missing `vite` package.

**Decision:** each prerequisite is an explicit capability check with an
actionable reason, applied where the capability is consumed. The engine's
typed refusal (`EnginePreparationError`) becomes a skip that names
`TOFU_TEST_TOFUDB_BIN` and the SQLite lane wherever a native authority
starts: `flask_app`, any fixture or test body (the `pytest_runtest_setup` and
`pytest_runtest_call` wrappers in `tests/conftest.py`), the native
`semantic_authority` (checked with `verify_current` before launch) and
child-process tests (`require_native_engine_for_checkout()` before they
spawn). A lane that selected `TOFU_TEST_TOFUDB_BIN` keeps every refusal fatal,
and no other exception is reclassified, so an owner that refuses a mismatched
engine raises `EnginePreparationError` itself, private benchmark authorities
such as the native HTML corpus (`benchmarks/knowledge_html/durable.py`)
included; a bare `RuntimeError` bypasses the wrappers and fails every worktree.
Binary discovery uses the same typed refusal for an absent executable or an
invalid explicit binary path. The selected native backend still refuses to
start; only the existing test prerequisite boundary classifies the failure.
`tests/conftest.py` verifies the selected artifact once per process; native
`semantic_authority` pins its recorded `TOFUDB_EXPECTED_SHA256` instead of
repeating the source fingerprint and `--build-info` probe for each test.
A test that drives the live app in a browser (it requests `browser` and
`live_server`, directly or through `page`) calls `require_shipped_graph()`
before its server and browser start, so `TOFU_REQUIRE_FRONTEND=1` lanes still
fail on a missing graph. Suites that run repository `node_modules` (the Vite
test bundler or preview server, TypeScript) call `require_node_modules()`,
which names the missing module and `npm ci`, with the packages the script
imports; a `shutil.which('node')` or script-exists check cannot see a missing
`node_modules`. A test that reads the checkout's graph directly calls
`require_shipped_graph()` before the read, inside the reading helper when tests
share one, so source-only tests in the same module still run. A static scan in
`tests/meta/test_browser_page_prerequisites.py` fails on a test that reads the
graph by name (`VITE_MANIFEST`, `VITE_OUT_DIR`, `validate_vite_artifact`,
`validate_published_vite_artifact`, `ROOT / 'static/vite…'`) without checking
first, running through `page` or the live-app browser hook, installing a
synthetic graph, or asserting the graph's presence itself (an explicit
full-service lane). A read wrapped in its own fallback handler is optional. A
consumer that only reaches the graph over HTTP, such as `GET /`, still needs
its explicit call; the scan cannot see it.

**node_modules census.** Problem: about 250 tests ran repository
`node_modules` (Vite test bundler, jsdom harnesses, TypeScript, esbuild,
postcss) with no prerequisite guard, or hand-skipped with
`skipif(not shutil.which('node'))`-style probes that stay silent under
`TOFU_REQUIRE_FRONTEND=1`; a worktree without `npm ci` produced bundler and
`require` failures instead of the remedy. Guards that looked present were
often conditional: `runtime_section_path()` bundles, and so guards, only for
some sections, and a `run_harness()` after the bundler call came too late.
Decision: the same meta module runs an interprocedural census. A test needs
node_modules when a reachable call (local helper, fixture, `tests` helper
module) launches a process that may be `node` and names node_modules code: a
repository JS entry point whose import closure loads a lockfile package, an
inline `require`/`import` of one, or a `node_modules/<package>` path. Such a
test must reach `require_node_modules(<modules>)` (or
`require_node_test_runner(<modules>)`, `_require_frontend_bundler()`,
`run_harness()`, `frontend_module_guard(need_jsdom=True)`, `skip_or_fail`)
on calls that certainly run before the first node_modules launch (fixtures,
then the body in evaluation order; a helper that certainly guards covers its
own launches), and it must not keep a hand-written dependency skip. A
module-scope guard counts when it certainly runs at import. The
synthetic-tree control in the same file pins each rule. Alternatives
rejected: a file-level text scan cannot tell a guard behind a branch from a
real one; treating every reachable guard as protective missed tests that
failed without `node_modules`; a pytest hook that converts bundler errors into
skips would also hide real bundler regressions; `require_node_modules()`
without module names proves only that `node` exists and does not count.
Policy specs that only need an engine to exist admit a synthetic one
instead of the host's artifact (`tests/storage/test_transport_resolution.py`).
`tests/server/test_native_engine_isolated.py` and
`tests/meta/test_browser_page_prerequisites.py` specify the hooks.

**Alternatives rejected:** pinning the shared authority to SQLite would
silently replace the configured backend; building or redeploying an engine
from pytest crosses the deployment owner; skipping on any startup error would
hide real authority defects; converting the refusal in each fixture by hand
misses the next native fixture, as the per-file parameters did; linking or
installing `node_modules` from pytest crosses the toolchain owner. Turning a
`ViteAssetError` into a skip in a conftest hook would also skip a
synthetic-graph regression in any checkout without a build; a module-level
skip would also skip the source-only tests beside the readers.

### First-party test source authority

**Problem:** checkout-only `PYTHONPATH` does not isolate first-party imports:
editable-install finders can still load a private checkout, while a shipped but
uninstalled SDK leaves mandatory test modules uncollectable.

**Decision:** after isolating writable state and before project imports,
`tests/conftest.py` binds exactly `tofu_search` to `packages/tofu-search`,
`tofu_trading` to `plugins/tofu-trading`, and `tofu_environments` to
`packages/tofu-environments` in the current checkout. This narrow test-only
finder precedes editable finders and confines each package and its children to
its declared source tree. Missing/malformed member directories or initializers,
escaping import origins, and already-imported foreign modules fail explicitly;
a missing local child cannot be supplied by an installed copy. It adds no
canonical/sibling path, scans no additional members, and leaves third-party
imports, production discovery, dependencies and plugin enablement unchanged.

**Alternatives rejected:** adding member paths alone cannot override every
editable finder; skipping mandatory imports hides release defects; changing
production/package dependencies would widen optional capability contracts.
`tests/meta/test_first_party_test_sources.py` runs the real bootstrap against
synthetic checkouts and a poisoned editable finder. Qualify mandatory collection
and first-party import origins from the exported checkout with `PYTHONPATH`
restricted to that checkout; source-tree results do not certify sanitized exports
or the host's native-library/toolchain compatibility.

## Parallel execution policy

`pyproject.toml` is the single owner of pytest's `worksteal` distribution
mode. A bare `python -m pytest` stays serial; the Make, CI, and affected-test
entry points opt into xdist with `-n`. Tests must therefore be correct when
collected in the same order but executed in any order on any worker.

`-n auto` is resource-aware rather than raw host-CPU fan-out. The xdist hook in
`tests/conftest.py` reuses the runtime affinity/cgroup CPU and live-memory probe,
caps the zero-configuration result at four workers, and avoids launching more
workers than explicitly selected test files. This preserves headroom for the
OS, browser, and a running personal Tofu instance. `JOBS=N` is the explicit
dedicated-host override; `JOBS=0` is the debugging/serial override.

Every worker owns separate data, storage, log, and temporary roots. Tests may
not mutate shipped source or use a fixed host-global port as an implicit lock.
CSS cascade negative controls pass in-memory stylesheet variants to the same
resolver as their baseline. `tests/meta/test_css_negative_controls.py` forbids
file/subprocess I/O while executing the recent-search and memory-modal controls.
Disposable pytest roots live under one current-UID temp parent and encode their
creating PID; normal teardown removes the current roots, while the next session
performs one bounded, exact-name reclaim of roots whose owner is dead. A root
that can still hold processes or host resources (a private PostgreSQL cluster)
is first retired by its owning module, which may keep it for a later pass.
Translation refusal markers and virtual Vite stdin entries stay inside these
declared lifecycles rather than accumulating across the host temp directory.
`loadscope`, `loadfile`, and `loadgroup` are optimization tools, not correctness
barriers: grouping one file does not prevent another worker from touching the
same external resource.

Process-wide sweeps follow the same rule. The stuck-task reaper scans the whole
chat registry, which in a worker also holds tasks that earlier tests left
running; a reaper spec that asserts a count or verdict points the reaper at a
registry holding only its own tasks
(`tests/support/chat_tasks.py::isolated_reaper_registry`). Alternatives
rejected: asserting only on the test's own task drops the count contract;
fixing each leaking suite leaves the sweep spec to fail on the next leak; a
per-test purge of the global registry would also remove records that
module-scoped fixtures share.

A process-wide switch that a product entrypoint sets for its own process is
per-test state in the suite. **Problem:** `tofu-agent run` sets
`TOFU_DISABLE_CONFIGURED_SLOTS=1` through `os.environ` for its one-shot
process. The CLI contract tests (`tests/tofu_agent/test_tofu_agent_run_cli.py`,
`tests/tofu_agent/test_native_cli.py`,
`tests/agent_core/test_harness_terminal_contract.py`) run it in-process. Without
a per-test boundary the switch stays on for their worker, and any later test
there that dispatches through the configured v2 pool (the LLM replay
scenarios) builds zero slots and fails `no_slot`, but only when work stealing
puts both on one worker.
**Decision:** the autouse `_isolate_configured_slot_switch` fixture in
`tests/conftest.py` starts every test with the switch unset. It records the
pre-test state through `monkeypatch` before deleting the variable, so undo
restores it even after the CLI's direct write. `delenv` alone records nothing
for an absent name.
`tests/meta/test_configured_slot_switch_isolation.py` runs the CLI contract
module and then an upstream replay scenario in one serial process.
**Alternatives rejected:** a fixture in each in-process CLI caller repeats the
boundary and misses the next caller. Clearing the switch only in the replay
harness fixes one consumer while the polluters keep leaking to every other
configured-pool test. Restoring all of `os.environ` after every test is a
broader behavior change than this defect justifies; it would first need an
inventory of the variables fixtures and lazy imports publish during a test.
Removing the CLI's process default would change the headless contract that
`test_run_disables_configured_slots_by_default` specifies.

A test also owns the process state that code under test writes on its
behalf. **Problem:** a probe of the unit lane found more tests that left
state for the next test on their worker: the explicit `TOFU_NUMERIC_THREADS`
spec exported four numeric-library ceilings through `os.environ` (its
`monkeypatch.delenv` recorded nothing for the absent names), the ephemeral
pre-flight rejection spec left `TOFU_EPHEMERAL_PREFLIGHT_TIMEOUT=1`, and the
child route-ownership spec left the child's conversation affinity bound on the
test thread, which failed a later research-worker spec that requires none.
Ephemeral mints also left proxy exemptions behind; that leak was the
product's and is fixed at its owner (`docs/modules/llm_io.md`, request-scoped
proxy exemptions). **Decision:** the test records the prior state, absence
included, before the code under test writes it (`monkeypatch.setenv` before
`delenv`, or saved values that `tearDown` restores) and pairs a direct routing
binding with the lane teardown (`clear_pinned_provider`,
`clear_conv_affinity`). `tests/meta/test_process_state_isolation.py` reruns
the known polluters in one serial session under
`tests/support/process_state_probe.py`, which records the environment, the
proxy bypass registry and the thread's provider pin and conversation affinity
before each test's setup and after its teardown, and requires them unchanged.
Modules whose import publishes process defaults by design (`server`) are
imported before the first case. **Alternatives rejected:** an autouse fixture
that restores every environment variable or routing binding (it would also
undo lazy-import publication and would hide the next polluter instead of
naming it); resets in each victim (the polluters keep leaking to every other
consumer).

The `serial` marker is a narrow unit-test exception for a host-global resource,
an intentional saturation test, or a timing contract that cannot safely
overlap the parallel unit lane. Each new use carries an inline resource reason.
`test-unit` runs those cases in a separate no-xdist phase; isolation work
should remove the marker rather than broaden the exception.

### Source-built test engines

**Problem:** the differential and context-read suites cargo-build the tofu-db
engine on first use (`tests/support/tofudb_binary.py`). A cold build outlasts
pytest-timeout's 300 s per-test hang guard, so the first dependent test and
every test queued behind the build lock errored (14 TofuDB context-read cases
in one run); the interrupted resolution then made later callers in the process
silently skip, and a real compile failure was rebuilt and re-reported by each
dependent test. **Decision:** `ensure_engine_binary` runs the build with the
caller's ITIMER_REAL deadline paused and re-arms the remaining time whatever
the outcome; the build keeps its own `_CARGO_BUILD_TIMEOUT_SECONDS` deadline.
A failed build is remembered for the process and raised to every later caller;
an interrupted build is retried. Tests that reset the resolver state also
reset `_FAILURE`. **Alternatives rejected:** a larger per-test timeout for
engine tests (a tuning knob that still loses to a slower host or a cold
cache); building at collection end (blocks every xdist worker, including those
that never run an engine test); declaring an engine marker on each dynamic
user (`getfixturevalue`, helper calls), which every new caller must remember.
With `--timeout-method=thread` the deadline cannot be paused and the old
behaviour remains. Specification: `tests/meta/test_tofudb_engine_preparation.py`.

### Per-test and collection overhead

Fixed costs multiply by ~50k tests and by every xdist worker, so they are
budgeted like product latency. Measure with `--durations=40` on the gate and
`make test-collect` timing before and after a fixture or harness change.

- The autouse NC-source belt (`restore_drifted_nc_sources`) trusts a clean
  read only while the file's stat signature (inode, size, mtime, ctime) is
  unchanged and the read happened more than 2 s after both its mtime and ctime
  (racy-git rule for whole-second FUSE timestamps; ctime also defeats an
  `os.utime` rewind). Any write re-arms a full content compare;
  `tests/meta/test_nc_guard_belt.py` pins both paths. Cost fell from ~17 ms to
  ~0.1 ms per test.
- Legacy jsdom modules compile native TypeScript owners at import time.
  `tests/_runtime_sections.py::_run_test_bundler` keeps a cross-process cache
  (default `$TMPDIR/tofu-test-bundle-cache-<uid>`, 7-day eviction) keyed by
  bundler arguments, checkout root, the Python harness adapter, bundler script,
  dependency lockfile, tsconfigs and the `frontend/src` file list; a hit additionally requires
  every file Rollup read (`vite_test_bundle.mjs --inputs-manifest`) to hash
  identically and every directory holding one to list identically (a new
  sibling can change resolution). A build is recorded only when all its inputs
  were last modified more than 2 s before it started, and digests are memoized
  by stat signature under the same rule. Outputs naming the per-session view
  directory are never cached. `TOFU_TEST_BUNDLE_CACHE=off` disables it; a path relocates it.
  Cold compile ~0.6-2 s per owner, warm hit ~0.05 s. Rejected alternatives:
  hashing the whole frontend tree (any edit invalidates every bundle and it
  still misses inputs outside the tree) and mtime-only validation (unsafe on
  whole-second mtimes).
- Durable seeding shares the writer's group commit. **Problem:**
  `tests/swarm/test_retained_messages_storage.py` seeds 1,101 retained
  identities through `RetainedAgentRepository.create`, two durable commits
  each. Created one at a time, the ~2,200 fsyncs took 84 s at load ~20, 275 s
  at load ~150 and exceeded the 300 s hang guard in the `-n 4` lane, while the
  paged roster read under test takes 0.03 s. **Decision:** the spec creates
  the identities from a 32-thread pool so the SQLite writer commits each
  queued batch with one fsync (7-10 s); the real create path and every
  assertion are unchanged, and a roster read that stops after the first page
  still fails (`1000 == 1101`). **Rejected:** a per-test `timeout` marker
  (raises the budget and stays host-bound), fewer identities or a patched
  `_ROSTER_PAGE_ROWS` (no longer crosses the real 1,000-row page), and writing
  rows below the repository (skips the create path the roster joins).

## Flakes and pre-existing failures

A failing test has one of three states: introduced by the change, reproducible
before the change, or caused by concurrent worktree changes. Record the exact
command and evidence. Fix or remove a flaky test promptly; retries and ignored
failures are not correctness.

Broad results in a concurrently edited worktree are a moving target. Report the
tested revision/state and never claim an unrelated failure was fixed.

## Supply-chain release gate

CI generates a CycloneDX document for the checked-out dependency graph and for
each API/worker release candidate. It then fails on HIGH or CRITICAL Python, npm,
or operating-system vulnerabilities, checked-in secrets, and deployment
misconfiguration. The scanner action is commit-pinned and its binary version is
fixed in `.github/workflows/ci.yml`; changing either is a reviewed dependency
update. Local data, logs, generated Vite assets, dependency directories, and
the nested `codex/` checkout are excluded because they are not release inputs.

The developer-runtime tag workflow repeats the source/secret gates, builds and
scans the public rootless `agent` target, and publishes both repository and
agent CycloneDX documents with the release. Package and image publish jobs all
depend on that tag-local gate; a green branch run is not treated as release
evidence.

An exception is never implemented with `continue-on-error` or a blanket scan
disable. Record an exact advisory/rule waiver with an expiry and removal owner,
then keep SBOM upload enabled so the accepted release remains auditable.

## Adding or changing tests

1. Put the test beside the owning domain and choose the narrowest marker.
2. Demonstrate that it fails when the intended invariant is removed.
3. Assert outcome and error semantics, not implementation narration.
4. Reuse shared fixtures for auth, storage, browser, and model behavior.
5. Bound spawned processes and waits without racing the host: synchronize on
   events, leave wall-clock limits to the per-test hang guard, and clean up
   ("Tests never encode host speed").
6. Delete tests whose protected surface is deleted.

Test-suite health is checked mechanically by `scripts/audit_tests.py` and
`tests/meta/test_suite_health_ratchet.py`; the ratchet covers executable quality
signals, not incident metadata. The census parses every tracked test file, so
it is CPU-bound: each file's AST walk is materialized once and reused by every
check, scope provenance walks statement lists only, and tracked paths are
answered from one `git ls-files` instead of one `git check-ignore` each. The
ratchet module shares one census among its views.

**Census lifetime.** Problem: the runnable-tool, category-regression and loose-
baseline assertions each repeated the same full test-tree census. Decision:
these read-only consumers share one lazy successful census through a module-
scoped pytest fixture. Missing-file and baseline-read checks still precede the
census; failures propagate and are retried rather than cached. A new fixture
invocation reads the tree afresh. Alternatives: a persistent process cache could
reuse an earlier tree, while removing assertions would weaken the ratchet. The
fixture changes neither scan scope nor baseline, deadline or assertion policy.

**Source-independent behavior checks.** Problem: moving a runtime interface
made a source-string hydration check fail while snapshot adoption still worked;
parsing the first CSS declaration also misses later cascade overrides. Decision:
`test_turn_native_settings_hydrate.py` executes the exported runtime with the
actual settings adopter, checking full versus partial updates, optional artifact
metadata and the single-snapshot opening path. The visual
`test_visual_tofu_paper_comfort.py` uses the shared isolated browser origin to
measure the composed palette, every gradient stop and text contrast; page-local
overrides prove that the checks reject regressions. Alternatives: following
private declarations to new files or pinning CSS spelling would preserve the
same refactoring debt. G baseline reductions require comparing test/source
identities with the exact historical census, not merely a smaller total.


## Research and background-delivery regression

Search/fetch/harness quality gates assert useful evidence, not HTTP-200 or tool
success counts. Offline failure-injected tests cover engines answering a different
query, unavailable/empty engines, site-scope violations, uncertain multilingual
snippets, omitted structured HTML, facts beyond preview windows, unsupported
quotes and positive findings with unresolved fields. Required-child tests cover
parent-first settlement, inbox-before-wake, optional background work, duplicate
completion, foreign ownership, cancellation and post-commit recovery.

Use `packages/tofu-search/tests/test_observation_quality.py`,
`tests/tasks_pkg/test_fetch_url_extract.py`, and
`tests/swarm/test_swarm_turn_handoff.py` first; then package/MCP, full-source,
continuation-identity, failure-continuation and restart neighbors. These synthetic
contracts measure deterministic correctness and bounds; they do not claim live
web recall, model entailment accuracy or end-user research completion. Live
quality evaluation must separately measure supported discoveries, false negative
claims, omitted discoveries, dependency integration, latency and cost per verified
result, with a fixed query/source set and explicit partial coverage.

## Rust-primary performance admission

Rust-primary migration applies to the complete application execution path.
Native implementation alone grants no production promotion: preparation,
provider routing, accounting, tools, final hooks, durable recovery and delivery
must keep their existing domain authorities and failure behavior. Performance
priority follows measured CPU/latency hotspots, including allocation, copying,
serialization and cross-language calls; external waits need separate attribution.
The [system gate](../benchmarks/system_performance.py) requires all 30 declared
integration cases, raw independent-process comparisons and per-metric guards.
Each case also supplies cold_start first-operation wall/CPU clocks from at least
100 fresh processes per variant, with distinct process identities across cases
and variants. Missing cold evidence is incomplete; warmed clocks cannot certify
activation, and a cold P50/P95/P99 or CPU regression fails the same 5% guards.
Runtime parity comes first, then storage/index, context/events/HTTP and optional
ingest. Browser code and plugin adapters retain their deployment boundaries until
an equivalent replacement has its own integration evidence. Component wins or
language choice never certify complete migration or universal optimality.


Conversation renderer identity and failed-Turn class checks execute the real
bundled Surface. Problem: source anchors treated a module extraction as lost
behavior. Decision: public renderer journeys assert keyed node reuse, optimistic
identity handoff and the assistant/human failure-class matrix. Rejected: moving
source needles to another file preserves the same fragile mechanism. The Surface
GC child watchdog includes both bundling and DOM setup (90 seconds); unchanged
reachability and render-count assertions provide the measured specification,
not the child's wall-clock duration under the parallel Node runner.


### Browser onboarding fixture ownership

Problem: the common dismissal helper treated 700 ms without an overlay as proof
that startup onboarding had finished. A delayed model-catalog response could
open the wizard afterwards and intercept unrelated pointer tests. Decision:
wait for and invoke the registered native onboarding close action first, which
records the same durable dismissal as the user control. Keep the existing
Settings close action and settle loop; missing ownership or persistent overlays
fail at setup. The deterministic browser specification invokes the actual
onboarding owner after the helper returns, without racing a timer.
Alternatives rejected: longer sleeps still race network completion; forced clicks
hide real overlays; setting private flags at each test duplicates setup policy.
Spec: `tests/visual/test_visual_onboarding_dismissal.py`; the shipped scroll-button
journeys exercise the full registry and bootstrap at 390 and 1180 pixels.

## Environment adapter inventory

Problem: literal environment keys passed through local parsing helpers were
missing from the configuration registry even though direct reads were covered.
Decision: the registry scanner follows unchanged parameters through local
helper calls, including keyword arguments and forwarding, to the existing
environment-reader boundary. Reassigned parameters and nested scopes provide
no such proof. Previously hidden keys are documented in the same registry;
this adds no runtime setting or default. Alternatives rejected: a list of
helper spellings misses renamed/new adapters, while counting every TOFU string
misclassifies messages and ordinary data. Verify `test_config_knobs.py`.

## Retained section import discovery

Problem: raw-section fixtures scanned each full section separately for every
possible prelude binding. Decision: index reference and declaration names once
per section, retaining the exact lexical exclusions for properties, object keys
and declared bindings. The same 161 sections (2,483,968 UTF-8 bytes) took
33.20 seconds before and 0.136 seconds after; every selected binding matched
the previous implementation. Alternatives rejected: a time-based cache hides
source changes; omitting import owners changes fixture behavior. Verify
`test_runtime_import_ports.py` and retained runtime composition/behavior tests.
