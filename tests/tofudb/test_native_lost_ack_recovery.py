"""A real native commit survives lost ACK, daemon death and client replacement."""
from __future__ import annotations

import pytest

from lib.storage.errors import StorageError
from lib.storage.runtime import StorageRuntime
from lib.storage.supervisor import StorageSupervisor

pytestmark = pytest.mark.api


def test_retained_client_replays_lost_ack_once_on_restarted_daemon(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFUDB_TRANSPORT', 'daemon')
    monkeypatch.setenv('TOFU_DB_DURABILITY', 'strict')
    monkeypatch.setenv('TOFUDB_OWNER_ID', '1')
    monkeypatch.setenv('TOFUDB_TENANT_ID', '1')
    for name in ('TOFU_STORAGE_CONNECTION_FILE', 'TOFU_DB_FASTPATH_DIR',
                 'TOFU_DB_FASTPATH_SYNC_INTERVAL_MS'):
        monkeypatch.delenv(name, raising=False)
    supervisor = StorageSupervisor(backend='tofudb', project_root=tmp_path, startup_timeout=30)
    runtime = StorageRuntime(supervisor)
    try:
        client = runtime.start()
        original = client._attempt
        committed = []

        def commit_then_lose_ack(operation, payload, **kwargs):
            value = original(operation, payload, **kwargs)
            committed.append(value)
            process = supervisor._process
            assert process is not None
            process.kill()
            process.wait(timeout=5)
            raise StorageError('database_unavailable', 'injected lost acknowledgement', True, 1)

        monkeypatch.setattr(client, '_attempt', commit_then_lose_ack)
        payload = {'namespace': 'recovery-spec', 'key': 'single-write',
                   'value': {'retained': 'durable'}, 'user_id': 1}
        result = client.command('record.put', payload, 'lost-ack-receipt', deadline=10)
        assert len(committed) == 1
        assert result == committed[0]
        current = runtime.client()
        assert current is not client
        assert client._pool._closed
        row = current.query('record.get', {'namespace': 'recovery-spec',
                                         'key': 'single-write', 'user_id': 1})
        assert row['version'] == 1
        assert row['value'] == payload['value']
        assert not runtime.status()['degraded']
    finally:
        runtime.stop(timeout=20)
