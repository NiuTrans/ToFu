# Testing strategy

Tests protect product behavior and architecture ownership. They are not an
archive of incidents, implementation line numbers, or past migrations.

## Test layers

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

Unit and API suites are the main diagnostic layers. Browser journeys cover
cross-boundary user outcomes that cannot be proven cheaply below the browser;
they are deliberately few and high value.

## Iteration ladder

Problem: pytest consumes imported fixtures through discovery, without ordinary
Python reads of those names. Decision: fixtures reused from another test module use
an explicit local export (`from ... import fixture as fixture`), visible to both
pytest and lint. Alternatives rejected: removing the import loses the fixture;
blanket unused/redefinition suppressions also hide accidental duplicate imports.
Verify fixture providers and collected test identities when changing these exports.

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

## Frontend policy

Frontend tests exercise the Vite/ESM sources and generated delivery graph.
They may use a real browser or an isolated module harness. Tests that rebuild
deleted classic file graphs, concatenate independently bundled registries, or
publish private owners onto `window` do not describe the shipped application.

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
(cwd, dates, and internal fields tokenized), so a driver drift fails loud as
"re-record the scenario" rather than silently passing. Recording requires
`TOFU_LLM_REPLAY=record` plus `TOFU_PARITY_LIVE=1` and writes
`script.next.jsonl` — committed fixtures are never overwritten; fix
fixtures, not normalizers. `tests/AGENTS.md` owns the contributor policy.

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
Disposable pytest roots live under one current-UID temp parent and encode their
creating PID; normal teardown removes the current roots, while the next session
performs one bounded, exact-name reclaim of roots whose owner is dead. Translation
refusal markers and virtual Vite stdin entries stay inside these declared
lifecycles rather than accumulating across the host temp directory.
`loadscope`, `loadfile`, and `loadgroup` are optimization tools, not correctness
barriers: grouping one file does not prevent another worker from touching the
same external resource.

The `serial` marker is a narrow unit-test exception for a host-global resource,
an intentional saturation test, or a timing contract that cannot safely
overlap the parallel unit lane. Each new use carries an inline resource reason.
`test-unit` runs those cases in a separate no-xdist phase; isolation work
should remove the marker rather than broaden the exception.

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
5. Give spawned processes and waits explicit timeouts and cleanup.
6. Delete tests whose protected surface is deleted.

Test-suite health is checked mechanically by `scripts/audit_tests.py` and
`tests/meta/test_suite_health_ratchet.py`; the ratchet covers executable quality
signals, not incident metadata.

Open G-ratchet follow-up: the recorded source-read identities predate module
relocations and the new `test_frontend_tofu_paper_comfort.py` CSS-text guard.
First move that guard's chroma, contrast and flat-ground requirements to actual
computed-style checks, preserving its mutation controls. Then reconcile the
renamed test/source pairs and retired installer/style guards before lowering
the baseline. A smaller total alone does not prove that no new dependency was
introduced; keep the identity gate failing until that comparison is complete.


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
