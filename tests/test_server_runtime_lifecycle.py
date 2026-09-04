from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from lib.server_runtime_lifecycle import (
    register_runtime_lifecycle,
    resolve_runtime_endpoint,
)


pytestmark = pytest.mark.unit


def test_runtime_endpoint_prefers_explicit_values_and_bounds_invalid_ports():
    env = {
        '_TOFU_RUNTIME_HOST': 'runtime-host',
        '_TOFU_RUNTIME_PORT': '16000',
        'TOFU_HOST': 'legacy-host',
        'TOFU_PORT': '17000',
    }

    assert resolve_runtime_endpoint(environ=env) == ('runtime-host', 16000)
    assert resolve_runtime_endpoint(
        host='explicit-host', port=18000, environ=env,
    ) == ('explicit-host', 18000)
    assert resolve_runtime_endpoint(port='bad', environ=env)[1] == 15000
    assert resolve_runtime_endpoint(port=70000, environ=env)[1] == 15000


def test_runtime_lifecycle_registers_loop_before_production_with_shared_event():
    app = SimpleNamespace(extensions={})
    order: list[str] = []
    captured: dict[str, object] = {}

    def serving(target, **kwargs):
        assert target is app
        order.append('serving')
        captured['serving'] = kwargs
        target.extensions['tofu_shutdown_requested'] = kwargs[
            'shutdown_requested'
        ]
        return True

    def production(target, **kwargs):
        assert target is app
        order.append('production')
        captured['production'] = kwargs
        return True

    assert register_runtime_lifecycle(
        app,
        production_registrar=production,
        serving_registrar=serving,
        announce_ready=lambda *_args: None,
        environ={'TOFU_HOST': '127.0.0.1', 'TOFU_PORT': '15555'},
        process_role='worker',
    ) is True

    assert order == ['serving', 'production']
    serving_args = captured['serving']
    production_args = captured['production']
    assert serving_args['host'] == '127.0.0.1'
    assert serving_args['port'] == 15555
    assert serving_args['process_role'] == 'worker'
    assert isinstance(serving_args['shutdown_requested'], threading.Event)
    assert (
        serving_args['shutdown_requested']
        is production_args['shutdown_requested']
    )


def test_runtime_lifecycle_reuses_existing_shutdown_authority():
    stop_event = threading.Event()
    app = SimpleNamespace(
        extensions={'tofu_shutdown_requested': stop_event},
    )
    seen: list[object] = []

    def registrar(_app, **kwargs):
        seen.append(kwargs['shutdown_requested'])
        return False

    assert register_runtime_lifecycle(
        app,
        production_registrar=registrar,
        serving_registrar=registrar,
        environ={},
    ) is False
    assert seen == [stop_event, stop_event]
