# ═══════════════════════════════════════════════════════════════
#  Tofu (豆腐) — Development Makefile
# ═══════════════════════════════════════════════════════════════
#
#  Usage:
#    make lint          — Run ruff linter + format check
#    make test-unit     — Run unit tests only
#    make test-api      — Run API integration tests only
#    make test-visual   — Run Playwright visual E2E tests
#    make test-e2e      — Run hermetic E2E smoke (real app + browser + stub LLM)
#    make test-frontend — Run frontend jsdom + tsc tests (needs `npm install`)
#    make test-all      — Run all tests (unit + api + visual)
#    make audit-tests   — Census the test suite's own health (report)
#    make suite-health  — Gate: test-suite health must not regress (ratchet)
#    make healthcheck   — Run project diagnostics
#    make ci            — Full CI pipeline (lint + unit + api + healthcheck)
#    make smoke         — Run smoke tests only
#    make desktop       — Build desktop installer (PyInstaller)
#    make desktop-icons — Generate .ico/.icns from logo.png
#    make vendor-mcp    — Re-sync tools/<name>/ snapshots of internal MCP servers
#    make stop          — Stop the running Tofu server (graceful SIGTERM)
#
# ═══════════════════════════════════════════════════════════════

.PHONY: lint test-unit test-api test-visual test-e2e test-frontend test-all test-coverage healthcheck ci smoke help desktop desktop-icons stop vendor-mcp audit-tests suite-health

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

# ── Linting ────────────────────────────────────────────────────

lint: ## Run ruff linter (errors only — blocks CI)
	python -m ruff check lib/ routes/ tests/

.PHONY: lint-format
lint-format: ## Check formatting (non-blocking, for gradual adoption)
	python -m ruff format --check lib/ routes/ tests/ || echo '⚠️  Format issues found — run `make lint-fix` to auto-fix'

lint-fix: ## Auto-fix lint issues
	python -m ruff check --fix lib/ routes/ tests/
	python -m ruff format lib/ routes/ tests/

# ── Vendoring internal MCP servers ─────────────────────────────

vendor-mcp: ## Re-sync tools/<name>/ snapshots of internal MCP servers from sibling checkouts
	./scripts/vendor_mcp.sh

.PHONY: typecheck
typecheck: ## Type-check the vanilla-JS frontend (tsc --checkJs, no build step)
	@if [ ! -d node_modules/typescript ]; then echo '⚠️  Run `npm install` first (installs TypeScript dev-dep)'; exit 1; fi
	npx tsc --noEmit

# ── Test-suite health ────────────────────────────────────────
#
# The suite is ~1160 files / ~320k lines — far past the point where "review the
# tests by reading them" is a real activity. audit-tests performs that review
# mechanically (AST only, no imports/execution, ~6s) and reports the failure
# modes that make a test worthless: no assertion, skip-only, laundered by a bare
# except, a source anchor that no longer matches, a scan target that no longer
# exists. suite-health is the CI-binding form (a one-way ratchet against
# tests/audit_baseline.json).

.PHONY: audit-tests suite-health
audit-tests: ## Census the test suite's own health (human-readable report)
	python3 scripts/audit_tests.py

suite-health: ## Gate: test-suite health must not regress (one-way ratchet)
	python3 -m pytest $(PYTEST_BASE) tests/test_suite_health_ratchet.py --timeout=600 --tb=short -q

# ── Tests ──────────────────────────────────────────────────────
#
# JOBS controls test parallelism (pytest-xdist). Default `auto` = one worker
# per core — full capacity. Each worker re-imports the full `server` module,
# which used to `mlockall()` its ~340 MB C-extension working set as
# UNRECLAIMABLE pinned memory; under `auto` on a many-core box that was a burst
# of tens of GB of un-reclaimable pages that OOM-reaped the pod (and any
# co-resident live server). That root cause is fixed in tests/conftest.py
# (TOFU_MLOCK=0 in test workers → transient, reclaimable RSS), so `auto` is now
# safe: worker RSS is ordinary reclaimable memory the kernel can page under
# pressure. Override with `JOBS=N` on a tight box; `JOBS=0` runs serially.
JOBS ?= auto
PYTEST_PARALLEL = $(if $(filter 0,$(JOBS)),,-n $(JOBS) --dist worksteal)

# PYTEST_BASE — flags every Python test target needs in THIS env. TWO entrypoint
# landmines, both empirically reproduced 2026-08-04:
#   `-p no:napari`  — the stray napari plugin whose import chain
#                     (napari→vispy→OpenGL) crashes collection with
#                     `OSError: GL ES 2.0 library not found` on a headless box.
#   `-p no:timeout` — pytest-timeout's entrypoint registers under the name
#                     'timeout', then pyproject addopts `-p pytest_timeout`
#                     registers the same module under its module name →
#                     `ValueError: Plugin already registered under a different
#                     name: timeout`. Blocking the ENTRYPOINT leaves addopts'
#                     explicit load as the single registration.
# Surgical rather than PYTEST_DISABLE_PLUGIN_AUTOLOAD=1, which would also drop
# xdist/anyio and force us to re-add each by hand.
PYTEST_BASE = -p no:napari -p no:timeout

test-unit: ## Run unit tests (parallel; override JOBS=N, JOBS=0 for serial)
	python -m pytest $(PYTEST_BASE) -m unit $(PYTEST_PARALLEL) --timeout=300 --tb=short -q

test-api: ## Run API integration tests (Flask test client + mock LLM)
	python -m pytest $(PYTEST_BASE) -m api $(PYTEST_PARALLEL) --timeout=300 --tb=short -q

test-visual: ## Run Playwright visual E2E tests (needs chromium)
	python -m pytest $(PYTEST_BASE) -m visual --tb=short -q

test-e2e: ## Run hermetic E2E journeys — real app + real browser + stub LLM (P0-3 主干道巡检)
	python -m pytest $(PYTEST_BASE) tests/test_e2e_smoke.py tests/test_e2e_journeys.py -m visual -ra --tb=short -q

test-frontend: ## Run frontend tests (jsdom harnesses + tsc ratchet — needs `npm install`)
	@if [ ! -d node_modules/jsdom ]; then echo '⚠️  Run `npm install` first (installs jsdom + typescript dev-deps)'; exit 1; fi
	TOFU_REQUIRE_FRONTEND=1 python -m pytest $(PYTEST_BASE) tests/test_frontend_*.py $(PYTEST_PARALLEL) --timeout=180 -ra --tb=short -q

test-all: ## Run all tests (unit + api + visual)
	python -m pytest $(PYTEST_BASE) --tb=short -q

test-coverage: ## Run unit + api tests with coverage report
	python -m pytest $(PYTEST_BASE) -m "unit or api" --cov=lib --cov=routes --cov-report=term-missing --tb=short -q

smoke: ## Run smoke tests only (import validation, cross-platform, syntax)
	python -m pytest $(PYTEST_BASE) tests/test_smoke.py -m unit --tb=short -v

test-affected: ## Iteration loop: run only tests that can see your changes (static reverse index — full tier stays the gate, P2-3)
	python scripts/test_select.py --run

# ── Diagnostics ────────────────────────────────────────────────

healthcheck: ## Run project health diagnostics
	python healthcheck.py

# ── CI Pipeline ────────────────────────────────────────────────

ci: lint test-unit test-api suite-health healthcheck ## Full CI pipeline (lint + unit + api + suite-health + healthcheck)
	@echo ""
	@echo "  ✅ CI pipeline passed"
	@echo ""

# ── Desktop Build ──────────────────────────────────────────────

.PHONY: desktop desktop-icons

desktop-icons: ## Generate platform icons (.ico/.icns) from logo.png
	python scripts/gen_desktop_icons.py

desktop: desktop-icons ## Build desktop installer (PyInstaller)
	pip install -r desktop/requirements-desktop.txt
	pyinstaller tofu.spec
	@echo ""
	@echo "  ✅ Desktop build complete → dist/Tofu/"
	@echo ""

# ── Server lifecycle ───────────────────────────────────────────

stop: ## Stop the running Tofu server (reads data/.server.lock, SIGTERM)
	./stop.sh
