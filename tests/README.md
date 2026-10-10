# Tests

## Directory structure

The suite is grouped into **domain subdirectories that mirror `lib/`**: the
tests for `lib/paper/` live in `tests/paper/`, the tests for
`lib/storage_sidecar/` live in `tests/storage_sidecar/`, and so on. A directory
listing of `tests/` is the domain map — find the module you are changing, then
open the same-named directory here.

```
tests/
├── conftest.py            — shared fixtures (app boot, mock LLM, Playwright, storage guard)
├── _*.py / _*.js          — shared harnesses (jsdom, replay, runtime sections); imported, never collected
├── fixtures/              — recorded LLM replay scripts and small synthetic inputs
├── support/               — shared assertion helpers (envelope, sidecar fixtures, …)
├── screenshots/           — local diagnostic output (gitignored)
├── agent_core/            — tests for lib/agent_core/
├── paper/                 — tests for lib/paper/
├── tasks_pkg/             — tests for lib/tasks_pkg/ (agent loop, finalize, tool pipeline)
├── frontend/              — jsdom/vite surface tests (no lib import; named test_frontend_*)
├── storage_sidecar/       — tests for lib/storage_sidecar/
├── tofudb/                — native TofuDB engine/differential/migration suites
├── meta/                  — the suite auditing itself (ratchets, architecture/import guards)
├── api/                   — HTTP wire-contract parity suites (test_api_*)
├── …                      — one directory per lib module or product area
└── mock_llm_server.py     — standalone mock LLM API server
```

Placement rule for new tests: put the file in the directory matching the
`lib.` module it imports most; when it exercises the browser surface or has no
code imports, use the product area (`frontend/`, `visual/`, `e2e/`, `meta/` for
suite self-audits). Cross-suite helper imports use the full package path
(`from tests.paper.test_paper_build_retention import …`); shared harnesses stay
at the root (`from tests._jsdom import …`).

Two files remain at the top level because of in-flight edits elsewhere;
they join their domains once those land.

## Test tiers

| Tier | Marker | Command | Needs Server? | Needs Browser? |
|------|--------|---------|---------------|----------------|
| **Unit** | `@pytest.mark.unit` | `make test-unit` | No | No |
| **API** | `@pytest.mark.api` | `make test-api` | Test server | No |
| **Visual** | `@pytest.mark.visual` | `make test-visual` | Live server | Chromium |
| **Slow** | `@pytest.mark.slow` | `make test-slow` | Varies | Varies |

## Running tests

```bash
# Quick: unit tests only (fast, no dependencies)
make test-unit

# Resource-aware xdist by default; explicit worker and serial overrides
make test-unit JOBS=8
make test-unit JOBS=0

# Full CI pipeline (lint + unit + api + healthcheck)
make ci

# With coverage
make test-coverage

# Individual markers
python -m pytest -m unit -v
python -m pytest -m api --tb=long
python -m pytest -m visual --tb=short

# One domain directory
python -m pytest tests/paper/ -q

# Filter by name
python -m pytest -k "test_build_body" -v

# Suite self-audit (collection quality, assertion census)
make suite-health
```

## tests/ vs debug/

| Directory | Purpose | CI? | Style |
|-----------|---------|-----|-------|
| `tests/` | Structured, self-contained pytest tests with mocks | ✅ Yes | pytest classes + markers |
| `debug/` | Manual exploration scripts, benchmarks, API-dependent tests | ❌ No | Standalone scripts, `python debug/script.py` |

Tests in `debug/` require real API keys, live servers, or specific hardware
(GPUs, etc.) and are **not** run in CI. When a `debug/` test proves valuable
for regression prevention, it gets migrated to `tests/` with proper mocking.

## Adding new tests

1. Create `tests/<domain>/test_*.py` in the domain of the module under test
   (see the placement rule above).
2. Add `@pytest.mark.unit` (or `api`/`visual`/`slow`) to every test.
3. Use fixtures from `conftest.py` for app boot/client/mock LLM.
4. Verify: `make test-unit` passes.
5. Ensure `make ci` passes before submitting a PR.

Frontend tests for migrated TypeScript owners should load the smallest real
owner graph through `tests._runtime_sections.native_module_path` or
`native_module_graph`. Give every materialized bundle a unique name below
`.native/`; graph placeholders intentionally share one temporary directory and
generic legacy filenames can otherwise overwrite an unrelated fixture. Inject
ports at public constructors, assert observable DOM/state/Promise behavior,
and do not reconstruct deleted `static/js` paths or inspect source strings.

Markers are declared in `pyproject.toml` under `[tool.pytest.ini_options]`,
which sets `--strict-markers` (an unknown/misspelled marker errors at
collection). A test with NO tier marker is auto-tagged `unit` by a
`pytest_collection_modifyitems` hook in `conftest.py` (so it still runs in
`make test-unit` / `ci` rather than silently disappearing), and the run emits
a warning naming the file. Always add the correct marker explicitly — the
auto-`unit` default is wrong for tests that need PostgreSQL (`api`), a browser
(`visual`), or are long-running (`slow`).
