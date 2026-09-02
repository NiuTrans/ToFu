"""Storage migration and ASGI lifespan share one bounded boot budget."""

import pytest

from lib.hypercorn_runtime import build_hypercorn_config
from lib.storage.startup_budget import (
    lifespan_startup_timeout,
    storage_startup_timeout,
)
from lib.storage.supervisor import StorageSupervisor


pytestmark = pytest.mark.unit


def test_ordinary_storage_keeps_existing_short_startup_budgets(monkeypatch):
    environment = {'TOFU_STORAGE_FASTPATH': 'off'}

    assert storage_startup_timeout(environment) == 30.0
    assert lifespan_startup_timeout(environment) == 60.0
    config = build_hypercorn_config(
        '127.0.0.1', 8000, keep_alive_timeout=5, environ=environment)
    assert config.startup_timeout == 60.0

    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'off')
    assert StorageSupervisor()._startup_timeout == 30.0


def test_fastpath_seed_budget_reaches_sidecar_and_outer_lifespan(monkeypatch):
    environment = {
        'TOFU_STORAGE_FASTPATH': 'auto',
        'TOFU_STORAGE_FASTPATH_STARTUP_TIMEOUT_S': '240',
    }

    assert storage_startup_timeout(environment) == 240.0
    assert lifespan_startup_timeout(environment) == 300.0
    config = build_hypercorn_config(
        '127.0.0.1', 8000, keep_alive_timeout=5, environ=environment)
    assert config.startup_timeout == 300.0

    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'auto')
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH_STARTUP_TIMEOUT_S', '240')
    assert StorageSupervisor()._startup_timeout == 240.0
    assert StorageSupervisor(startup_timeout=2)._startup_timeout == 2.0


def test_fastpath_seed_budget_has_lean_fallback_and_hard_ceiling():
    malformed = {
        'TOFU_STORAGE_FASTPATH': 'required',
        'TOFU_STORAGE_FASTPATH_STARTUP_TIMEOUT_S': 'not-a-number',
    }
    excessive = {
        'TOFU_STORAGE_FASTPATH': 'auto',
        'TOFU_STORAGE_FASTPATH_STARTUP_TIMEOUT_S': '999999',
    }

    assert storage_startup_timeout(malformed) == 900.0
    assert storage_startup_timeout(excessive) == 3600.0
    assert lifespan_startup_timeout(excessive) == 3660.0
