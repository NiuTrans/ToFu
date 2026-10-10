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
  tools}` (cwd/tmp → `{{CWD}}`, home → `{{HOME}}`, dates → `{{DATE}}`); calls
  match the script in strict recording order and drift/exhaustion raises
  `ScenarioDriftError`.
- Record requires `TOFU_LLM_REPLAY=record` AND `TOFU_PARITY_LIVE=1` plus a real
  provider key. It writes `script.next.jsonl` next to the fixture — never over
  a committed `script.jsonl`; a new generation is reviewed and renamed by hand.
  Offline re-generation of committed fixtures uses
  `TOFU_LLM_REPLAY_AUTHOR=1` (same `.next` output, no network).
- **Fix fixtures, not normalizers.** When a legitimate product change alters
  the request (new prompt section, new tool schema), re-record the scenario
  and review the digest diff; never teach the normalizer a new special case
  to keep a stale fixture green.
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
