"""Native SQL application admission, initialization races and ambiguous TCP ACK."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import errno
import json
from pathlib import Path
import threading
import time

import pytest

from lib.native_binary import NativeConfigurationError
from lib.storage.errors import StorageError
from lib.storage_authority_sql.native_checkpoint_config import NativeCheckpointConfiguration
from tests.storage_authority_sql.test_logical_session_contract import pipeline
from tests.storage_authority_sql.test_native_checkpoint_rpc import configuration, serving
from tests.storage_authority_sql.test_native_checkpoint_storage import backend, acquire, call, rows, snapshot

pytestmark = pytest.mark.api


@pytest.mark.parametrize('damage', ['extra', 'owner', 'document', 'identity', 'relative', 'digest', 'oversize'])
def test_explicit_configuration_rejects_invalid_scope_and_budgets(monkeypatch, damage):
    from lib.storage_authority_sql.generated_checkpoint_storage_contract import SPEC
    configuration = NativeCheckpointConfiguration(Path('/explicit/pinned/image.so'), 'a'*64, 4096, 3, 7, 1 << 20, 1024)
    policy = SPEC['application']
    value = dict(asdict(configuration), format=policy['configuration_format'], library_path=str(configuration.library_path))
    changes = {'extra': {'unknown': 1}, 'owner': {'owner_user_id': True},
        'document': {'document_bytes': (8 << 20)+1}, 'identity': {'identity_bytes': 0},
        'relative': {'library_path': 'relative.so'}, 'digest': {'sha256': 'Z'*64}}
    value.update(changes.get(damage, {}))
    monkeypatch.setenv(policy['environment'], json.dumps(value) if damage != 'oversize' else ' '*(policy['configuration_bytes']+1))
    with pytest.raises(NativeConfigurationError):
        NativeCheckpointConfiguration.from_environment()


def test_bad_image_pin_is_sticky_and_never_advertises_available(serving, configuration, backend, monkeypatch):
    from lib.storage_authority_sql import native_checkpoint
    original, admissions = native_checkpoint._seal, []
    def observe(*args, **kwargs):
        admissions.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(native_checkpoint, '_seal', observe)
    server, client, _ = serving(config=replace(configuration, sha256='0'*64))
    for _ in range(2):
        with pytest.raises(StorageError) as caught:
            client.native_checkpoint_available(user_id=7)
        assert caught.value.code == 'database_unavailable'
    assert admissions == [1] and server.native_checkpoint._controller is None and rows(backend) == []


def test_close_joins_in_progress_image_admission(serving, backend, monkeypatch):
    from lib.storage_authority_sql import native_checkpoint
    original = native_checkpoint._seal
    entered, release, closing = threading.Event(), threading.Event(), threading.Event()
    def hold(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(native_checkpoint, '_seal', hold)
    server, client, _ = serving()
    def close():
        closing.set()
        server.server_close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        admission = pool.submit(client.native_checkpoint_available, user_id=7)
        assert entered.wait(5)
        stopped = pool.submit(close)
        try:
            assert closing.wait(5)
            with server.native_checkpoint._condition:
                assert server.native_checkpoint._condition.wait_for(lambda: server.native_checkpoint._closed, timeout=5)
            assert not stopped.done()
        finally:
            release.set()
        stopped.result(timeout=10)
        with pytest.raises(StorageError) as caught:
            admission.result(timeout=10)
        assert caught.value.code == 'database_unavailable'
    assert server.native_checkpoint._controller._state == 'closed' and rows(backend) == []


def test_actual_tcp_reply_loss_does_not_replay_committed_control(serving, backend, monkeypatch):
    from lib.storage_authority_sql import server as transport
    from tofu_db.generated_checkpoint_control import CONTROL_OPERATION
    _, client, port = serving()
    cap = acquire(port)
    call(port, 'load', cap)
    assert call(port, 'begin', cap, round=0)['accepted']
    original, replies = transport._StorageHandler._send_response, []
    def drop(self, response, request_id, operation):
        if operation == CONTROL_OPERATION:
            replies.append(response)
            return False  # Real socketserver shutdown closes before any response bytes.
        return original(self, response, request_id, operation)
    monkeypatch.setattr(transport._StorageHandler, '_send_response', drop)
    with pytest.raises(StorageError):
        call(port, 'complete', cap, state=snapshot())
    assert len(replies) == 1 and replies[0]['ok'] and replies[0]['result']['accepted']
    assert len(rows(backend)) == 2
    assert client.transport_metrics()['pre_dispatch_command_retries'] == 0


def test_rpc_capture_uses_one_native_outbox_transaction(serving, backend, pipeline):
    from tests.storage_authority_sql.test_logical_session_contract import published
    _, _, port = serving(pipeline=pipeline)
    cap = acquire(port)
    assert call(port, 'load', cap)['accepted']
    assert call(port, 'begin', cap, round=0)['accepted']
    assert call(port, 'complete', cap, state=snapshot())['accepted']
    records = published(pipeline)
    assert len(records) == 3 and len(rows(backend)) == 2
    assert all(row['tenant_id'] == '3' and row['owner_user_id'] == 7 for row in records)


def test_bind_failure_retains_original_error_and_closes_partial_service(serving, backend, monkeypatch):
    from lib.storage_authority_sql.server import _StorageTCPServer, _StorageHandler
    server, _, _ = serving()
    monkeypatch.setattr('lib.storage_authority_sql.server.bind_ephemeral_loopback',
                        lambda sock: sock.bind(server.server_address))
    with pytest.raises(OSError) as caught:
        _StorageTCPServer(server.server_address, _StorageHandler, backend=backend,
                          token=server.token, rpc_capacity=8, read_only_preview=False)
    assert caught.value.errno == errno.EADDRINUSE


@pytest.mark.parametrize('damage', ['auth', 'receipt', 'kind', 'unknown', 'owner', 'operation_type', 'stale'])
def test_wire_admission_refuses_before_code_or_durable_effects(serving, backend, damage):
    from tofu_db.generated_checkpoint_control import CAPABILITIES_OPERATION, CONTROL_OPERATION, TRANSPORT_FORMAT
    server, client, _ = serving()
    operation, kind, receipt = CAPABILITIES_OPERATION, 'query', None
    payload = {'owner_user_id': 7}
    if damage == 'auth': client._token = 'invalid-token-'*4
    if damage == 'receipt': receipt = 'forbidden-receipt'
    if damage == 'kind': kind = 'command'
    if damage == 'unknown': operation = 'native_checkpoint.unknown'
    if damage == 'owner': payload['owner_user_id'] = 8
    if damage in {'operation_type', 'stale', 'receipt'}:
        operation, kind = CONTROL_OPERATION, 'command'
        payload.update(format=TRANSPORT_FORMAT, runtime_id=server.native_checkpoint.runtime_id,
                       operation='create', payload={}, response_bytes=4096)
        if damage == 'operation_type': payload['operation'] = []
        elif damage == 'stale': payload['runtime_id'] = '550e8400-e29b-41d4-a716-446655440000'
    with pytest.raises(StorageError) as caught:
        client._attempt(kind, operation, payload, timeout=5, command_id=receipt, priority='user',
                        deadline_unix_ms=int((time.time()+5)*1000))
    assert caught.value.code == ('database_forbidden' if damage == 'owner' else 'database_protocol_error')
    assert server.native_checkpoint._controller is None and rows(backend) == []
