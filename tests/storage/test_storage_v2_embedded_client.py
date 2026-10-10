"""Embedded-transport client and supervisor wiring.

Unit tests drive ``EmbeddedStorageV2Client`` with a fake runtime so envelope
mapping, classified retry, and the committed-event unwrap are exercised
without a native build. The integration tests run the real supervisor in
embedded mode against the checkout's release engine and prove the
no-socket invariant plus the graceful restart cycle.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lib.storage.errors import StorageError
from lib.storage.v2_embedded_client import EmbeddedStorageV2Client
from tofu_db.embedded import EmbeddedTransportError

# serial: embedded-engine lifecycle (spawn/health/restart) cannot overlap a
# parallel lane without starving the engine startup budget.
pytestmark = [pytest.mark.api, pytest.mark.serial]

_SECRET = b's' * 48


class _FakeRuntime:
    """Scripted stand-in for tofu_db.embedded.EmbeddedRuntime."""

    def __init__(self):
        self.responses: list = []
        self.seen: list[dict] = []
        self.closed = False
        self.close_calls = 0

    def execute_json(self, operation, payload, *, command_id, deadline_unix_ms):
        if self.closed:
            raise EmbeddedTransportError('embedded runtime is closed')
        self.seen.append({
            'operation': operation,
            'payload': dict(payload or {}),
            'command_id': command_id,
        })
        outcome = self.responses.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def close(self):
        self.closed = True
        self.close_calls += 1


def _ok(payload) -> dict:
    return {
        'status': 0,
        'error_code': None,
        'error_message': None,
        'retryable': False,
        'payload': payload,
    }


def _error(code: str, *, retryable: bool = False, message: str = 'boom') -> dict:
    return {
        'status': 1,
        'error_code': code,
        'error_message': message,
        'retryable': retryable,
        'payload': None,
    }


def _client(runtime: _FakeRuntime) -> EmbeddedStorageV2Client:
    return EmbeddedStorageV2Client(
        runtime, _SECRET, owner_id=7, tenant_id=3)


@pytest.mark.unit
class TestEmbeddedClientAdapter:
    def test_constructor_validation_inherits_the_loopback_contract(self) -> None:
        runtime = _FakeRuntime()
        with pytest.raises(ValueError):
            EmbeddedStorageV2Client(runtime, b'short', owner_id=1, tenant_id=1)
        with pytest.raises(ValueError):
            EmbeddedStorageV2Client(runtime, _SECRET, owner_id=0, tenant_id=1)
        assert _client(runtime).endpoint == ('embedded', 0)

    def test_query_maps_the_envelope_payload_and_injects_owner(self) -> None:
        runtime = _FakeRuntime()
        runtime.responses = [_ok({'count': 4})]
        result = _client(runtime).query('conversation.count', {}, deadline=5.0)
        assert result == {'count': 4}
        assert runtime.seen[0]['payload']['user_id'] == 7
        assert runtime.seen[0]['command_id'] is None

    def test_engine_error_envelope_uses_the_stable_taxonomy(self) -> None:
        runtime = _FakeRuntime()
        # deadline_elapsed is a classified transient: a read retries it, so
        # the scripted runtime answers the full read-attempt budget.
        runtime.responses = [_error('deadline_elapsed')] * 3
        with pytest.raises(StorageError) as captured:
            _client(runtime).query('conversation.count', {}, deadline=0.05)
        assert captured.value.code == 'database_timeout'

    def test_busy_command_retries_with_the_same_command_id(self) -> None:
        runtime = _FakeRuntime()
        runtime.responses = [
            _error('database_busy', retryable=True),
            _ok({'inserted': True}),
        ]
        result = _client(runtime).command(
            'record.put', {'namespace': 'embed', 'key': 'k', 'value': 1}, 'cmd-1')
        assert result == {'inserted': True}
        assert [seen['command_id'] for seen in runtime.seen] == ['cmd-1', 'cmd-1']

    def test_transport_error_classifies_as_retryable_unavailable(self) -> None:
        runtime = _FakeRuntime()
        runtime.closed = True
        with pytest.raises(StorageError) as captured:
            _client(runtime).query('conversation.count', {}, deadline=0.05)
        assert captured.value.code == 'database_unavailable'
        assert captured.value.retryable is True

    def test_ffi_error_leg_classifies_as_retryable_unavailable(self) -> None:
        runtime = _FakeRuntime()
        runtime.responses = [{
            'status': -1,
            'error_code': 'embedded_transport_error',
            'error_message': 'handle failure',
            'retryable': False,
            'payload': None,
        }] * 3
        with pytest.raises(StorageError) as captured:
            _client(runtime).query('conversation.count', {}, deadline=0.05)
        assert captured.value.code == 'database_unavailable'
        assert captured.value.retryable is True

    def test_command_unwraps_the_committed_event_envelope(self) -> None:
        from tofu_db.generated_v2 import COMMITTED_EVENTS_CONTRACT

        runtime = _FakeRuntime()
        runtime.responses = [_ok({
            '_storageCommitContract': COMMITTED_EVENTS_CONTRACT,
            'value': {'inserted': True},
            'events': [],
        })]
        result = _client(runtime).command(
            'record.put', {'namespace': 'embed', 'key': 'k', 'value': 1}, 'cmd-2')
        assert result == {'inserted': True}

    def test_health_reports_ready_and_forwards_the_resource_budget(self) -> None:
        runtime = _FakeRuntime()
        budget = {'maximumConnections': 8, 'memoryCapacityBytes': 4096}
        runtime.responses = [_ok({
            'contract': 'tofu-db.system-health.v1',
            'ready': True,
            'walDurability': 'strict',
            'resourceBudget': budget,
        })]
        client = _client(runtime)
        health = client.health(deadline=1.0)
        assert health['ready'] is True
        assert health['backend'] == 'tofudb'
        assert health['protocol'] == 'storage.v2'
        assert health['owner_id'] == 7
        assert health['tenant_id'] == 3
        assert health['resourceBudget'] == budget

    def test_client_close_retires_the_pool_but_not_the_runtime(self) -> None:
        runtime = _FakeRuntime()
        client = _client(runtime)
        client.close()
        assert runtime.close_calls == 0
        assert runtime.closed is False
        with pytest.raises(StorageError):
            client.query('conversation.count', {}, deadline=0.05)


def _socket_inodes() -> set[str]:
    inodes: set[str] = set()
    for fd in Path('/proc/self/fd').iterdir():
        try:
            target = os.readlink(fd)
        except OSError:
            continue
        if target.startswith('socket:['):
            inodes.add(target)
    return inodes


@pytest.mark.api
class TestEmbeddedSupervisor:
    """Real supervisor over the checkout's release engine, embedded mode."""

    def _start(self, tmp_path, monkeypatch):
        from lib.storage import StorageRuntime, StorageSupervisor

        monkeypatch.setenv('TOFUDB_TRANSPORT', 'embedded')
        monkeypatch.setenv('TOFUDB_OWNER_ID', '1')
        monkeypatch.setenv('TOFUDB_TENANT_ID', '1')
        runtime = StorageRuntime(StorageSupervisor(
            project_root=tmp_path, backend='tofudb', startup_timeout=60),
            auto_restart=False)
        runtime.start()
        return runtime

    def test_start_health_round_trip_and_restart_without_sockets(
            self, tmp_path, monkeypatch):
        sockets_before = _socket_inodes()
        runtime = self._start(tmp_path, monkeypatch)
        try:
            client = runtime.client()
            health = client.health(deadline=2.0)
            assert health['ready'] is True
            assert health['backend'] == 'tofudb'
            assert health['resourceBudget']['maximumConnections'] >= 1
            status = runtime.status()
            assert status['backend'] == 'tofudb'
            assert status['transport'] == 'embedded'
            assert status['port'] is None
            key = f'k-{os.getpid()}'
            client.command('record.put', {
                'namespace': 'embed_smoke', 'key': key, 'value': {'n': 1},
            }, f'embedded-put-{key}')
            # No new socket may appear at any point of the embedded path.
            assert _socket_inodes() <= sockets_before
        finally:
            runtime.stop()
        assert runtime.status()['state'] == 'stopped'
        runtime.start()
        try:
            row = runtime.client().query('record.get', {
                'namespace': 'embed_smoke', 'key': key, 'user_id': 1,
            })
            assert row['value'] == {'n': 1}
            assert _socket_inodes() <= sockets_before
        finally:
            runtime.stop()

    def test_normal_durability_is_refused_fail_closed(
            self, tmp_path, monkeypatch):
        monkeypatch.setenv('TOFU_DB_DURABILITY', 'normal')
        with pytest.raises(RuntimeError, match='strict durability only'):
            self._start(tmp_path, monkeypatch)
