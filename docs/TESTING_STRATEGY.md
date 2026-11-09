# Testing strategy

Tests protect product behavior and architecture ownership. They are not an
archive of incidents, implementation line numbers, or past migrations.

Backend-specific restart specifications select their backend through indirect
fixture parametrization. This avoids constructing SQL authorities for native-only
cases that immediately skip, and lets complete-file verification retain the
actual coverage. Shared lease/fencing behavior still runs on every authority.

The context injection writer audit resolves aliases within their lexical scope.
Problem: a file-wide name table attributed one function's local prompt to an
unrelated function's parameter. Decision: function arguments shadow outer names,
and assignments belong to their declaring scope. An allowlist for those false
positives would hide future real writes; negative fixtures cover both genuine
aliased writes and unrelated functions with the same variable name.

Observation wrappers around production recorders must return their receipts.
System-note injection uses the recorder's block identity and rolls back when
recording fails; collecting call arguments must preserve that boundary.

Problem: lazy imports retained patched dispatch factories and attempt readers
after fixture teardown, so real Worker resume tests failed only after unit tests.
Decision: load command and recovery consumers before replacing their source factories, and
patch the review consumer's reader without replacing its source export. The
factory-identity spec in `test_coordinator_dispatch_tools.py` and ordered runs of
`test_project_dispatch_runtime.py` with `test_worker_attempt_real_start.py` cover
restoration. Alternatives rejected: forcing isolated test processes hides the
leak; reloading production modules during teardown can invalidate other aliases.

Problem: the production shutdown wiring test closed the process-global event
delivery coordinator, so a later real claimed-attempt test failed only in a
shared pytest process. Decision: isolate the event-delivery shutdown dependency
alongside the other runtime owners and assert its drain order, worker thread,
and timeout argument. The ordered claimed-attempt → shutdown → claimed-attempt
reproduction exercises fixture restoration. Automatically reopening a closed
production coordinator would weaken shutdown ownership and hide the fixture leak.

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

## Synchronous test-client interruption

**Problem:** `pytest-timeout` raises its failure from a one-shot SIGALRM.
Asyncio can retain that failure in a child Task or report it from a callback
while the synchronous Quart test client continues awaiting its response.
A configured deadline alone therefore does not prove that this bridge exits.

**Decision:** the private-loop owner in `tests/conftest.py` delegates to the
already installed signal handler, retains its original exception outside Task
state, and cancels the awaited request. Its final close guard covers the entire
bridge delegation, including the prelude; handler installation belongs inside
the request cleanup and handler restoration boundary. It joins cancellation before
propagating the original failure. A recorded alarm while awaiting the request
settles pending tasks, async generators and the default executor, even when
application code suppresses cancellation and returns a value or raises another
error. Direct calls, including calls from a worker thread, retain their original
close-only boundary for every non-alarm outcome: values, ordinary exceptions,
explicit cancellation, `KeyboardInterrupt` and `SystemExit`. Accepted application
background work remains outside that request's teardown, including when its
Task factory fails during setup. The existing helper entry used from a running
loop delegates its lifecycle to the original `asyncio.run`: policy registration,
context propagation and complete settlement remain standard-library owned.
Its coroutine only registers the running loop and current Task for caller
interruption. Completed root exceptions are observed before the
loop closes; the final alarm decision follows both that observation and signal
handler restoration, so an alarm received during restoration cannot be lost.
Without a recorded alarm, cleanup failures retain the original `finally`
precedence; an exception already handled by the caller is not a request failure.
An alarm received during the close-only handoff still propagates after close
without reclaiming accepted application background work. During full settlement,
signal failures are retained without cancelling the physical join. A deadline
raised during cleanup remains a deadline failure, even when another application
exception preceded cleanup. A caller interrupted while using the existing
helper thread waits for its cleanup receipt and thread exit. Ordinary values,
application exceptions and cancellation retain their meaning; non-main threads
do not install signal handlers. The existing timer is never replaced or extended.

**Alternatives rejected:** an asyncio exception handler alone cannot observe a
still-referenced failed child Task; a larger deadline, another timer or changing
all pytest jobs to thread timeouts changes the testing contract without fixing
exception ownership. Boundary specifications in
`tests/conversation_sync/test_sync_client_event_loop_bridge.py` inject the signal
at the root, callback, child and cleanup boundaries, keep failure probes finite,
and cover ordinary and interrupted resource settlement. The existing hang-guard
suite separately verifies the pytest plugin and Makefile entrypoint.

## Test layers

The billing payment tie-order differential case seeds its two private
authorities before serving: the SQL schema owner initializes the temporary
database, and the native offline migration restore writes the same documents
and indexes. Each payment has `created_at=100`, inserted in `b, a, c` order;
the real list operations must return `c, b, a` and the offset-one, limit-two
page `b, a`, with timestamps included in the comparison. Calling
`billing.payment.record` cannot establish a shared clock across processes;
other billing scripts continue to exercise that write operation.

Single and multiplexed conversation replay tests share
`tests/support/conversation_replay.py`. Its repository publishes
owner/conversation-scoped broker invalidations after advancing scripted state.
Quiet stream
heartbeats carry liveness without re-reading storage, so a finite script must
announce its next page or cursor expiry to finish the real stream. Ownership
probes leave the script untouched; empty pages still announce its next state.
Catch-up specs cover quiet and caught-up transitions between pages with
`hasMore=False`; the multiplex suite directly consumes replay, reset and close
frames and checks subscription cleanup and notification scope. Restoring
heartbeat polling or forcing `hasMore` would
hide a stale fixture contract and weaken coverage of the production wait path.

Conversation navigation browser checks follow the current reader controls:
settled tool panels open at the newest calls and page backward through their
in-card earlier boundary. Command output uses the focusable output chip; its
native disclosure summary is retained for semantics and is not the keyboard
entry point. The harness asserts focus before activation so a hidden target
cannot leave Enter on the previously focused panel toggle and collapse it.
`test_visual_conversation_navigation_performance.py` keeps the single-read,
late-response, exact-output and cancellation assertions through these controls.

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
the owner's process group from SIGKILLing it. Explicit restart sends SIGINT and
waits for the durable shutdown. Final disposal sends SIGQUIT to the postmaster:
PostgreSQL retires its children and releases IPC without checkpointing files
that the fixture will delete. It still waits for exit without its own deadline
(the hang guard owns wall-clock limits), and never SIGKILLs the postmaster.
The root is removed only after that exit; an interrupted wait
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

**Startup readiness:** the existing pytest/harness test deadline owns the wait,
and a postmaster that exits still fails immediately. Problem: an additional
20 s fixture deadline repeatedly interrupted live PostgreSQL processes still
reporting startup in progress, including during serial checks, and forced reruns.
Decision: remove that redundant deadline while retaining the
readiness probe, process-exit check and cleanup barrier. A larger nested deadline
was rejected because the next slower healthy startup would fail again. The slow
live-start regression is covered by `tests/meta/test_postgres_bootstrap_reuse.py`.

Startup reclamation imports the stdlib-only `tests/support/postgres_cleanup.py`
owner. The cluster fixture re-exports its retirement entry point for existing
callers. Problem: importing the complete fixture from root conftest made every
cluster-construction change a transitive input to all tests. Decision: separate
the unchanged cleanup protocol from construction and clock fixtures. With
`pytest_plugins` edges included, the same frozen 4,540-file inventory selects
102 files for a cluster-helper change, down from 4,540, including nested plugin
consumers and the core guards. This is a selection measurement, not a runtime
claim; changes to root conftest or shared cleanup still retain their full scope.
Reject ignoring plugin declarations or weakening the startup reclaim merely to
reduce the selected count. The fresh-process boundary is specified in
`tests/meta/test_postgres_bootstrap_reuse.py`.

**Repeated PostgreSQL verification work (2026-11-08).** A live global-network
integration selected 66,631 cases and had completed about 24% after 105 minutes.
Both the dependency-manifest change and the transitive shared initialization
closure selected the whole inventory; removing the manifest trigger alone did
not narrow it. Worker stacks showed repeated private-cluster preparation and
shutdown waits. Selection counts therefore did not establish task latency.

**Decision:** the PostgreSQL fixture initializes one pristine, unstarted data
directory per pytest worker, then copies it into each case's private cluster.
Data, roles, WAL, sockets, processes and restart hooks remain independently
owned. Executable identity or launch-environment changes require fresh initdb;
the template captures those inputs before initialization, so replacement during
the build cannot relabel old data with a new executable's identity. Partial
copies are removed without damaging the bootstrap. Semantic-authority
tests compile the unchanged default C clock once per worker; custom clock
sources retain their explicit compilation and injection path. Each server still
creates its own random clock epoch, including after restart.

Final disposal uses PostgreSQL's documented immediate-shutdown protocol only
after the case is finished and the cluster's files are destined for deletion.
Committed writes, fsync, full-page writes, explicit restart, owner-death binding
and exact IPC/root reclamation retain their existing checks. See
[PostgreSQL shutdown modes](https://www.postgresql.org/docs/current/server-shutdown.html).
The fixture waits for all owned server processes to exit; it does not report
completion early or leave a background cleanup queue.

**Measurement:** three alternating pairs over fresh private clusters, each
running the real sidecar schema initializer and committing it, changed median
final teardown from 17.345 s to 0.111 s and the complete cluster/schema/disposal
cycle from 18.986 s to 1.722 s. Every pair improved; the host was concurrently
running other global gates. A separate three-pair bootstrap comparison with
the same query changed the cluster-cycle median from 1.885 s to 1.078 s;
initdb itself took about 0.919 s versus 0.153 s to copy the pristine data.
These are component measurements, not a full-suite or live-conversation speedup.
The same 41-case serial neighbor command (clock reuse, native worker clock,
worker-lease authority and transaction locks) passed 40 cases and skipped one
before and after the disposal change: 495.72 s versus 97.17 s. The bootstrap
and clock-compilation reuse were already enabled in both runs, isolating the
disposal change; the variable host load prevents treating this as a universal
suite multiplier.
The bootstrap retains 40,603,336 bytes and the default clock 16,584 bytes per
worker on this PostgreSQL installation, for that pytest session only. Normal
teardown removes them; a dead owner's bootstrap uses the existing exact-root
reclaim. There is no cross-session artifact cache or history of old bootstraps.

**Alternatives rejected:** dropping PostgreSQL parity cases; sharing a mutable
database across cases; disabling WAL/fsync; SIGKILLing the postmaster; dropping
the process-exit barrier; treating the manifest alone as the full-scope cause.
Specs: `tests/meta/test_postgres_bootstrap_reuse.py`,
`tests/meta/test_postgres_clock_reuse.py`, the existing cluster lifetime suite,
and the native-clock/worker-lease/transaction-lock neighbors.

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

Problem: repeated tool-budget assertions counted the internal canonical carrier,
including private evidence metadata, instead of the text delivered to the model.
Decision: budget tests count `model_text_from_tool_result` or the actual dispatched
model message, and verify canonical/recovery metadata separately. Alternatives
rejected: raising the limit conceals measurement error; shrinking production views
to satisfy an internal-carrier count discards usable evidence. The long-agent
aggregate source and failed-retention cases exercise this distinction.

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

The default development entrypoint is the shared incremental verification engine:

```sh
make test
make test-plan
make test TESTS="tests/project_mod/test_verification_defaults.py"
# Across committed branch changes and current working bytes:
.venv/bin/python3 scripts/verify.py run --changed --base main --json
```

`make test-affected` uses the same entrypoint. With no `TESTS` argument, the
snapshot diff, the existing static guards and recorded dynamic dependencies
determine the scope. No diff is reported as `unchanged`, not a test pass.
For explicit scopes the underlying command remains `scripts/verify.py run <paths>`.
`make test-full` aliases `test-all`; explicit domain and full qualification
commands continue to execute their entire declared scopes. The engine orchestrates
the Python development loop; pytest, Vitest and browser tools keep their own
execution responsibilities.

It tests an immutable snapshot including uncommitted/untracked source, reports
the tested tree and phase timings, and shares complete evidence with landing
verification (`record`/`check`).
Unchanged observed inputs can reuse a pass; failures, skips, interrupted runs,
changed environments and unobserved external inputs cannot masquerade as a cached
pass. Missing paths refuse the entire selection. This development command
does not claim to discover every test for a change or replace domain/release
gates. `landing_verification record` continues to force committed-revision
execution. Policy, cache lifetime and measurements:
[`project_integration.md`](modules/project_integration.md#incremental-verification).

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
   For task start, worker claim or dispatch admission changes, run
   `tests/conversation/test_worker_attempt_real_start.py`: a spec that replaces
   `start_conversation_attempt_executor` or `start_task` never reaches task
   registration or `bind_account`, which is how the 2026-11-06 identity outage
   shipped unnoticed.
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

**Frequent integration policy (owner decision 2026-11-08).** Ordinary PRs and
main/master pushes run affected Python tests through the shared verification
engine. Cold selection follows transitive Python and declared fixture dependencies
plus static guards;
matching complete observations can avoid repeat execution. Changes to actual
pytest execution, configuration or shared fixtures still widen the scope.
Nightly, manual and `v*` release-tag events run full unit/API/slow/coverage plus
visual/performance qualification. Missing base history also selects full
qualification. Existing frontend, browser, lint, architecture, package,
container and supply-chain checks keep their declared scopes. The workflow
captures the candidate SHA and uploads its incremental verification receipt.
Selection is not a proof about every possible dynamic input; full qualification
remains necessary before release. Local `make test-full` remains explicit.

The primary workflow's terminal `CI required` job depends on every lane and runs
with `always()`. Only explicit success passes; malformed/empty results,
cancellation, failure and unexpected skips fail. Only jobs inactive in the
declared incremental/full profile may be skipped; a missing profile fails.
PR jobs retain the default synthetic merge-candidate
checkout. `tests/meta/test_ci_results.py` validates dependency coverage and
executes the workflow gate with injected failure/skip outcomes;
`test_ci_verification_scope.py` covers event/history selection, and
`tests/project_mod/test_verification_ci.py` covers real candidate execution.

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

Generic native worker-queue tests use their own task kind. The reserved
`conversation-attempt` kind keeps queued cancellation claimable until its
domain handler settles the Turn (`worker_job_lifecycle_v1.json`); using that
kind in a transport-only fixture and expecting immediate cancellation tests
the wrong lifecycle. Verify
`worker_jobs_preserve_idempotency_priority_leases_fencing_and_cancellation`
alongside the conversation worker cancellation specifications.

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
Workspace registration and delivery-contention specs follow the same rule: their holders stay fenced until the owning test releases them in `finally`, and event/future rendezvous use the per-test hang guard. A 10-second pre-capture wait can expire during valid Git preparation, and a 15-second holder timer can silently remove the very contention being tested. Verify the registration/cancellation and source-writer cases in `tests/storage/test_integration_delivery.py`.
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

### Headless command and Goal fixture ordering

**Problem:** a subprocess sleep did not establish whether completion preceded
`poll`; searching the entire next request misclassified a retained pre-poll
original as duplicate delivery. An archive fixture also treated a sealed
outcome as physical worker exit, stopped storage under terminal delivery, and
used a 40-second observer window that included cold recovery. Unjoined stage
work could then resolve the following case's provider script.

**Decision:** the command fixture holds the actual shell on its own FIFO and
releases it at an explicit inbox-first or poll-first boundary. The spec requires
zero new completion carriers after poll while preserving earlier originals.
Goal fixtures borrow the existing explicit-executor port, own all stage Futures
including appended reviewers, and join before storage stop, assertions, provider
restoration and executor shutdown. Reconciliation must consume the exact
original receipt before one ordinary persistence acknowledgement closes its
intentional delivery debt. Failure cleanup uses owned cancellation and that
same acknowledgement, never flag deletion or global maintenance. One fresh
HTTP read follows physical completion; the existing per-test hang guard owns
liveness, not a shorter observer deadline.

**Alternatives rejected:** longer sleeps or deadlines preserve host races;
removing background history on poll or weakening originals acceptance changes
production semantics; resetting the dispatcher cannot join in-flight work.
Verify `tests/api/test_headless_no_saved_conversation.py` and
`tests/evolution/test_evolution_headless_runtime.py` in both completion orders
and together under the normal parallel gate.

### Watchdog backoff clock ownership

**Problem:** counting restart attempts in a real-time observation window lets
interpreter startup and host scheduling consume the test's clock. A fast-failing
fixture can then appear healthy or produce too few attempts to distinguish
escalating backoff from the deliberately fixed-delay neuter.

**Decision:** `tests/server/test_supervisor.py` runs the actual copied watchdog
with a subprocess-local `date`/`sleep` boundary. Time advances only after an
observed sleep request is acknowledged. The same logical observation window
asserts both requested delays and completed child launches; the neuter defeats
only escalation. Exact process-group retirement joins the owned leader and
asserts no surviving members or PID file. Production watchdog behavior is
unchanged. The shared disposable shell fixture also fences `systemctl` locally,
so status/stop tests cannot consult or mutate the caller's shared user unit.
A synthetic host executable proves that boundary without a caller-side stub;
these specifications do not claim real systemd integration.

**Alternatives rejected:** larger wall-clock windows, retries or serial-only
execution still depend on host speed; lowering launch-count bounds weakens the
load-bearing assertion; replacing process-wide time would affect unrelated
threads and the test hang guard.

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
typed refusal (`EngineAdmissionRefusedError`) becomes a skip that names
`TOFU_TEST_TOFUDB_BIN` and the SQLite lane wherever a native authority
starts: `flask_app`, any fixture or test body (the `pytest_runtest_setup` and
`pytest_runtest_call` wrappers in `tests/conftest.py`), the native
`semantic_authority` (checked with `verify_current` before launch) and
child-process tests (`require_native_engine_for_checkout()` before they
spawn). A lane that selected `TOFU_TEST_TOFUDB_BIN` keeps every refusal fatal,
and no other exception is reclassified, so an owner that refuses a mismatched
engine raises `EngineAdmissionRefusedError` itself, private benchmark authorities
such as the native HTML corpus (`benchmarks/knowledge_html/durable.py`)
included; a bare `RuntimeError` bypasses the wrappers and fails every worktree.
Binary discovery uses the same typed refusal for an absent executable or an
invalid explicit binary path. Timeouts and failures of preparation commands,
such as the execution service's generation plan, keep the base
`EnginePreparationError` and fail: they belong to the code under test, and
reporting them as a missing engine hid a failing qualification as a skip. The selected native backend still refuses to
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
its explicit call; the scan cannot see it. The plugin-page fixture and
application-shell render/cache probes call the same guard before constructing
their HTTP application. Their delivery assertions use the production graph
from `npm run build:frontend`; temporary runtime-section library bundles do
not provide that prerequisite. Behavioral specs exercise both the ordinary
missing-build skip and the required-frontend failure, while source-only
fragment tests remain runnable without a build.

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
`tests/conftest.py` reuses the runtime affinity/cgroup CPU and live-memory probe
(one effective CPU and 1 GiB of current memory headroom per worker), caps the
zero-configuration result at four workers, and avoids launching more workers
than explicitly selected test files. Every test process also runs at the lowest
CPU and I/O priority ("Test processes yield to the live server" below); the
ceiling stays because priority cannot order requests to a shared network volume
("The four-worker default stays"). `JOBS=N` is the explicit dedicated-host
override; `JOBS=0` is the debugging/serial override.

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

### Test processes yield to the live server

**Problem:** test runs on a host that also serves the live Tofu server competed
with it, its execution host and its TofuDB at equal CPU and I/O priority. On
2026-11-07 concurrent suites drove load to 142 on 70 CPUs, the live TofuDB held
an authority for 121.6 s and workers lost their leases. Raising the server's
priority needs privileges; lowering test processes does not.

**Decision (owner decision 2026-11-08):** every pytest process yields.
Importing `tests/conftest.py` first calls
`tests/support/process_priority.lower_own_priority()`, which moves every thread
of the calling process to nice 19 and the idle I/O class (Linux keeps both per
thread; other POSIX systems lower the process nice value only). It runs before
the conftest imports the application or probes an engine, and before xdist
starts workers, so the controller, its workers and everything tests start
(TofuDB, SQL Sidecar and PostgreSQL engines, Node, browsers, Git, cargo)
inherit it. Direct pytest, the Make targets, `scripts/verify.py` and
`landing_verification record`/`check` all load this conftest,
so no launcher wraps it; `bundles/yourprovider/tests/conftest.py`, outside the
testpaths, calls the same owner for `make test-bundles`. Package suites with
their own pytest configuration (`packages/*`, `plugins/*`, run by CI) do not
load either. It addresses only the calling process's own thread IDs
(`PRIO_PROCESS`, `IOPRIO_WHO_PROCESS`), never a process group, user or parent,
so a pytest started from the live server (an agent's `run_command`, the linear
checkpoint gate) leaves the server, its execution host and production TofuDB
unchanged. The launchers themselves keep their priority: the server calls the
verification library in-process (`lib/linear_git_checkpoint.py`).

Scope of the guarantee: nice orders CPU time only inside one CPU cgroup, and a
cgroup's CFS quota still throttles all of its members, niced or not, once the
quota is used. I/O priority binds only where the block scheduler honors it
(BFQ, CFQ; mq-deadline from Linux 5.14); it does not order requests to a FUSE
or network volume, whose daemon serves every client alike.

**Alternatives rejected:** per-launcher `nice`/`ionice` wrappers miss direct
pytest and every new launcher, and lowering a launcher would lower the server
when it calls one in-process. A `pytest_configure` hook runs after conftest
module work, which already starts engine probes at the launcher's priority
(observed with `TOFU_TEST_TOFUDB_BIN`). `PRIO_PGRP`, `PRIO_USER` or renicing
the parent reach the server whenever it launched the run. `SCHED_IDLE` ranks
below nice 19 but can starve test deadlines without bound and hides the nice
value from `ps`.

`tests/meta/test_harness_priority.py` specifies it: every own thread, including
one that predates the call, and later children reach nice 19 and the idle
class; the launching process and a child started before the call keep
best-effort level 4; CPU calls address only own thread IDs; and a nested `-n 1`
session started at best-effort level 4 runs its controller and worker idle.
Verify a real run with `ps -o pid,ni,cmd` and `ionice -p` on the controller,
its workers and a test-started engine.

### The four-worker default stays

**Problem:** once test processes yield CPU and I/O priority, the owner allowed
the four-worker `-n auto` default to rise if the live server stays responsive
while a niced run uses more workers. A whole-suite landing run takes about 12
hours at four workers on the development host (70 CPUs, 63-CPU cgroup quota).

**Decision:** keep the ceiling and the per-worker CPU and memory-headroom rule;
the measurement did not show a responsive live server at more workers. On
2026-11-08 the development host (63-CPU cgroup quota, live server on port
15000) ran niced `tests/api` sessions, with the source-matched TofuDB engine,
while `/api/health` was probed every 0.5 s and the live `logs/app.log` was read
for `storage.v2 slow command` warnings (2 s threshold):

| Window | Health p50 / p99 / max | Storage slow commands | Throttled periods | Container CPUs |
|---|---|---|---|---|
| Idle, 11 × 90 s | 3.8–4.1 / 36–580 / 42–1,842 ms | 0–9 per window, longest 21.5 s | 0–1 per window | 5.2–15.6 |
| 4 workers, 414 s and 542 s | 3.8–3.9 / 41–45 / 522–633 ms | 0 | 1 of 4,141; 0 of 5,416 | 8.0–8.9 |
| 16 workers, stopped at 37 s | 3.8 ms / one probe 4,809 ms | 2, up to 4.5 s | 0 of 371 | 6.3 |

Within the 16-worker session's first 37 s the live server logged `GET
/api/health 200 SLOW (4.808s)`, other requests at 2.9–3.2 s and storage
commands at 3.0–4.5 s in the same five seconds, a request stall longer than
any idle window showed. The measurement guard (any probe above 2 s) stopped
it, so 63 workers, the value without a ceiling here, were not run. CPU was not
contended (no throttled period), so priority could not have prevented the
stall. The cause was not isolated. The interpreter, `.venv`, shared checkout
and live TofuDB data share one beegfs FUSE volume whose daemon serves every
client alike, and more workers raise the rate of imports and subprocess starts
on it. Four workers stayed within the idle range.

**Alternatives rejected:** removing the ceiling (63 workers here) was not
shown safe; raising it to a constant such as 8 or 16 would be a host-tuned
knob without evidence (AGENTS.md principle 5); sizing workers from sampled
load or FUSE latency adds a sampling window and threshold, and CPU was not the
contended resource. A new measurement may reopen this once test processes stop
sharing that volume with the live server, for example with a local interpreter
and dependency environment.

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

- Runner contract tests use a private placeholder JS fixture, independent of
  the generated application view. **Problem:** importing `JS_DIR` just to
  supply an unused argv path made `test_jsdom_runner_structured.py` build the
  entire runtime during collection, including on each xdist worker.
  **Decision:** create that placeholder only for the six cases that invoke
  Node; preserve their real jsdom execution and exact/legacy assertion checks.
  A fresh-process collection specification refuses application materialization.
  **Measured (2026-11-07, same lane and probe):** collecting 31 cases took
  103.99 s through `pytest.main` before and 2.44 s after; test-bundler calls
  fell from 158 to zero. Pytest's collection-only clocks were 101.68/0.46 s;
  the 32 guard/regression cases passed in 9.23 s with frontend dependencies
  required. These are component observations under concurrent host load.
  **Alternatives rejected:** additional workers multiply this setup, and
  caching an unrelated application build still performs unnecessary work.
  Verify `tests/meta/test_verification_collection.py` and
  `tests/frontend/test_jsdom_runner_structured.py`.
- Benchmark acceptance scans current sources only for a supported provenance
  receipt (see `STORAGE_PERFORMANCE_CHARTER.md`, active conversation acceptance).
  Missing/unsupported receipts still fail qualification. The nine synthetic
  pipeline tests previously scanned the whole checkout although they supply
  no such receipt, adding unrelated dependencies to observed test impacts.
  With unchanged pipeline assertions and fresh serial pytest processes, the
  file took 19.75 s before and 0.36 s after on 2026-11-07. Source drift within
  the pipeline's own manifest is still exercised; supported receipts still
  capture and compare the complete current inventory on every assessment.
- Turn delta SQL tests own a fresh in-memory SQLite connection per case.
  **Problem:** `test_turns_delta_sync.py` committed every schema statement to
  a temporary disk file although its protocol, revision, cache-loss and
  ownership checks never reopen the database. **Decision:** run the complete
  current schema and unchanged operations/commit behavior in private memory;
  close the connection even when setup or a test fails. No mutable database
  is shared between cases. Reopen, process-recovery and durability tests keep
  their own disk-backed fixtures. **Measured (2026-11-07, concurrent host
  load):** three alternating file/memory probes initializing the same 76
  tables took 69.15-110.45 s on disk versus 0.0090-0.0097 s in memory, with
  identical seeded delta results. These are fixture timings, not full-suite
  latency. **Alternatives rejected:** fewer tables/assertions would weaken
  coverage, a shared mutable database would couple cases, and more workers
  would retain the redundant disk writes. Verify the entire
  `tests/storage_authority_sql/test_turns_delta_sync.py` file.
  Its session double carries the adapter's real `WorkerCommitHints`, and the
  compaction test checks the affected owner/conversation hint alongside the
  persisted change event. Omitting this session port raised `AttributeError`
  before the registered operation's change-event contract could be asserted.
- The autouse NC-source belt (`restore_drifted_nc_sources`) trusts a clean
  read only while the file's stat signature (inode, size, mtime, ctime) is
  unchanged and the read happened more than 2 s after both its mtime and ctime
  (racy-git rule for whole-second FUSE timestamps; ctime also defeats an
  `os.utime` rewind). Any write re-arms a full content compare;
  `tests/meta/test_nc_guard_belt.py` pins both paths. Cost fell from ~17 ms to
  ~0.1 ms per test.
- **Problem:** legacy jsdom modules compile native TypeScript owners at import
  time; collectors repeated work within and across independent checkouts.
  **Decision:** retain complete, content-validated build artifacts.
  `tests/_runtime_sections.py::_run_test_bundler` keeps a cross-process cache
  (default `$TMPDIR/tofu-test-bundle-cache-<uid>`, 7-day eviction) keyed by
  exact bundler arguments (only path arguments are relocated), the Python
  adapter, bundler script, Node/preload identities, public build environment,
  shared dependency-installation identity, lockfiles, package metadata,
  tsconfigs and the `frontend/src`
  file list. Portable actions share across checkouts; a hit additionally requires
  every file Rollup read (`vite_test_bundle.mjs --inputs-manifest`, including
  installed dependency modules) to hash identically and every input directory's
  membership and symlink targets to
  match (a new sibling or changed symlink can change resolution).
  A build is recorded only when all its inputs
  were last modified more than 2 s before it started, and digests are memoized
  by stat signature under the same rule. Outputs naming the per-session view
  directory are never cached. Generated input bytes are never rewritten for
  cache matching. Location-sensitive module syntax (`import.meta`, `__dirname`,
  `__filename`), unrecognized syntax and output naming the source root keep
  checkout-local reuse. Dotenv expansion binds the root and complete environment;
  arbitrary Node preloads disable cache admission. Existing
  `TOFU_TEST_BUNDLE_CACHE=off` disables it; a path relocates it.
  Native-module and import-port symbols derive from source-relative identity;
  embedding an absolute checkout path in their name would defeat reuse before
  validation. Generated entry source containing absolute imports still keeps
  those exact bytes, so such actions need not share across checkouts.
  **Complete artifacts:** v1 bundler receipts list all emitted JS, CSS and assets.
  Every artifact's hash and destination is checked before restoring anything.
  Public-directory copies outside Rollup's hooks mark the receipt incomplete,
  so those builds execute normally without entering the cache. This repairs
  the former JS-only cache, which could omit companion styles on a hit.
  **Concurrent collection:** collectors of the same action in the same checkout
  share a kernel-owned compile lease and revalidate output after acquisition.
  Different actions/checkouts never wait for this lease. Failed or interrupted
  producers leave no successful receipt; followers release the shared lease
  and retry independently. No service, polling timer, worker cap or retry knob
  is added. Lease directories share the existing 7-day eviction; pruning skips
  active leases and waiters verify inode identity after acquisition.
  Rejected alternatives:
  hashing the whole frontend tree (any edit invalidates every bundle and it
  still misses inputs outside the tree) and mtime-only validation (unsafe on
  whole-second mtimes), normalizing arbitrary generated source text (can change
  program meaning), and serializing independent checkouts behind one build.
  Verify `tests/meta/test_test_bundle_cache.py` and
  `tests/meta/test_verification_bundle_artifacts.py`; reproduce with
  `benchmarks/verification_bundle_reuse.py --implementation-root TREE --collectors N
  --output REPORT.json`. The probe freezes implementation bytes, checks complete
  artifact identity and executes the exported function in every collector view.
  **Measurement (2026-11-07, artifact implementation `ac54fa1d7`):** three
  alternating before/after pairs against `90e02d1f9`, six identical private
  checkouts, real Vite with a TS/CSS fixture.
  With one collector per checkout, builds fell from 6 to 1 and total preparation
  median from 3.9454 s to 0.9486 s (4.16×; ranges 3.5329–6.5037 s and
  0.7247–1.3815 s). With four collectors per checkout, builds fell from 24 to 1
  and median from 7.0426 s to 0.8581 s (8.21×; ranges 5.5132–7.1335 s and
  0.8154–1.4012 s). All twelve runs emitted identical complete artifact digests.
  Retained cache payload fell from 5,160 to 1,211 bytes; allocated cache storage
  fell from 49,152 to 12,288 bytes, including the one lease directory. This is
  preparation-component evidence, not a whole-suite or conversation speedup.
  Separate real-owner specifications execute `html-safety.ts` through both
  adapters in two checkouts and require exactly one build with identical output.
  **Generated feature entries (2026-11-07):** feature adapters previously wrote
  absolute checkout paths into their private entry sources; multi-owner graphs
  also put those paths into their exported bundle identifier. Identical owners
  therefore missed the complete-artifact cache across lanes. The adapter now
  authors its own entry imports through `#tofu-test-root/`, resolved by Vite to
  this invocation's root, and derives graph identifiers from logical owner paths.
  External paths remain exact. The cache still hashes every authored entry and
  resolved module; it does not rewrite application source or normalize arbitrary
  string literals. Unknown or location-sensitive inputs retain the same refusal.
  Rejected alternatives: replacing absolute strings while checking cache keys
  changes source meaning; storing a separate bundle per lane repeats compilation.
  Three alternating frozen-source pairs against `64676724b`, six fresh checkouts
  per arm, used the real `selection-focus.ts` owner and its orchestration registry.
  Single-owner builds fell 6→1, with preparation medians 3.567→0.667 s (ranges
  3.534–5.757 / 0.646–0.682); shared-graph builds also fell 6→1, 5.005→0.697 s
  (ranges 3.669–5.755 / 0.682–1.505). Every output executed the same focus and
  registry-identity checks. Single-owner bytes were identical in both arms;
  graph bytes now share one stable private identifier instead of one per root.
  Graph cache payload fell 19,866→3,311 bytes and allocated file storage
  49,152→8,192 bytes; single-owner storage stayed 3,018/8,192 bytes. These clocks
  cover preparation only. The real-owner specification changes a third checkout's
  implementation and requires a fresh build and changed behavior, preserving
  exact invalidation. Verify `test_verification_bundle_artifacts.py`, the cache
  fault suite and runtime-import-port contracts.
- **Problem:** `_section_import_ports` repeated the same eager-prelude import
  resolution for every retained section; a live verification-worker stack
  landed in `Path.resolve` while collecting the folder-longpress tests.
  **Decision:** resolve the common prelude once per collection's source snapshot,
  keyed by exact header text and resolution origin; section-specific lazy
  imports retain their original order and ownership. This follows the existing
  per-process manifest/section snapshot lifecycle; a new invocation loads its
  source tree afresh. Do not add global or persistent resolution caches.
  The 161 actual retained sections made 27,048 common resolution calls before,
  versus 168 after. Three alternating runs over the same source snapshot had
  medians 3.1724 s and 0.1331 s; complete returned binding/module/service tuples
  had the same digest in all six runs. The clock covers import-port discovery,
  not bundling or test execution. A tuple is reused rather than reparsed per
  section; changed header bytes or origin produce a new tuple. The rejected
  alternative, caching only by section name, still repeats all common work.
  Verify `tests/meta/test_runtime_import_ports.py`: aliases, type-only exclusion,
  lazy-before-eager ordering, service factories and changed text/origin.
  The same profile exposed a separate scope error in
  `test_frontend_folder_longpress.py`: two source views were obtained by
  materializing all 161 retained sections. The test now requests just
  `main/main_folders_mobile.js` and `mobile_panels.js` through the existing
  single-section adapter, keeping their real import ports, the native folder
  control owner and both negative controls. A fresh-process collection guard
  rejects accidental whole-graph requests; no helper API or test case is removed.
- The Request Inspector round-log and Markdown raw-HTML tests follow the same
  owner-scoped collection boundary. **Problem:** three cases requested the
  complete retained runtime merely to read `core/debug_panel.js` and
  `core/markdown.js`. **Decision:** request those two sections directly through
  `runtime_section_path`, preserving their real import ports and the separate
  typed `html-safety.ts` owner. The request-log retention negative control,
  source-integrity check, Markdown escaping/code/blockquote assertions and
  option-sensitive cache checks are unchanged. The collection guard rejects
  whole-graph requests and requires every original case to remain collected.
  Request-log execution explicitly checks its `jsdom` prerequisite before
  launching Node; it no longer borrows that guard from unrelated preparation.
  **Alternatives rejected:** more workers repeat the unrelated preparation;
  substituting a fake sanitizer or deleting negative controls weakens the
  executable contract. No public helper, compiler or application source changes.
  **Measured (2026-11-08):** three alternating pairs used the same checkout,
  frozen `b038949c7` helper, Vite script and dependency lock, a fresh process
  and separate empty artifact cache per arm. The after arm used the final
  `0e9c48374` consumer sources, including the explicit `jsdom` prerequisite.
  Only those two test modules' frozen source bytes differed; helper and toolchain
  fingerprints matched before and after all six arms.
  `pytest.main --collect-only --assert=plain -n 0` collected the same three
  complete node IDs in every arm; real bundler invocations fell from 159 to 5
  and unique retained sections from 161 to 2. Collection medians were
  151.01 / 11.15 s, with ranges 148.16–166.00 / 9.35–14.42 s during concurrent
  full-suite verification. The unchanged JavaScript harnesses
  and Python assertion ASTs were compared separately, and normal execution
  still exercised all original cases and negative control. These are cold
  collection-component clocks; gains from later shared-helper changes are
  not included in this comparison.
- **Problem:** the synthetic motion renderer still fetched GSAP during scene
  preparation. A cold verification checkout timed out against the CDN while a
  control checkout with cached assets passed, producing a false merge regression.
  **Decision:** `_fake_media` stages a scene-local runtime fixture alongside its
  existing synthetic render/probe/concat/mux ports. The browser never executes
  these fixture bytes; real composition, quality-axis propagation, film gates
  and publication assertions remain. `test_motion_video_runtime_assets.py`
  separately verifies pinned-byte validation, caching and staging. The quality
  propagation spec now starts with an empty runtime cache and rejects HTTP, so
  a missing fixture fails immediately instead of waiting on a download.
  The pinned GSAP bytes now also ship in `lib/motion_video/vendor/` (declared
  wheel package data) and `ensure_gsap` verifies them before use, so a cold
  checkout with an empty runtime cache stages the real runtime offline; the
  motion cache and the CDN remain fallbacks for older installed packages
  without the vendored file, and any byte substitution fails the pinned
  digest check rather than serving unverified JavaScript.
  Rejected alternative: warming a shared CDN cache hides the dependency and
  couples independent checkouts to network availability.
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
