"""Contracts for graceful, lifecycle-owned in-place server replacement."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

import lib.server_reexec as server_reexec


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolated_reexec_coordinator():
    server_reexec._reset_server_reexec_state_for_tests()
    yield
    server_reexec._reset_server_reexec_state_for_tests()


def test_reexec_request_fences_serving_once_and_is_idempotent():
    shutdown_reasons: list[str] = []
    server_reexec.install_server_reexec_shutdown_requester(
        shutdown_reasons.append)

    assert server_reexec.begin_server_reexec('manual-update') is True
    assert server_reexec.begin_server_reexec('duplicate') is True

    assert shutdown_reasons == ['manual-update']
    assert server_reexec.pending_server_reexec_reason() == 'manual-update'


def test_reexec_refuses_when_serving_loop_bridge_is_absent():
    assert server_reexec.begin_server_reexec('unowned') is False
    assert server_reexec.confirm_server_reexec_storage_boundary_released() is False
    assert server_reexec.pending_server_reexec_reason() == ''


def test_reexec_cannot_run_before_shutdown_or_preparation(monkeypatch):
    server_reexec.install_server_reexec_shutdown_requester(lambda _reason: None)
    assert server_reexec.begin_server_reexec('test') is True
    executed: list[tuple[str, list[str]]] = []

    with pytest.raises(RuntimeError, match='completed production shutdown'):
        server_reexec.execute_pending_server_reexec(
            lifecycle_stopped=False,
            exec_function=lambda executable, arguments: executed.append(
                (executable, arguments)),
        )
    with pytest.raises(RuntimeError, match='preparation did not finish'):
        server_reexec.execute_pending_server_reexec(
            lifecycle_stopped=True,
            preparation_timeout=0,
            exec_function=lambda executable, arguments: executed.append(
                (executable, arguments)),
        )
    assert server_reexec.finish_server_reexec_preparation() is True
    with pytest.raises(RuntimeError, match='storage boundary to be released'):
        server_reexec.execute_pending_server_reexec(
            lifecycle_stopped=True,
            exec_function=lambda executable, arguments: executed.append(
                (executable, arguments)),
        )
    assert executed == []


def test_reexec_after_shutdown_preserves_endpoint_and_exec_contract(monkeypatch):
    server_reexec.install_server_reexec_shutdown_requester(lambda _reason: None)
    assert server_reexec.begin_server_reexec('auto-head-change') is True
    assert server_reexec.finish_server_reexec_preparation() is True
    assert (
        server_reexec.confirm_server_reexec_storage_boundary_released() is True
    )
    monkeypatch.setenv('_TOFU_RUNTIME_PORT', '15000')
    monkeypatch.setenv('_TOFU_ENV_REEXEC', '1')
    monkeypatch.delenv('_TOFU_REEXEC_PORT', raising=False)
    monkeypatch.setattr(
        server_reexec,
        '_clear_inheritable_file_descriptors',
        lambda _logger: 0,
    )
    executed: list[tuple[str, list[str]]] = []

    assert server_reexec.execute_pending_server_reexec(
        lifecycle_stopped=True,
        exec_function=lambda executable, arguments: executed.append(
            (executable, arguments)),
    ) is True

    assert executed == [(sys.executable, [sys.executable, *sys.argv])]
    assert '_TOFU_ENV_REEXEC' not in server_reexec.os.environ
    assert server_reexec.os.environ['_TOFU_REEXEC_PORT'] == '15000'
    assert server_reexec.pending_server_reexec_reason() == ''


def test_server_executes_pending_restart_only_after_serve_returns():
    source = (ROOT / 'server.py').read_text(encoding='utf-8')
    install_at = source.index('install_server_reexec_shutdown_requester(')
    serve_at = source.index('asyncio.run(_serve())')
    execute_at = source.index('execute_pending_server_reexec(')

    assert install_at < serve_at < execute_at
