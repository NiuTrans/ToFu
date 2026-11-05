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


def _probe(monkeypatch, *, cpus: int, available_mb: int | None):
    snapshot = runtime_guards.SystemResourceSnapshot(
        host_cpu_count=cpus, affinity_cpu_count=cpus, cgroup_cpu_count=None,
        effective_cpu_count=cpus, host_memory_total_mb=None,
        host_memory_available_mb=available_mb, cgroup_memory_limit_mb=None,
        cgroup_memory_current_mb=None, effective_memory_capacity_mb=None,
        effective_memory_available_mb=available_mb, disk_total_mb=None,
        disk_free_mb=None)
    monkeypatch.setattr(
        runtime_guards, 'probe_system_resources', lambda *_a, **_k: snapshot)


def test_pyproject_owns_worksteal_without_enabling_bare_pytest():
    root = Path(__file__).parents[2]
    project = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))
    addopts = shlex.split(project['tool']['pytest']['ini_options']['addopts'])

    dist_index = addopts.index('--dist')
    assert addopts[dist_index + 1] == 'worksteal'
    assert '-n' not in addopts
    assert addopts.index('no:xdist') < addopts.index('xdist.plugin')
    assert addopts.index('no:anyio') < addopts.index('anyio.pytest_plugin')


def test_auto_workers_follow_the_probe_with_personal_ceiling(monkeypatch):
    monkeypatch.delenv('PYTEST_XDIST_AUTO_NUM_WORKERS', raising=False)
    # The runtime task ceiling is unlimited, so it can never size test workers.
    monkeypatch.setenv('TOFU_MAX_INFLIGHT_TASKS', '1')
    _probe(monkeypatch, cpus=12, available_mb=32 * 1024)

    assert shared_pytest.pytest_xdist_auto_num_workers(_config('tests')) == 4


@pytest.mark.parametrize(('cpus', 'available_mb', 'expected'), [
    (2, 64 * 1024, 2),
    (16, 3 * 1024, 3),
    (16, 512, 1),
    (16, None, 1),
])
def test_auto_workers_need_one_cpu_and_one_gib_of_headroom_each(
        monkeypatch, cpus, available_mb, expected):
    monkeypatch.delenv('PYTEST_XDIST_AUTO_NUM_WORKERS', raising=False)
    _probe(monkeypatch, cpus=cpus, available_mb=available_mb)

    assert shared_pytest.pytest_xdist_auto_num_workers(_config('tests')) == expected


def test_auto_workers_do_not_exceed_selected_test_files(monkeypatch):
    monkeypatch.delenv('PYTEST_XDIST_AUTO_NUM_WORKERS', raising=False)
    _probe(monkeypatch, cpus=4, available_mb=16 * 1024)

    assert shared_pytest.pytest_xdist_auto_num_workers(_config(
        'tests/test_one.py', 'tests/test_two.py')) == 2


def test_auto_worker_probe_failure_falls_back_lean(monkeypatch):
    monkeypatch.delenv('PYTEST_XDIST_AUTO_NUM_WORKERS', raising=False)

    def _failed_probe(*_args, **_kwargs):
        raise OSError('probe unavailable')

    monkeypatch.setattr(
        runtime_guards, 'probe_system_resources', _failed_probe)

    assert shared_pytest.pytest_xdist_auto_num_workers(_config('tests')) == 1


def test_official_auto_worker_override_remains_authoritative(monkeypatch):
    monkeypatch.setenv('PYTEST_XDIST_AUTO_NUM_WORKERS', '7')

    assert shared_pytest.pytest_xdist_auto_num_workers(_config('tests')) == 7
