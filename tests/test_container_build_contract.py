"""Container and frontend-CI build boundaries must fail loudly."""

from __future__ import annotations

from pathlib import Path

import pytest


pytestmark = pytest.mark.unit
_ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    return (_ROOT / name).read_text(encoding='utf-8')


def test_frontend_ci_uses_lockfile_without_fallback():
    workflow = _read('.github/workflows/ci.yml')
    assert 'run: npm ci\n' in workflow
    assert 'npm ci || npm install' not in workflow


def test_docker_playwright_install_fails_loudly():
    dockerfile = _read('Dockerfile')
    assert 'python -m playwright install chromium --with-deps' in dockerfile
    assert 'playwright install chromium --with-deps 2>/dev/null || true' not in dockerfile


def test_docker_context_excludes_local_bulk_and_secrets():
    ignored = {
        line.strip() for line in _read('.dockerignore').splitlines()
        if line.strip() and not line.lstrip().startswith('#')
    }
    required = {
        '.git', '.env', 'data/', 'logs/', 'uploads/', 'tests/', 'debug/',
        'benchmarks/', 'evaluations/', 'node_modules/', 'node_modules.*/',
        'swebench_*/', '*.log', '.coverage',
    }
    assert required <= ignored
    assert '!README.md' in ignored
