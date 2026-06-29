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
#    make healthcheck   — Run project diagnostics
#    make ci            — Full CI pipeline (lint + unit + api + healthcheck)
#    make smoke         — Run smoke tests only
#    make desktop       — Build desktop installer (PyInstaller)
#    make desktop-icons — Generate .ico/.icns from logo.png
#    make vendor-mcp    — Re-sync tools/<name>/ snapshots of internal MCP servers
#    make stop          — Stop the running Tofu server (graceful SIGTERM)
#
# ═══════════════════════════════════════════════════════════════

.PHONY: lint test-unit test-api test-visual test-e2e test-frontend test-all test-coverage healthcheck ci smoke help desktop desktop-icons stop vendor-mcp

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

# ── Tests ──────────────────────────────────────────────────────

test-unit: ## Run unit tests (no server, no browser, no network)
	python -m pytest -m unit --tb=short -q

test-api: ## Run API integration tests (Flask test client + mock LLM)
	python -m pytest -m api --tb=short -q

test-visual: ## Run Playwright visual E2E tests (needs chromium)
	python -m pytest -m visual --tb=short -q

test-e2e: ## Run the hermetic E2E smoke test (real app + real browser + stub LLM, no API key)
	python -m pytest tests/test_e2e_smoke.py -m visual -ra --tb=short -q

test-frontend: ## Run frontend tests (jsdom harnesses + tsc ratchet — needs `npm install`)
	@if [ ! -d node_modules/jsdom ]; then echo '⚠️  Run `npm install` first (installs jsdom + typescript dev-deps)'; exit 1; fi
	python -m pytest tests/test_frontend_*.py -ra --tb=short -q

test-all: ## Run all tests (unit + api + visual)
	python -m pytest --tb=short -q

test-coverage: ## Run unit + api tests with coverage report
	python -m pytest -m "unit or api" --cov=lib --cov=routes --cov-report=term-missing --tb=short -q

smoke: ## Run smoke tests only (import validation, cross-platform, syntax)
	python -m pytest tests/test_smoke.py -m unit --tb=short -v

# ── Diagnostics ────────────────────────────────────────────────

healthcheck: ## Run project health diagnostics
	python healthcheck.py

# ── CI Pipeline ────────────────────────────────────────────────

ci: lint test-unit test-api healthcheck ## Full CI pipeline (lint + unit + api + healthcheck)
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
