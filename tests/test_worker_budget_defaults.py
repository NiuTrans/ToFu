"""Personal-server worker defaults stay bounded and malformed knobs fail safe."""

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_async_database_default_is_at_most_eight(monkeypatch):
    import lib.database.aio as aio

    monkeypatch.delenv('TOFU_DB_AIO_WORKERS', raising=False)
    assert 4 <= aio._default_executor_workers() <= 8


def test_personal_pg_idle_pool_default_is_sixteen():
    source = (Path(__file__).parents[1] / 'lib/database/_core.py').read_text()
    assert "_bounded_pool_int('TOFU_DB_POOL_MAX', 16" in source


def test_parallel_tool_pool_defaults_to_eight_and_clamps(monkeypatch):
    from lib.tasks_pkg.tool_dispatch._pipeline import _parallel_worker_limit

    monkeypatch.delenv('TOOL_MAX_PARALLEL_WORKERS', raising=False)
    assert _parallel_worker_limit(100) == 8
    assert _parallel_worker_limit(3) == 3
    monkeypatch.setenv('TOOL_MAX_PARALLEL_WORKERS', '99999')
    assert _parallel_worker_limit(100) == 32
    monkeypatch.setenv('TOOL_MAX_PARALLEL_WORKERS', 'not-a-number')
    assert _parallel_worker_limit(100) == 8


def test_parallel_timeout_knob_is_bounded_and_malformed_safe(monkeypatch):
    from lib.tasks_pkg.tool_dispatch._pipeline import _bounded_tool_env_int

    monkeypatch.setenv('TOOL_PARALLEL_TIMEOUT', 'bad')
    assert _bounded_tool_env_int('TOOL_PARALLEL_TIMEOUT', 300, 1, 3600) == 300
    monkeypatch.setenv('TOOL_PARALLEL_TIMEOUT', '999999')
    assert _bounded_tool_env_int('TOOL_PARALLEL_TIMEOUT', 300, 1, 3600) == 3600


def test_server_zero_config_executor_defaults_are_sixteen(monkeypatch):
    from lib import server_loop_runtime

    monkeypatch.setattr(server_loop_runtime.os, 'cpu_count', lambda: 64)
    logger = server_loop_runtime.logging.getLogger(__name__)
    for key in ('TOFU_SYNC_WORKERS', 'TOFU_AGENT_WORKERS'):
        assert server_loop_runtime._worker_count(key, {}, logger) == 16


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

    # A library-specific operator choice is never clobbered by the aggregate
    # zero-config knob.
    monkeypatch.setenv('OPENBLAS_NUM_THREADS', '7')
    monkeypatch.setenv('TOFU_NUMERIC_THREADS', '2')
    assert server._install_numeric_thread_defaults() == 2
    assert os.environ['OPENBLAS_NUM_THREADS'] == '7'


def test_make_defaults_bound_process_and_nested_numeric_parallelism():
    source = (Path(__file__).parents[1] / 'Makefile').read_text()
    assert 'JOBS ?= 4' in source
    assert 'TEST_NUMERIC_THREADS ?= 1' in source
    for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS',
                 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
        assert f'{name}=$(TEST_NUMERIC_THREADS)' in source
    assert 'python -m pytest' in source
