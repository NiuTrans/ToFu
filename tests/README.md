# Tests

## Directory Structure

```
tests/
├── conftest.py                  — Shared fixtures (Flask app, mock LLM, Playwright)
├── mock_llm_server.py           — Standalone mock LLM API server
├── run_all.py                   — Legacy test runner (prefer pytest/make)
│
├── test_smoke.py                — Import validation, syntax checks, blueprint registration
├── test_backend_unit.py         — Core backend unit tests (build_body, tool parsing, etc.)
├── test_swarm_unit.py           — Multi-agent swarm system (protocol, registry, orchestrator)
├── test_package_facades.py      — Package façade import validation (search, browser, pdf, skills)
├── test_project_tools.py        — Project-mode helpers (output cleaning, write targets, safety)
├── test_cross_platform.py       — Cross-platform compat layer tests
├── test_cc_alignment.py         — Claude Code alignment feature tests
├── test_new_features.py         — New feature integration tests
├── test_compaction_improvements.py — Context compaction pipeline tests
├── test_streaming_and_prefetch.py  — Streaming & URL prefetch tests
│
├── test_api_integration.py      — API integration tests (Flask test client + mock LLM)
├── test_visual_e2e.py           — Playwright visual E2E tests
└── visual_check.py              — VLM screenshot analysis helper
```

## Test Tiers

| Tier | Marker | Command | Needs Server? | Needs Browser? |
|------|--------|---------|---------------|----------------|
| **Unit** | `@pytest.mark.unit` | `make test-unit` | No | No |
| **API** | `@pytest.mark.api` | `make test-api` | Test server | No |
| **Visual** | `@pytest.mark.visual` | `make test-visual` | Live server | Chromium |
| **Slow** | `@pytest.mark.slow` | (opt-in) | Varies | Varies |

## Running Tests

```bash
# Quick: unit tests only (fast, no dependencies)
make test-unit

# Full CI pipeline (lint + unit + api + healthcheck)
make ci

# With coverage
make test-coverage

# Individual markers
python -m pytest -m unit -v
python -m pytest -m api --tb=long
python -m pytest -m visual --tb=short

# Filter by name
python -m pytest -k "test_build_body" -v

# Just smoke tests
make smoke
```

## tests/ vs debug/

| Directory | Purpose | CI? | Style |
|-----------|---------|-----|-------|
| `tests/` | Structured, self-contained pytest tests with mocks | ✅ Yes | pytest classes + markers |
| `debug/` | Manual exploration scripts, benchmarks, API-dependent tests | ❌ No | Standalone scripts, `python debug/script.py` |

Tests in `debug/` require real API keys, live servers, or specific hardware
(GPUs, etc.) and are **not** run in CI. When a `debug/` test proves valuable
for regression prevention, it gets migrated to `tests/` with proper mocking.

## Adding New Tests

1. Create `tests/test_*.py` with test functions/classes
2. Add `@pytest.mark.unit` (or `api`/`visual`/`slow`) to every test
3. Use fixtures from `conftest.py` for Flask app/client/mock LLM
4. Verify: `make test-unit` passes
5. Ensure `make ci` passes before submitting a PR

Tests without markers will fail (`strict-markers` is enabled in `pyproject.toml`).
