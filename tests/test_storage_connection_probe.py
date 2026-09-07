"""Storage Sidecar Kubernetes probes keep liveness dependency-free."""

from __future__ import annotations

import pytest

from lib.storage import connection_probe


pytestmark = pytest.mark.unit


def test_liveness_reads_only_the_local_handoff(monkeypatch):
    connection = {
        'host': '127.0.0.1',
        'port': 12345,
        'token': 'x' * 48,
        'backend': 'postgres',
    }
    readiness_calls = []
    monkeypatch.setattr(connection_probe, '_read_connection', lambda: connection)
    monkeypatch.setattr(
        connection_probe,
        '_storage_is_ready',
        lambda _connection: readiness_calls.append(_connection),
    )

    assert connection_probe.main(['--liveness']) == 0
    assert readiness_calls == []


def test_readiness_keeps_the_authenticated_storage_health_check(monkeypatch):
    connection = {
        'host': '127.0.0.1',
        'port': 12345,
        'token': 'x' * 48,
        'backend': 'postgres',
    }
    monkeypatch.setattr(connection_probe, '_read_connection', lambda: connection)
    monkeypatch.setattr(
        connection_probe, '_storage_is_ready', lambda value: value is connection)

    assert connection_probe.main([]) == 0


def test_probe_modes_fail_closed_on_invalid_input_or_missing_handoff(monkeypatch):
    monkeypatch.setattr(
        connection_probe,
        '_read_connection',
        lambda: (_ for _ in ()).throw(RuntimeError('missing')),
    )

    assert connection_probe.main([]) == 2
    assert connection_probe.main(['--liveness']) == 2
    assert connection_probe.main(['--unknown']) == 2
