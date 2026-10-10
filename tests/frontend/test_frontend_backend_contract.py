"""Behavior specifications for the browser route inventory guard.

The production surface check lives in test_frontend_api_contract.py. This file
proves its parser and matcher reject omissions without starting storage.
"""
from pathlib import Path
import subprocess

import pytest

from tests.support.frontend_api_inventory import browser_route_app, route_failure

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_browser_api_ast_inventory():
    result = subprocess.run(
        ['node', '--test', 'tests/frontend_api_inventory.test.mjs'],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_route_matcher_checks_verbs_and_retired_paths():
    app = browser_route_app()
    assert route_failure(app, '/api/v4/conversations', 'GET') is None
    assert route_failure(app, '/api/v4/conversations/<dynamic>', 'DELETE') is None
    assert route_failure(app, '/api/v4/evolution/capability-gaps', 'GET') is None
    assert route_failure(app, '/api/v4/slides/<dynamic>/pages/<dynamic>.png', 'GET') is None
    assert route_failure(app, '/api/v4/conversations', 'PATCH')
    assert route_failure(app, '/api/v1/conversations', 'GET')
    assert route_failure(app, '/api/v4/missing-audit-endpoint', 'GET')
