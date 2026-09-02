"""Personal-server worker defaults stay bounded and malformed knobs fail safe."""

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_parallel_tool_pool_defaults_to_four_and_clamps(monkeypatch):
    import runtime_guards
    from lib.tasks_pkg.tool_dispatch._pipeline import _parallel_worker_limit

    monkeypatch.delenv('TOOL_MAX_PARALLEL_WORKERS', raising=False)
    monkeypatch.delenv('TOFU_DEPLOYMENT_MODE', raising=False)
    monkeypatch.setattr(
        runtime_guards, 'deployment_resource_default', lambda *_args: 4)
    assert _parallel_worker_limit(100) == 4
    assert _parallel_worker_limit(3) == 3
    monkeypatch.setenv('TOOL_MAX_PARALLEL_WORKERS', '99999')
    assert _parallel_worker_limit(100) == 32
    monkeypatch.setenv('TOOL_MAX_PARALLEL_WORKERS', 'not-a-number')
    assert _parallel_worker_limit(100) == 4


def test_parallel_timeout_knob_is_bounded_and_malformed_safe(monkeypatch):
    from lib.tasks_pkg.tool_dispatch._pipeline import _bounded_tool_env_int

    monkeypatch.setenv('TOOL_PARALLEL_TIMEOUT', 'bad')
    assert _bounded_tool_env_int('TOOL_PARALLEL_TIMEOUT', 300, 1, 3600) == 300
    monkeypatch.setenv('TOOL_PARALLEL_TIMEOUT', '999999')
    assert _bounded_tool_env_int('TOOL_PARALLEL_TIMEOUT', 300, 1, 3600) == 3600


def test_server_zero_config_executor_defaults_fit_personal_hosts(monkeypatch):
    from lib import server_loop_runtime

    monkeypatch.setattr(server_loop_runtime.os, 'cpu_count', lambda: 64)
    expected = {
        'TOFU_SYNC_WORKERS': 8,
        'TOFU_AGENT_WORKERS': 4,
    }
    monkeypatch.setattr(
        server_loop_runtime, 'deployment_resource_default',
        lambda key, _environment: expected[key])
    logger = server_loop_runtime.logging.getLogger(__name__)
    assert server_loop_runtime._worker_count(
        'TOFU_SYNC_WORKERS', {}, logger) == 8
    assert server_loop_runtime._worker_count(
        'TOFU_AGENT_WORKERS', {}, logger) == 4


def test_server_numeric_runtime_defaults_are_bounded_and_overridable(
        monkeypatch):
    import server

    for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS',
                 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('TOFU_NUMERIC_THREADS', '999')
    assert server._install_numeric_thread_defaults() == 32
    assert all(os.environ[name] == '32' for name in (
        'OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS',
        'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS'))

    # Inherited host settings cannot bypass Tofu's aggregate resource ceiling.
    monkeypatch.setenv('OPENBLAS_NUM_THREADS', '7')
    monkeypatch.setenv('OMP_NUM_THREADS', '1')
    monkeypatch.setenv('TOFU_NUMERIC_THREADS', '2')
    assert server._install_numeric_thread_defaults() == 2
    assert os.environ['OPENBLAS_NUM_THREADS'] == '2'
    # A smaller, deliberate per-library limit remains intact.
    assert os.environ['OMP_NUM_THREADS'] == '1'


def test_every_server_child_launcher_installs_allocator_budget():
    root = Path(__file__).parents[1]
    launchers = (
        root / 'server_manager.py',
        root / 'bootstrap_pkg' / 'launcher.py',
        root / 'desktop' / 'launcher.py',
        root / 'supervisor.py',
        root / 'lib' / 'storage' / 'supervisor.py',
    )
    for launcher in launchers:
        source = launcher.read_text(encoding='utf-8')
        assert 'install_process_resource_defaults(' in source, launcher
    assert 'MALLOC_ARENA_MAX=2' in (
        root / 'Dockerfile').read_text(encoding='utf-8')
    server_source = (root / 'server.py').read_text(encoding='utf-8')
    assert server_source.index('\n_load_early_resource_environment()\n') \
        < server_source.index('install_runtime_resource_defaults(os.environ)')
    assert server_source.index('install_runtime_resource_defaults(os.environ)') \
        < server_source.index(
            '_NUMERIC_THREADS = _install_numeric_thread_defaults()')


def test_make_defaults_bound_process_and_nested_numeric_parallelism():
    source = (Path(__file__).parents[1] / 'Makefile').read_text()
    assert 'JOBS ?= auto' in source
    assert 'TEST_NUMERIC_THREADS ?= 1' in source
    for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS',
                 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
        assert f'{name}=$(TEST_NUMERIC_THREADS)' in source
    assert 'python -m pytest' in source
