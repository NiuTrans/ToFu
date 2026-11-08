# Test-suite guidance

## Scope and first read

Tests are executable product and architecture specifications. Read `README.md`
and `docs/TESTING_STRATEGY.md`; use the domain map to find the smallest owning
suite.

## Test design

- Assert public behavior or a durable boundary: identity/authorization, typed
  failure, idempotency, rollback, cancellation, cleanup, and resource bounds.
- Do not pin private function locations, exact comments, incidental ordering,
  magic source counts, generated bundles, or compatibility paths already
  removed.
- Unit tests have no network or real model. API tests use controlled storage and
  provider seams. Browser tests cover only outcomes that cheaper layers cannot.
- Tests must be hermetic and parallel-safe under xdist work stealing. Each test
  owns its database, directories, ports, environment, clock/random/provider
  seams, processes, and cleanup.
- Never mutate shipped source or rely on a fixed host-global resource as a lock.
  Fixtures are minimal, synthetic, non-secret, and live under `fixtures/` or
  `support/` only when shared.
- Use `live_llm`, `slow`, `visual`, and other markers honestly. Expensive or
  external tests stay opt-in and cannot be a hidden release prerequisite.

## Verification ladder

Run the changed test directly, then neighboring contract tests, then the domain
gate. Use `make suite-health` for collection/quality changes and broad
`make test-unit`, `make test-api`, `make test-frontend`, or `make test-all` only
after the worktree is stable.

- Under xdist (`-n`), one nonexistent path in an explicit file list makes the
  whole session collect 0 items ("no tests ran", exit 5) without an error.
  That is a broken argument list, not a pass: build changed-test lists with
  `git diff --name-only --diff-filter=d` and rerun one file serially with
  `--collect-only` to see the real error.
- Quart 0.22 test clients answer a view exception with a debug 500 page when
  `TESTING` is set. A test that expects the exception sets
  `PROPAGATE_EXCEPTIONS = True` and leaves `TESTING` unset.
- `tests/meta/test_function_size_ratchet.py` reports grown/shrunk entries
  before unbudgeted functions; `python3 scripts/check_function_sizes.py`
  prints the complete failure set.
- Smoke-boot a scratch server with `TOFU_RUN_SERVER=1`, a scratch
  `TOFU_DATA_DIR` and a verified free port. Without that flag
  `python3 server.py` hands startup to the shared manager on 127.0.0.1:15001
  (`bootstrap_pkg/server_entry.py`). Never run `./supervisor.sh stop` from a
  worktree: it also stops the shared systemd unit.

## LLM record/replay

`tests/support/llm_replay/` records real LLM streaming sessions to JSONL
fixtures (`tests/fixtures/llm_replay/<scenario>/script.jsonl`) and replays them
by substituting ONLY the three transport functions — `lib.llm.stream_chat`,
`lib.llm.astream.async_stream_chat`, `lib.llm.chat.chat` — while the real agent
loop, dispatch layer, tool pipeline, and persistence run
(`tests/llm/test_llm_replay_scenarios.py`). Mock at the expensive/nondeterministic
boundary; everything else stays real.

- Replay is the default in scenario tests: keyless, hermetic, xdist-safe.
  Request identity is a sha256 over normalized `{model, messages, system,
  tools}` (cwd/tmp → `{{CWD}}`, home → `{{HOME}}`, host OS name and release →
  `{{OS}}`, dates → `{{DATE}}`); calls match the script in strict recording
  order and drift/exhaustion raises `ScenarioDriftError`.
- The normalized request behind every recorded field digest lives in the
  shared content-addressed store `tests/fixtures/llm_replay/requests/`
  (`<field>-<digest prefix>.json`, verified by digest on load). A drift error
  names the JSON path and the recorded vs current values of the first
  difference; `tests/llm/test_llm_replay_unit.py::TestFixtureCorpus` requires
  every committed step to resolve and rejects unreferenced entries.
- Record requires `TOFU_LLM_REPLAY=record` AND `TOFU_PARITY_LIVE=1` plus a real
  provider key. It writes `script.next.jsonl` next to the fixture — never over
  a committed `script.jsonl`; a new generation is reviewed and renamed by hand.
  Offline re-generation of committed fixtures uses
  `TOFU_LLM_REPLAY_AUTHOR=1` (same `.next` output, no network; the committed
  script's events and results are the seed, so only digests change).
- Re-record after an intended request change, from the repository root of the
  merged tree:

  ```sh
  rm -f tests/fixtures/llm_replay/*/script.next.jsonl
  TOFU_LLM_REPLAY_AUTHOR=1 python3 -m pytest -q -p no:cacheprovider -n 6 \
    tests/llm/test_llm_replay_scenarios.py tests/llm/test_upstream_turn_replay.py
  for d in tests/fixtures/llm_replay/*/; do
    if [ -f "$d/script.next.jsonl" ]; then mv "$d/script.next.jsonl" "$d/script.jsonl"; fi
  done
  python3 -m pytest -q -p no:cacheprovider tests/llm/test_llm_replay_unit.py
  git diff -M tests/fixtures/llm_replay/requests
  ```

  The unit run names unreferenced store entries to delete; the final diff is
  the review: every changed request line must trace to an intended commit.
- **Fix fixtures, not normalizers.** The normalizer tokenizes host facts only
  (paths, home, host OS, dates). When a legitimate product change alters the
  request (new prompt section, new tool schema), re-record the scenario and
  review the request diff; never teach the normalizer a new special case to
  keep a stale fixture green.
- **Scenarios never race the wall clock.** When a timeout or delay decides
  request bytes, the scenario expires the production timer explicitly
  (`test_parallel_tools_timeout`); a sleep that outlasts a real timer flakes
  on a loaded host.
- Inject pre-chunk provider failures (429/5xx) with a `{"kind":"error"}`
  step; streamed content/tool events with `stream` steps; non-streaming
  completions with `chat` steps. Every scenario asserts the durable event
  log's invariants (`run_task_log_invariants`), not event-by-event equality —
  `expected.json` holds only robust minimal assertions (final content
  substrings, tool names, statuses).
- Add a new scenario when the behavior under test has a distinct provider
  wire shape (a new event sequence, failure class, or settlement pattern).
  Extend an existing fixture's steps when the scenario already covers the
  shape and only the prompt/assertions differ.
