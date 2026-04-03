# ═══════════════════════════════════════════════════════════════
#  Tofu (豆腐) — Development Makefile
# ═══════════════════════════════════════════════════════════════
#
#  Usage:
#    make lint          — Run ruff linter + format check
#    make test-unit     — Run unit tests only
#    make test-api      — Run API integration tests only
#    make test-visual   — Run Playwright visual E2E tests
#    make test-all      — Run all tests (unit + api + visual)
#    make healthcheck   — Run project diagnostics
#    make ci            — Full CI pipeline (lint + unit + api + healthcheck)
#    make smoke         — Run smoke tests only
#
# ═══════════════════════════════════════════════════════════════

.PHONY: lint test-unit test-api test-visual test-all test-coverage healthcheck ci smoke help

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

# ── Tests ──────────────────────────────────────────────────────

test-unit: ## Run unit tests (no server, no browser, no network)
	python -m pytest -m unit --tb=short -q

test-api: ## Run API integration tests (Flask test client + mock LLM)
	python -m pytest -m api --tb=short -q

test-visual: ## Run Playwright visual E2E tests (needs chromium)
	python -m pytest -m visual --tb=short -q

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
