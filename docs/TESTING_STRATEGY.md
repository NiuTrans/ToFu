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

Unit and API suites are the main diagnostic layers. Browser journeys cover
cross-boundary user outcomes that cannot be proven cheaply below the browser;
they are deliberately few and high value.

## Iteration ladder

1. Reproduce and run the smallest test at the owning boundary.
2. Run neighboring contract tests when data crosses a module/process boundary.
3. Regenerate and check artifacts when a source contract changes.
4. Run the domain gate after focused tests pass.
5. Run broad gates once the worktree is stable.

`make test-affected` is a transparent selection hint for the inner loop. It
does not replace domain or release gates. Do not rerun an unchanged suite.

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

When a source-level architecture ratchet is necessary, assert a durable
boundary (for example, “routes contain no SQL” or “generated output is
current”), not a historical incident ID. A ratchet without a behavioral or
ownership explanation should be deleted.

## Frontend policy

Frontend tests exercise the Vite/ESM sources and generated delivery graph.
They may use a real browser or an isolated module harness. Tests that rebuild
deleted classic file graphs, concatenate independently bundled registries, or
publish private owners onto `window` do not describe the shipped application.

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
`tests/test_suite_health_ratchet.py`; the ratchet covers executable quality
signals, not incident metadata.
