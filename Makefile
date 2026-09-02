# ═══════════════════════════════════════════════════════════════
#  Tofu (豆腐) — Development Makefile
# ═══════════════════════════════════════════════════════════════
#
#  Usage:
#    make lint          — Run ruff linter + format check
#    make test-unit     — Run unit tests only
#    make test-api      — Run API integration tests only
#    make test-slow     — Run expensive deterministic tests
#    make test-visual   — Run Playwright visual E2E tests
#    make test-e2e      — Run hermetic E2E smoke (real app + browser + stub LLM)
#    make test-frontend — Run Vite/ESM artifact + owner contracts (needs npm)
#    make test-all      — Run all tests (unit + api + visual)
#    make audit-tests   — Census the test suite's own health (report)
#    make suite-health  — Gate: test-suite health must not regress (ratchet)
#    make docs-check    — Validate the current-only documentation catalog
#    make healthcheck   — Run project diagnostics
#    make ci            — Full CI pipeline (lint + unit + api + healthcheck)
#    make smoke         — Run smoke tests only
#    make desktop       — Build desktop installer (PyInstaller)
#    make desktop-icons — Generate .ico/.icns from logo.png
#    make vendor-mcp    — Re-sync tools/<name>/ snapshots of internal MCP servers
#    make stop          — Stop the running Tofu server (graceful SIGTERM)
#
# ═══════════════════════════════════════════════════════════════

.PHONY: lint docs-check architecture-check contracts-check test-unit test-api test-api-core test-slow test-visual test-e2e test-frontend test-all test-coverage healthcheck ci smoke help desktop desktop-icons stop vendor-mcp audit-tests suite-health

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

# ── Linting ────────────────────────────────────────────────────

lint: ## Run ruff linter (errors only — blocks CI)
	python -m ruff check lib/ routes/ tests/

docs-check: ## Validate the current-only documentation catalog and links
	python3 scripts/check_documentation.py

architecture-check: ## Reject retired surfaces and incident-only source notation
	python3 scripts/check_architecture.py

contracts-check: ## Reject generated API client and server drift
	python3 scripts/gen_conversation_sync_contract.py --check
	python3 scripts/gen_api_v4_contract.py --check

.PHONY: lint-format
lint-format: ## Check formatting (non-blocking, for gradual adoption)
	python -m ruff format --check lib/ routes/ tests/ || echo '⚠️  Format issues found — run `make lint-fix` to auto-fix'

lint-fix: ## Auto-fix lint issues
	python -m ruff check --fix lib/ routes/ tests/
	python -m ruff format lib/ routes/ tests/

# ── Vendoring internal MCP servers ─────────────────────────────

vendor-mcp: ## Re-sync tools/<name>/ snapshots of internal MCP servers from sibling checkouts
	./scripts/vendor_mcp.sh

.PHONY: typecheck frontend-build frontend-budget
typecheck: ## Type-check the Vite ESM graph
	@if [ ! -d node_modules/typescript ]; then echo '⚠️  Run `npm install` first (installs TypeScript dev-dep)'; exit 1; fi
	npx tsc --noEmit
	npm run typecheck:modules

frontend-build: ## Build content-hashed Vite/TypeScript modules
	npm run build:frontend

frontend-budget: frontend-build ## Enforce compressed frontend resource budgets
	python3 scripts/frontend_budget.py

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
	$(PYTEST) $(PYTEST_BASE) tests/test_suite_health_ratchet.py --timeout=600 --tb=short -q

# ── Tests ──────────────────────────────────────────────────────
#
# JOBS controls test parallelism (pytest-xdist). ``auto`` is NOT raw host CPU
# count: tests/conftest.py derives a 1..4 default from the same affinity/cgroup
# CPU and live-memory probe used by the personal runtime. Each worker imports
# the full server stack (measured ~170-205 MB RSS) and can spawn Node/browser
# children, so this bound preserves headroom for the OS and a live Tofu server.
# Dedicated hosts may choose an explicit JOBS=N; JOBS=0 runs serially.
# ``--dist worksteal`` is owned once by pyproject.toml.
JOBS ?= auto
PYTEST_PARALLEL = $(if $(filter 0,$(JOBS)),,-n $(JOBS))

# Every xdist worker imports NumPy through the app/plugin graph. On a 64-core
# host, leaving BLAS defaults untouched creates ~64 native threads PER worker;
# bounded process parallelism alone does not prevent that nested explosion.
# Tests are latency-insensitive and predominantly non-numeric, so one native
# numeric worker is the safe default. Dedicated benchmark hosts can override
# with TEST_NUMERIC_THREADS=N.
TEST_NUMERIC_THREADS ?= 1
PYTEST = OPENBLAS_NUM_THREADS=$(TEST_NUMERIC_THREADS) \
	OMP_NUM_THREADS=$(TEST_NUMERIC_THREADS) \
	MKL_NUM_THREADS=$(TEST_NUMERIC_THREADS) \
	NUMEXPR_NUM_THREADS=$(TEST_NUMERIC_THREADS) python -m pytest

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
# fixture plugins such as anyio. pyproject.toml explicitly owns timeout, xdist,
# and anyio because every test entry point depends on those policies.
PYTEST_BASE = -p no:napari -p no:timeout

# The pre-Vite jsdom harnesses load individual files from the deleted
# ``static/js`` graph and therefore cannot describe the shipped application.
# Keep Python/backend collection independent of those historical files; the
# frontend lane below runs the ESM source, artifact and serving contracts after
# producing the exact graph users receive.
FRONTEND_UNIT_IGNORES = \
	--ignore=tests/test_application_shell_fragments.py \
	--ignore-glob=tests/test_frontend_*.py \
	--ignore=tests/test_bundle_corruption_guard.py \
	--ignore=tests/test_bundle_concurrency.py \
	--ignore=tests/test_bundle_manifest_parity.py \
	--ignore=tests/test_bundle_manifest_freshness.py \
	--ignore=tests/test_bundle_nonblocking_serve.py \
	--ignore=tests/test_bundle_source_syntax_ratchet.py \
	--ignore=tests/test_i18n_pack_emission.py \
	--ignore=tests/test_stale_bundle_self_heal.py \
	--ignore=tests/test_stale_i18n_pack_self_heal.py \
	--ignore=tests/test_static_route_offload.py \
	--ignore=tests/test_static_serving_registration.py \
	--ignore=tests/test_task_mode_states.py \
	--ignore=tests/test_orchestration_endpoint_parity.py \
	--ignore=tests/test_orchestration_wire_contracts.py \
	--ignore=tests/test_agent_mode_selector.py \
	--ignore=tests/test_vite_migration_residuals.py

FRONTEND_ESM_TESTS = \
	tests/test_application_shell_fragments.py \
	tests/test_bundle_corruption_guard.py \
	tests/test_bundle_concurrency.py \
	tests/test_bundle_manifest_parity.py \
	tests/test_bundle_manifest_freshness.py \
	tests/test_bundle_nonblocking_serve.py \
	tests/test_bundle_source_syntax_ratchet.py \
	tests/test_frontend_api_transport_vite.py \
	tests/test_frontend_attempt_stream_vite.py \
	tests/test_frontend_authoritative_composer.py \
	tests/test_frontend_auto_translate_default.py \
	tests/test_frontend_balance_vite.py \
	tests/test_frontend_browser_storage_vite.py \
	tests/test_frontend_budget.py \
	tests/test_frontend_conversation_surface_vite.py \
	tests/test_frontend_memory_vite.py \
	tests/test_frontend_model_catalog_vite.py \
	tests/test_frontend_paper_arxiv_fetch_vite.py \
	tests/test_frontend_paper_arxiv_search_vite.py \
	tests/test_frontend_paper_lifecycle_vite.py \
	tests/test_frontend_paper_podcast_runtime_vite.py \
	tests/test_frontend_paper_push_transport_vite.py \
	tests/test_frontend_paper_qa_task_vite.py \
	tests/test_frontend_paper_recommend_vite.py \
	tests/test_frontend_paper_report_runtime_vite.py \
	tests/test_frontend_paper_video.py \
	tests/test_frontend_paper_video_runtime_vite.py \
	tests/test_frontend_project_mode_decoupling.py \
	tests/test_frontend_timeline_narration_translation.py \
	tests/test_frontend_translate_guard.py \
	tests/test_frontend_vite_asset_base.py \
	tests/test_frontend_vite_domains.py \
	tests/test_agent_mode_selector.py \
	tests/test_vite_migration_residuals.py \
	tests/test_i18n_pack_emission.py \
	tests/test_install_npm_bounded.py \
	tests/test_orchestration_endpoint_parity.py \
	tests/test_orchestration_wire_contracts.py \
	tests/test_stale_bundle_self_heal.py \
	tests/test_stale_i18n_pack_self_heal.py \
	tests/test_static_route_offload.py \
	tests/test_static_serving_registration.py \
	tests/test_task_mode_states.py

ifeq ($(JOBS),0)
test-unit: ## Run unit tests (parallel; override JOBS=N, JOBS=0 for serial)
	$(PYTEST) $(PYTEST_BASE) $(FRONTEND_UNIT_IGNORES) -m unit --timeout=300 --tb=short -q
else
test-unit: ## Run unit tests (parallel; override JOBS=N, JOBS=0 for serial)
	$(PYTEST) $(PYTEST_BASE) $(FRONTEND_UNIT_IGNORES) -m "unit and not serial" $(PYTEST_PARALLEL) --timeout=300 --tb=short -q
	$(PYTEST) $(PYTEST_BASE) $(FRONTEND_UNIT_IGNORES) -m "unit and serial" --timeout=600 --tb=short -q
endif

test-api: frontend-build test-api-core ## Build the shipped graph, then run API integration tests

test-api-core: ## Run API integration tests against the available shipped graph
	$(PYTEST) $(PYTEST_BASE) -m api $(PYTEST_PARALLEL) --timeout=300 --tb=short -q

test-slow: ## Run expensive deterministic tests with bounded parallelism
	$(PYTEST) $(PYTEST_BASE) -m slow $(PYTEST_PARALLEL) --timeout=600 --tb=short -q

test-visual: ## Run Playwright visual E2E tests (needs chromium)
	$(PYTEST) $(PYTEST_BASE) -m visual --tb=short -q

test-e2e: ## Run hermetic E2E journeys — real app + real browser + stub LLM (P0-3 主干道巡检)
	$(PYTEST) $(PYTEST_BASE) tests/test_e2e_smoke.py tests/test_e2e_journeys.py -m visual -ra --tb=short -q

test-frontend: frontend-build ## Run Vite ESM source, artifact and serving contracts
	@if [ ! -d node_modules/jsdom ]; then echo '⚠️  Run `npm install` first (installs jsdom + typescript dev-deps)'; exit 1; fi
	npm run check:runtime
	npm run check:styles
	npm run check:i18n
	npm run check:actions
	TOFU_REQUIRE_FRONTEND=1 $(PYTEST) $(PYTEST_BASE) $(FRONTEND_ESM_TESTS) $(PYTEST_PARALLEL) --timeout=300 -ra --tb=short -q

test-all: test-unit test-api test-frontend test-e2e ## Run all current backend, ESM frontend and browser gates

test-coverage: ## Run unit + api tests with coverage report
	$(PYTEST) $(PYTEST_BASE) -m "unit or api" --cov=lib --cov=routes --cov-report=term-missing --tb=short -q

smoke: ## Run smoke tests only (import validation, cross-platform, syntax)
	$(PYTEST) $(PYTEST_BASE) tests/test_smoke.py -m unit --tb=short -v

test-affected: ## Iteration loop: run only tests that can see your changes (static reverse index — full tier stays the gate, P2-3)
	python scripts/test_select.py --run

# ── Diagnostics ────────────────────────────────────────────────

healthcheck: ## Run project health diagnostics
	python healthcheck.py

# ── CI Pipeline ────────────────────────────────────────────────

ci: lint docs-check architecture-check contracts-check test-unit test-api suite-health healthcheck ## Full CI pipeline
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

.PHONY: start stop restart status logs doctor

start: ## Start Tofu through the project-local manager
	python serverctl.py start

stop: ## Stop Tofu and keep it stopped
	python serverctl.py stop

restart: ## Restart Tofu through the project-local manager
	python serverctl.py restart

status: ## Show manager + server ownership and health state
	python serverctl.py status

logs: ## Follow the managed server console
	python serverctl.py logs -f

doctor: ## Diagnose locks, ports, manager and legacy lifecycle owners
	python serverctl.py doctor
