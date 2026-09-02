"""Executable contract for bounded, work-stealing pytest execution.

The scheduler is owned by pyproject.toml. ``tests.conftest`` owns only the
resource-aware worker budget used when an entry point requests ``-n auto``.
"""

from __future__ import annotations

from pathlib import Path
import shlex
from types import SimpleNamespace
import tomllib

import pytest
import runtime_guards
from tests import conftest as shared_pytest


pytestmark = pytest.mark.unit

_AUDIT_SYNTHETIC_REPO_PATHS = {
    'tests/test_one.py',
    'tests/test_two.py',
}


def _config(*arguments: str):
    return SimpleNamespace(args=list(arguments))


def test_pyproject_owns_worksteal_without_enabling_bare_pytest():
    root = Path(__file__).parents[1]
    project = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))
    addopts = shlex.split(project['tool']['pytest']['ini_options']['addopts'])

    dist_index = addopts.index('--dist')
    assert addopts[dist_index + 1] == 'worksteal'
    assert '-n' not in addopts
    assert addopts.index('no:xdist') < addopts.index('xdist.plugin')
    assert addopts.index('no:anyio') < addopts.index('anyio.pytest_plugin')


def test_auto_workers_reuse_runtime_budget_with_personal_ceiling(monkeypatch):
    monkeypatch.delenv('PYTEST_XDIST_AUTO_NUM_WORKERS', raising=False)
    monkeypatch.setattr(
        runtime_guards, 'deployment_resource_default',
        lambda name: 12 if name == 'TOFU_MAX_INFLIGHT_TASKS' else 0,
    )

    assert shared_pytest.pytest_xdist_auto_num_workers(_config('tests')) == 4


def test_auto_workers_do_not_exceed_selected_test_files(monkeypatch):
    monkeypatch.delenv('PYTEST_XDIST_AUTO_NUM_WORKERS', raising=False)
    monkeypatch.setattr(
        runtime_guards, 'deployment_resource_default', lambda _name: 4)

    assert shared_pytest.pytest_xdist_auto_num_workers(_config(
        'tests/test_one.py', 'tests/test_two.py')) == 2


def test_auto_worker_probe_failure_falls_back_lean(monkeypatch):
    monkeypatch.delenv('PYTEST_XDIST_AUTO_NUM_WORKERS', raising=False)

    def _failed_probe(_name):
        raise OSError('probe unavailable')

    monkeypatch.setattr(
        runtime_guards, 'deployment_resource_default', _failed_probe)

    assert shared_pytest.pytest_xdist_auto_num_workers(_config('tests')) == 1


def test_official_auto_worker_override_remains_authoritative(monkeypatch):
    monkeypatch.setenv('PYTEST_XDIST_AUTO_NUM_WORKERS', '7')

    assert shared_pytest.pytest_xdist_auto_num_workers(_config('tests')) == 7
