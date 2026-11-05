"""Personal-server worker defaults stay bounded and malformed knobs fail safe."""

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_parallel_tool_pool_defaults_to_four_and_clamps(monkeypatch):
    import runtime_guards
    from lib.tasks_pkg.tool_dispatch._pipeline import _parallel_idle_reuse_hint

    monkeypatch.delenv('TOOL_MAX_PARALLEL_WORKERS', raising=False)
    monkeypatch.delenv('TOFU_DEPLOYMENT_MODE', raising=False)
    monkeypatch.setattr(
        runtime_guards, 'deployment_resource_default', lambda *_args: 4)
    assert _parallel_idle_reuse_hint(100) == 4
    assert _parallel_idle_reuse_hint(3) == 3
    monkeypatch.setenv('TOOL_MAX_PARALLEL_WORKERS', '99999')
    assert _parallel_idle_reuse_hint(100) == 32
    monkeypatch.setenv('TOOL_MAX_PARALLEL_WORKERS', 'not-a-number')
    assert _parallel_idle_reuse_hint(100) == 4


def test_parallel_timeout_knob_is_bounded_and_malformed_safe(monkeypatch):
    from lib.tasks_pkg.tool_dispatch._pipeline import _bounded_tool_env_int

    monkeypatch.setenv('TOOL_PARALLEL_TIMEOUT', 'bad')
    assert _bounded_tool_env_int('TOOL_PARALLEL_TIMEOUT', 300, 1, 3600) == 300
    monkeypatch.setenv('TOOL_PARALLEL_TIMEOUT', '999999')
    assert _bounded_tool_env_int('TOOL_PARALLEL_TIMEOUT', 300, 1, 3600) == 3600


def test_server_zero_config_executor_defaults_fit_personal_hosts(monkeypatch):
    from lib.server import server_loop_runtime

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


_NUMERIC_LIBRARY_THREADS = ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS',
                            'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS')


def _own_numeric_library_environment(monkeypatch):
    """Start without library ceilings; undo restores each prior value.

    The installer writes ``os.environ`` directly. ``delenv`` records nothing
    for an absent name, so undo would keep the exported ceilings for every
    later test on the worker; ``setenv`` records the absence first.
    """
    for name in _NUMERIC_LIBRARY_THREADS:
        monkeypatch.setenv(name, '')
        monkeypatch.delenv(name)


@pytest.mark.parametrize('configured', [None, '', '0', '-3', 'bad', 'UNLIMITED'])
def test_server_numeric_threads_default_unlimited_leaves_libraries_untouched(
        monkeypatch, configured):
    import runtime_guards
    import server

    monkeypatch.setattr(runtime_guards, 'probe_system_resources',
                        lambda *_args, **_kwargs: pytest.fail('numeric threads must not probe'))
    _own_numeric_library_environment(monkeypatch)
    monkeypatch.setenv('OMP_NUM_THREADS', '7')
    if configured is None:
        monkeypatch.delenv('TOFU_NUMERIC_THREADS', raising=False)
    else:
        monkeypatch.setenv('TOFU_NUMERIC_THREADS', str(
            runtime_guards.UNLIMITED) if configured == 'UNLIMITED' else configured)
    assert server._install_numeric_thread_defaults() == runtime_guards.UNLIMITED
    assert os.environ['OMP_NUM_THREADS'] == '7'
    assert not {'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS',
                'NUMEXPR_NUM_THREADS'} & set(os.environ)


def test_server_numeric_threads_explicit_operator_ceiling_is_honored(monkeypatch):
    import server

    _own_numeric_library_environment(monkeypatch)
    monkeypatch.setenv('TOFU_NUMERIC_THREADS', '999')
    assert server._install_numeric_thread_defaults() == 999
    assert all(os.environ[name] == '999' for name in _NUMERIC_LIBRARY_THREADS)

    monkeypatch.setenv('OPENBLAS_NUM_THREADS', '7')
    monkeypatch.setenv('OMP_NUM_THREADS', '1')
    monkeypatch.setenv('TOFU_NUMERIC_THREADS', '2')
    assert server._install_numeric_thread_defaults() == 2
    assert os.environ['OPENBLAS_NUM_THREADS'] == '2'
    # A smaller, deliberate per-library limit remains intact.
    assert os.environ['OMP_NUM_THREADS'] == '1'


def test_every_server_child_launcher_installs_allocator_budget(
        monkeypatch, tmp_path):
    root = Path(__file__).parents[2]
    launchers = (
        root / 'server_manager.py',
        root / 'bootstrap_pkg' / 'launcher.py',
        root / 'desktop' / 'launcher.py',
        root / 'lib' / 'storage' / 'supervisor.py',
    )
    for launcher in launchers:
        source = launcher.read_text(encoding='utf-8')
        assert 'install_process_resource_defaults(' in source, launcher
    # Supervisor-managed workers compose their environment through the one
    # shared lifecycle helper, which must itself install the budget.
    assert 'managed_worker_environment(' in (
        root / 'supervisor.py').read_text(encoding='utf-8')
    from server_manager import managed_worker_environment
    monkeypatch.delenv('MALLOC_ARENA_MAX', raising=False)
    monkeypatch.delenv('TOFU_MALLOC_ARENA_MAX', raising=False)
    composed = managed_worker_environment(str(tmp_path), {}, {})
    # The arena count is a fixed contract value, never probe-derived; the
    # container image cannot run the launcher first, so it pins the same value.
    from bootstrap_pkg.resource_policy_generated import RESOURCE_FALLBACKS
    arenas = RESOURCE_FALLBACKS['personal']['TOFU_MALLOC_ARENA_MAX']
    assert composed['MALLOC_ARENA_MAX'] == str(arenas)
    assert f'MALLOC_ARENA_MAX={arenas} ' in (
        root / 'Dockerfile').read_text(encoding='utf-8')
    server_source = (root / 'server.py').read_text(encoding='utf-8')
    assert server_source.index('\n_load_early_resource_environment()\n') \
        < server_source.index('install_runtime_resource_defaults(os.environ)')
    assert server_source.index('install_runtime_resource_defaults(os.environ)') \
        < server_source.index(
            '_NUMERIC_THREADS = _install_numeric_thread_defaults()')


def test_make_defaults_bound_process_and_nested_numeric_parallelism():
    source = (Path(__file__).parents[2] / 'Makefile').read_text()
    assert 'JOBS ?= auto' in source
    assert 'TEST_NUMERIC_THREADS ?= 1' in source
    for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS',
                 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
        assert f'{name}=$(TEST_NUMERIC_THREADS)' in source
    assert 'python -m pytest' in source
