"""Hermetic storage.v2 client streaming contract tests."""

from __future__ import annotations

import json
import socket
import threading

import pytest

from tofu_db.wire import (
    FIELD_CHUNK_INDEX,
    FIELD_COMMAND_ID,
    FIELD_CORRELATION_ID,
    FIELD_ERROR_CODE,
    FIELD_ERROR_MESSAGE,
    FIELD_FINAL,
    FIELD_KIND,
    FIELD_OPERATION,
    FIELD_PAYLOAD,
    FIELD_PROTOCOL_VERSION,
    FIELD_RETRYABLE,
    FIELD_SCHEMA_ID,
    FIELD_STATUS,
    FIELD_STREAM_ID,
    FIELD_TOTAL_PAYLOAD_BYTES,
    KIND_BLOB_CHUNK,
    KIND_REQUEST,
    KIND_RESPONSE,
    KIND_RESPONSE_CHUNK,
    MAX_BLOB_CHUNK_BYTES,
    MAX_PAYLOAD_BYTES,
    PROTOCOL_VERSION,
    STORAGE_SCHEMA_VERSION,
    StorageV2ConnectionClosedError,
    StorageV2ProtocolError,
    StorageV2Session,
    encode_message,
    pack_frame,
    recv_frame,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("operation,command_id", [
    ("tool_result_artifact.put", "large-tool-result"),
    ("swarm.agent.save", "large-agent"),
    ("turn.projection.update", "large-turn"),
    ("raw_archive.put", "large-archive"),
    ("plugin.row.put", "large-plugin"),
    ("record.get", None),
])
def test_client_streams_large_request_and_reassembles_large_response(operation, command_id) -> None:
    client_socket, peer_socket = socket.socketpair()
    client_socket.settimeout(10)
    peer_socket.settimeout(10)
    session = object.__new__(StorageV2Session)
    session._owner_id = 11
    session._tenant_id = 7
    session._counter = 1
    session._sock = client_socket
    session.schema_id = STORAGE_SCHEMA_VERSION
    # No shared process response-byte budget in this hermetic socketpair
    # harness — ``None`` selects the historical standalone-transport path.
    session._response_admission = None

    expected_content = "x" * MAX_PAYLOAD_BYTES
    expected_response = b"y" * (MAX_BLOB_CHUNK_BYTES * 2 + 7)
    peer_failures: list[BaseException] = []

    def peer() -> None:
        try:
            chunks: list[bytes] = []
            correlation: bytes | None = None
            stream_id: bytes | None = None
            saw_final = False
            while True:
                message = recv_frame(peer_socket)
                if message[FIELD_KIND] == KIND_BLOB_CHUNK:
                    assert not saw_final
                    if correlation is None:
                        correlation = message[FIELD_CORRELATION_ID]
                        stream_id = message[FIELD_STREAM_ID]
                    assert message[FIELD_CORRELATION_ID] == correlation
                    assert message[FIELD_STREAM_ID] == stream_id
                    assert message[FIELD_CHUNK_INDEX] == len(chunks)
                    assert message[FIELD_TOTAL_PAYLOAD_BYTES] > MAX_PAYLOAD_BYTES
                    saw_final = bool(message[FIELD_FINAL])
                    chunks.append(message[FIELD_PAYLOAD])
                    continue
                assert message[FIELD_KIND] == KIND_REQUEST
                assert saw_final
                assert message[FIELD_CORRELATION_ID] == correlation
                assert message[FIELD_PAYLOAD] == b""
                assert message[FIELD_OPERATION] == operation
                assert message[FIELD_COMMAND_ID] == command_id
                request_payload = json.loads(b"".join(chunks).decode("utf-8"))
                assert request_payload["content"] == expected_content
                break

            response_chunks = [
                expected_response[offset : offset + MAX_BLOB_CHUNK_BYTES]
                for offset in range(0, len(expected_response), MAX_BLOB_CHUNK_BYTES)
            ]
            for index, chunk in enumerate(response_chunks):
                body = encode_message({
                    FIELD_KIND: KIND_RESPONSE_CHUNK,
                    FIELD_PROTOCOL_VERSION: PROTOCOL_VERSION,
                    FIELD_CORRELATION_ID: correlation,
                    FIELD_SCHEMA_ID: STORAGE_SCHEMA_VERSION,
                    FIELD_PAYLOAD: chunk,
                    FIELD_STREAM_ID: correlation,
                    FIELD_CHUNK_INDEX: index,
                    FIELD_FINAL: index + 1 == len(response_chunks),
                })
                peer_socket.sendall(pack_frame(body))
            terminator = encode_message({
                FIELD_KIND: KIND_RESPONSE,
                FIELD_PROTOCOL_VERSION: PROTOCOL_VERSION,
                FIELD_CORRELATION_ID: correlation,
                FIELD_SCHEMA_ID: STORAGE_SCHEMA_VERSION,
                FIELD_PAYLOAD: b"",
                FIELD_STATUS: 0,
                FIELD_ERROR_CODE: None,
                FIELD_ERROR_MESSAGE: None,
                FIELD_RETRYABLE: False,
            })
            peer_socket.sendall(pack_frame(terminator))
        except BaseException as error:  # surface peer-thread assertions
            peer_failures.append(error)
            raise  # immediate traceback; main thread also asserts the ferry
        finally:
            peer_socket.close()

    peer_thread = threading.Thread(target=peer, daemon=True)
    peer_thread.start()
    try:
        response = session.request(
            operation,
            {
                "user_id": 11,
                "content": expected_content,
                "media_type": "text/plain",
                "created_at_ms": 1,
                "expires_at_ms": 2,
            },
            command_id=command_id,
            deadline_unix_ms=10_000,
        )
        assert response.status == 0
        assert response.payload == expected_response
    finally:
        session.close()
        peer_thread.join(timeout=10)
    assert not peer_thread.is_alive()
    assert not peer_failures


def test_optional_json_encoder_preserves_exact_payload_bytes(monkeypatch):
    import tofu_db.wire as client
    accelerator = pytest.importorskip('orjson')
    monkeypatch.setattr(client, '_fast_json', accelerator)
    padding = '界"\\\n\x00' * 2048
    values = [
        {'data': 'cGRm' * 32768, 'sequence': 0},
        {'data': padding, 'values': [None, True, False, 0, -1, -(1 << 63), (1 << 64) - 1]},
        {'data': padding, 'nested': {'tuple': ('text', 1), 'list': [[], {}]}},
        {'data': padding, 'float': 1e-5},
        {'data': padding, 'nan': float('nan')},
        {'data': padding, 'large_integer': 1 << 80},
        {'data': padding, 'non_string_keys': {1: 'one'}},
        {'data': padding, 'wide': [0] * (client._JSON_ACCELERATOR_MAX_VALUES + 1)},
        {'small': 'payload'},
    ]
    nested = {'data': padding}
    for _ in range(client._JSON_ACCELERATOR_MAX_DEPTH + 1):
        nested = {'nested': nested}
    values.append({'data': padding, 'nested': nested})
    for value in values:
        assert client._payload_bytes(value) == client._stdlib_payload_bytes(value)
    monkeypatch.setattr(client, '_fast_json', None)
    assert client._payload_bytes(values[0]) == client._stdlib_payload_bytes(values[0])


def test_optional_json_encoder_preserves_invalid_input_failures(monkeypatch):
    import tofu_db.wire as client
    from dataclasses import dataclass
    accelerator = pytest.importorskip('orjson')
    monkeypatch.setattr(client, '_fast_json', accelerator)

    @dataclass
    class Unregistered:
        value: str

    cycle = []
    cycle.append(cycle)
    for value in [{'data': 'x' * 9000, 'custom': Unregistered('x')},
                  {'data': 'x' * 9000 + '\ud800'}, {'data': 'x' * 9000, 'cycle': cycle}]:
        with pytest.raises((TypeError, ValueError, UnicodeEncodeError)) as original:
            client._stdlib_payload_bytes(value)
        with pytest.raises(type(original.value)):
            client._payload_bytes(value)


def test_optional_json_encoder_uses_fast_path_for_large_text(monkeypatch):
    import tofu_db.wire as client
    accelerator = pytest.importorskip('orjson')
    calls = []

    class ObservedEncoder:
        JSONEncodeError = accelerator.JSONEncodeError

        @staticmethod
        def dumps(value):
            calls.append(value)
            return accelerator.dumps(value)

    monkeypatch.setattr(client, '_fast_json', ObservedEncoder)
    payload = {'data': 'cGRm' * 32768, 'sequence': 1}
    assert client._payload_bytes(payload) == client._stdlib_payload_bytes(payload)
    assert len(calls) == 1
    client._payload_bytes({'data': 'x' * 9000, 'float': float('inf')})
    assert len(calls) == 1


def test_nested_large_projection_uses_exact_fast_encoding(monkeypatch):
    import tofu_db.wire as client
    import orjson
    calls = []

    class Encoder:
        JSONEncodeError = orjson.JSONEncodeError

        @staticmethod
        def dumps(value):
            calls.append(True)
            return orjson.dumps(value)

    monkeypatch.setattr(client, '_fast_json', Encoder)
    payload = {'conversation_id': 'chat', 'projection': {
        'content': '工具输出\n"\\🙂' * 4000}, 'user_id': 11}
    assert client._payload_bytes(payload) == client._stdlib_payload_bytes(payload)
    assert calls == [True]
    client._payload_bytes({'projection': {'content': 'small'}})
    assert calls == [True]


@pytest.mark.parametrize('accelerated', [True, False])
def test_large_response_decoder_preserves_numbers_unicode_and_error_types(monkeypatch, accelerated):
    import json
    import tofu_db.wire as client
    import orjson
    monkeypatch.setattr(client, '_fast_json', orjson if accelerated else None)
    padding = json.dumps('工具输出\n"\\🙂' * 4000).encode()
    suffixes = [b'0', b'-0.0', b'1.2345678901234567', b'1e309', b'NaN',
                b'18446744073709551615', b'1208925819614629174706177',
                b'-1208925819614629174706177', b'"\\ud800"', b'[null,true,false]']
    for value in suffixes:
        encoded = b'{"text":' + padding + b',"value":' + value + b'}'
        expected = json.loads(encoded.decode('utf-8'))
        for body in [encoded, bytearray(encoded)]:
            result = client.StorageV2Response(0, 0, body, None, None, False).json()
            assert json.dumps(result, sort_keys=True) == json.dumps(expected, sort_keys=True)
    for encoded in [b'{"text":' + padding + b',"bad":}',
                    b'{"text":' + padding + b',"bad":"\xff"}']:
        with pytest.raises((UnicodeDecodeError, json.JSONDecodeError)) as original:
            json.loads(encoded.decode('utf-8'))
        with pytest.raises(type(original.value)):
            client.StorageV2Response(0, 0, encoded, None, None, False).json()


def test_large_response_decoder_avoids_stdlib_only_for_exact_supported_shapes(monkeypatch):
    import json
    import tofu_db.wire as client
    import orjson
    monkeypatch.setattr(client, '_fast_json', orjson)
    original = json.loads
    calls = []

    def observed(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(client.json, 'loads', observed)
    payload = {'projection': {'content': 'x' * 16000}, 'rev': 42}
    assert client._response_json(json.dumps(payload).encode()) == payload
    assert not calls
    payload['unsafe_integer'] = (1 << 80) + 1
    assert client._response_json(json.dumps(payload).encode()) == payload
    assert len(calls) == 1


def _histogram_client():
    from lib.storage.v2_client import StorageV2Client
    return StorageV2Client(
        '127.0.0.1', 1, b's' * 32, owner_id=11, tenant_id=7,
        timeout=0.5, read_attempts=3)


def _capture_histogram(monkeypatch):
    import lib.observability as observability
    calls = []
    monkeypatch.setattr(
        observability, 'record_storage_rpc',
        lambda kind, operation, seconds, outcome='success': calls.append(
            (kind, operation, seconds, outcome)))
    return calls


def test_v2_client_records_storage_rpc_histogram_success(monkeypatch):
    client = _histogram_client()
    calls = _capture_histogram(monkeypatch)
    monkeypatch.setattr(client, '_attempt', lambda *a, **k: {'ok': True})
    assert client.query('conversation.list', {}) == {'ok': True}
    assert len(calls) == 1
    kind, operation, seconds, outcome = calls[0]
    assert kind == 'query'
    assert operation == 'conversation.list'
    assert seconds >= 0.0
    assert outcome == 'success'


def test_v2_client_records_storage_rpc_histogram_error(monkeypatch):
    from lib.storage.errors import StorageError
    client = _histogram_client()
    calls = _capture_histogram(monkeypatch)

    def fail(*a, **k):
        raise StorageError('database_internal', 'boom', False)

    monkeypatch.setattr(client, '_attempt', fail)
    with pytest.raises(StorageError):
        client.query('conversation.list', {})
    assert len(calls) == 1
    assert calls[0][3] == 'error'


def test_v2_client_records_one_histogram_sample_per_logical_call(monkeypatch):
    """Retries stay inside one sample, matching the v1 client semantics."""
    from lib.storage.errors import StorageError
    client = _histogram_client()
    calls = _capture_histogram(monkeypatch)
    attempts = []

    def flaky(*a, **k):
        attempts.append(True)
        if len(attempts) == 1:
            raise StorageError('database_busy', 'transient', True, 1)
        return {'ok': True}

    monkeypatch.setattr(client, '_attempt', flaky)
    monkeypatch.setattr('lib.storage.v2_client.time.sleep', lambda _: None)
    assert client.query('conversation.list', {}) == {'ok': True}
    assert len(attempts) == 2
    assert len(calls) == 1
    assert calls[0][3] == 'success'


def test_v2_client_histogram_recording_never_breaks_the_rpc_path(monkeypatch):
    client = _histogram_client()
    import lib.observability as observability

    def explode(*a, **k):
        raise RuntimeError('metrics sink down')

    monkeypatch.setattr(observability, 'record_storage_rpc', explode)
    monkeypatch.setattr(client, '_attempt', lambda *a, **k: {'ok': True})
    assert client.query('conversation.list', {}) == {'ok': True}


def test_v2_client_health_prefers_the_daemon_control_operation(monkeypatch):
    client = _histogram_client()
    client.set_readiness_budget({'memoryCapacityBytes': 1})
    daemon_payload = {
        'contract': 'tofu-db.system-health.v1',
        'backend': 'tofudb',
        'protocol': 2,
        'ready': True,
        'components': {'listener': {'configured': True, 'healthy': True}},
        'walDurability': 'strict',
        'resourceBudget': {'memoryCapacityBytes': 4096},
    }
    calls = []

    def fake_call(operation, payload, *, deadline):
        calls.append(operation)
        assert operation == 'system.health'
        return daemon_payload

    monkeypatch.setattr(client, '_health_call', fake_call)
    health = client.health()
    assert calls == ['system.health']
    assert health['ready'] is True
    assert health['protocol'] == 'storage.v2'
    assert health['backend'] == 'tofudb'
    assert health['owner_id'] == 11
    assert health['contract'] == 'tofu-db.system-health.v1'
    assert health['walDurability'] == 'strict'
    assert health['resourceBudget'] == {'memoryCapacityBytes': 4096}


def test_v2_client_health_falls_back_to_a_semantic_probe_on_old_binaries(monkeypatch):
    from lib.storage.errors import StorageError
    client = _histogram_client()
    calls = []

    def fake_call(operation, payload, *, deadline):
        calls.append(operation)
        if operation == 'system.health':
            raise StorageError('unknown_operation', 'old binary', False)
        return {'count': 0}

    monkeypatch.setattr(client, '_health_call', fake_call)
    health = client.health()
    assert calls == ['system.health', 'conversation.count']
    assert health['ready'] is True
    assert health['protocol'] == 'storage.v2'


def test_v2_client_health_reports_not_ready_when_the_daemon_is_sick(monkeypatch):
    from lib.storage.errors import StorageError
    client = _histogram_client()

    def fake_call(operation, payload, *, deadline):
        raise StorageError('database_unavailable', 'down', True, 100)

    monkeypatch.setattr(client, '_health_call', fake_call)
    health = client.health()
    assert health['ready'] is False


def test_v2_client_metrics_prefers_the_daemon_control_operation(monkeypatch):
    client = _histogram_client()
    daemon_payload = {
        'contract': 'tofu-db.system-metrics.v1',
        'backend': 'tofudb',
        'protocol': 2,
        'listener': {'acceptedConnections': 3},
        'commitStages': [{'stage': 'wal', 'calls': 2, 'totalNanos': 1000}],
    }
    monkeypatch.setattr(
        client, '_call', lambda *a, **k: daemon_payload)
    metrics = client.metrics()
    assert metrics['contract'] == 'tofu-db.system-metrics.v1'
    assert metrics['protocol'] == 'storage.v2'
    assert metrics['backend'] == 'tofudb'
    assert metrics['listener'] == {'acceptedConnections': 3}


def test_v2_client_metrics_degrades_to_the_synthetic_envelope(monkeypatch):
    from lib.storage.errors import StorageError
    client = _histogram_client()
    client.set_readiness_budget({'memoryCapacityBytes': 1})

    def fake_call(kind, operation, payload, *, deadline, command_id=None):
        raise StorageError('unknown_operation', 'old binary', False)

    monkeypatch.setattr(client, '_call', fake_call)
    metrics = client.metrics()
    assert metrics == {
        'backend': 'tofudb',
        'protocol': 'storage.v2',
        'resourceBudget': {'memoryCapacityBytes': 1},
    }


def test_v2_client_retries_a_busy_command_with_the_same_command_id(monkeypatch):
    """Rotation backpressure is pre-mutation: same-id retry cannot double-apply."""
    from lib.storage.errors import StorageError
    client = _histogram_client()
    attempts = []

    def busy_once(operation, payload, *, timeout, command_id, deadline_unix_ms):
        attempts.append(command_id)
        if len(attempts) == 1:
            raise StorageError('database_busy', 'rotation in flight', True, 1)
        return {'ok': True}

    monkeypatch.setattr(client, '_attempt', busy_once)
    monkeypatch.setattr('lib.storage.v2_client.time.sleep', lambda _: None)
    result = client.command('record.put', {'key': 'k', 'value': 1}, command_id=None)
    assert result == {'ok': True}
    assert len(attempts) == 2
    assert attempts[0] == attempts[1]
    assert attempts[0].startswith('record.put:')


def test_v2_client_retries_ambiguous_command_failures_with_same_receipt(monkeypatch):
    from lib.storage.errors import StorageError
    client = _histogram_client()
    attempts = []

    def unavailable(operation, payload, *, timeout, command_id, deadline_unix_ms):
        attempts.append(command_id)
        raise StorageError('database_unavailable', 'lost ack', True, 1)

    monkeypatch.setattr(client, '_attempt', unavailable)
    monkeypatch.setattr('lib.storage.v2_client.time.sleep', lambda _: None)
    with pytest.raises(StorageError):
        client.command('record.put', {'key': 'k', 'value': 1}, command_id=None)
    assert len(attempts) == 3
    assert len(set(attempts)) == 1


def test_v2_client_busy_command_budget_absorbs_sustained_backpressure(monkeypatch):
    from lib.storage.errors import StorageError
    client = _histogram_client()
    attempts = []

    def busy_five_times(operation, payload, *, timeout, command_id, deadline_unix_ms):
        attempts.append(command_id)
        if len(attempts) <= 5:
            raise StorageError('database_busy', 'rotation in flight', True, 1)
        return {'ok': True}

    monkeypatch.setattr(client, '_attempt', busy_five_times)
    monkeypatch.setattr('lib.storage.v2_client.time.sleep', lambda _: None)
    assert client.command('record.put', {'key': 'k', 'value': 1},
                          command_id=None) == {'ok': True}
    assert len(attempts) == 6
    assert len(set(attempts)) == 1


def test_v2_client_busy_command_budget_is_bounded(monkeypatch):
    import lib.storage.v2_client as implementation
    from lib.storage.errors import StorageError
    client = _histogram_client()
    attempts = []
    clock = [1000.0]
    budget = max(client._timeout * client._read_attempts,
                 implementation._COMMAND_MIN_RETRY_BUDGET_S)

    def busy_forever(operation, payload, *, timeout, command_id, deadline_unix_ms):
        attempts.append(command_id)
        assert 0 < timeout <= client._timeout
        assert clock[0] < 1000.0 + budget
        clock[0] += min(timeout, 0.01)
        raise StorageError('database_busy', 'rotation in flight', True, 1)

    monkeypatch.setattr(client, '_attempt', busy_forever)
    monkeypatch.setattr(implementation.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(implementation.time, 'sleep',
                        lambda delay: clock.__setitem__(0, clock[0] + delay))
    monkeypatch.setattr(implementation.random, 'uniform', lambda *_: 0)
    with pytest.raises(StorageError, match='rotation in flight'):
        client.command('record.put', {'key': 'k', 'value': 1}, command_id=None)
    assert budget - 0.05 <= clock[0] - 1000.0 <= budget + 0.01
    assert len(attempts) > 16
    assert len(set(attempts)) == 1


# ── Connection-close classification and session liveness ──────────────────
def test_recv_frame_classifies_clean_close_at_a_frame_boundary() -> None:
    client_socket, peer_socket = socket.socketpair()
    client_socket.settimeout(5)
    peer_socket.close()
    with pytest.raises(StorageV2ConnectionClosedError) as excinfo:
        recv_frame(client_socket)
    assert isinstance(excinfo.value, StorageV2ProtocolError)
    client_socket.close()


def test_recv_frame_mid_frame_close_is_not_a_clean_close() -> None:
    client_socket, peer_socket = socket.socketpair()
    client_socket.settimeout(5)
    # Two bytes of the four-byte length prefix, then FIN: the parser consumed
    # part of a frame, so this is a truncation, never an availability event.
    peer_socket.sendall(b"\x00\x00")
    peer_socket.close()
    with pytest.raises(StorageV2ProtocolError) as excinfo:
        recv_frame(client_socket)
    assert not isinstance(excinfo.value, StorageV2ConnectionClosedError)
    assert "mid-frame" in str(excinfo.value)
    client_socket.close()


def _probe_session(sock: socket.socket) -> StorageV2Session:
    session = object.__new__(StorageV2Session)
    session._sock = sock
    return session


def test_probe_liveness_reports_alive_then_detects_queued_peer_close() -> None:
    client_socket, peer_socket = socket.socketpair()
    client_socket.settimeout(5)
    session = _probe_session(client_socket)
    assert session.probe_liveness() is True
    peer_socket.close()
    assert session.probe_liveness() is False
    # The probe preserves the socket timeout the next request relies on.
    assert client_socket.gettimeout() == 5
    client_socket.close()


def test_probe_liveness_flags_unsolicited_bytes_as_desynchronized() -> None:
    client_socket, peer_socket = socket.socketpair()
    client_socket.settimeout(5)
    session = _probe_session(client_socket)
    peer_socket.sendall(b"\x00")
    assert session.probe_liveness() is False
    peer_socket.close()
    client_socket.close()


def test_v2_client_replays_a_closed_connection_command_with_same_command_id(monkeypatch):
    """A frame-boundary close shares the deadline_elapsed receipt-fence proof."""
    from lib.storage.v2_client import _ConnectionClosedError
    client = _histogram_client()
    attempts = []

    def closed_once(operation, payload, *, timeout, command_id, deadline_unix_ms):
        attempts.append(command_id)
        if len(attempts) == 1:
            raise _ConnectionClosedError(
                'database_unavailable',
                'storage.v2 authority closed the connection', True, 1)
        return {'ok': True}

    monkeypatch.setattr(client, '_attempt', closed_once)
    monkeypatch.setattr('lib.storage.v2_client.time.sleep', lambda _: None)
    result = client.command('record.put', {'key': 'k', 'value': 1}, command_id=None)
    assert result == {'ok': True}
    assert len(attempts) == 2
    assert attempts[0] == attempts[1]
    assert len(set(attempts)) == 1


@pytest.mark.parametrize('fallback', [False, True], ids=['poll', 'select'])
@pytest.mark.parametrize('timeout', [None, 0.0, 5.0])
def test_liveness_readiness_preserves_socket_configuration_and_receive_bytes(monkeypatch, fallback, timeout):
    import tofu_db.wire as wire
    if fallback:
        monkeypatch.delattr(wire.select, 'poll', raising=False)
    client_socket, peer_socket = socket.socketpair()
    client_socket.settimeout(timeout)
    class OwnedSocket:
        def fileno(self): return client_socket.fileno()
        def setblocking(self, value): raise AssertionError('probe changed blocking mode')
        def settimeout(self, value): raise AssertionError('probe changed timeout')
        def recv(self, *args): raise AssertionError('readiness probe received bytes')
    session = _probe_session(OwnedSocket())
    try:
        assert session.probe_liveness()
        peer_socket.sendall(b'x')
        assert not session.probe_liveness()
        assert client_socket.recv(1) == b'x'
        assert session.probe_liveness()
        assert client_socket.gettimeout() == timeout
        peer_socket.close()
        assert not session.probe_liveness()
        client_socket.close()
        assert not session.probe_liveness()
    finally:
        peer_socket.close(); client_socket.close()



@pytest.mark.parametrize('float_at_start', [True, False])
def test_float_bearing_large_response_uses_one_exact_decoder(monkeypatch, float_at_start):
    import json
    import orjson
    import tofu_db.wire as client
    from types import SimpleNamespace

    calls=[]
    def accelerated_loads(body):
        calls.append(True)
        return orjson.loads(body)
    monkeypatch.setattr(client,'_fast_json',SimpleNamespace(
        loads=accelerated_loads,JSONDecodeError=orjson.JSONDecodeError))
    fields={'elapsed':-0.0,'content':'large historical output '*2000,
            'exact_integer':(1<<80)+1}
    if not float_at_start:
        fields={'content':fields['content'],'exact_integer':fields['exact_integer'],'elapsed':fields['elapsed']}
    encoded=json.dumps(fields).encode()
    for body in (encoded,bytearray(encoded)):
        decoded=client.StorageV2Response(0,0,body,None,None,False).json()
        assert json.dumps(decoded,sort_keys=True)==json.dumps(fields,sort_keys=True)
    assert not calls, 'An already-ineligible response must not build a discarded accelerated tree'


def test_numeric_looking_quoted_output_keeps_exact_accelerated_decode(monkeypatch):
    import json
    import orjson
    import tofu_db.wire as client
    from types import SimpleNamespace

    calls=[]
    def accelerated_loads(body):
        calls.append(True)
        return orjson.loads(body)
    monkeypatch.setattr(client,'_fast_json',SimpleNamespace(
        loads=accelerated_loads,JSONDecodeError=orjson.JSONDecodeError))
    fields={'content':('literal "cost":1.23 and escaped \\"time":1e10\n'*500),
            'revision':42,'integers':[1,2,3]}
    encoded=json.dumps(fields).encode()
    assert client.StorageV2Response(0,0,encoded,None,None,False).json()==fields
    assert calls==[True], 'Quoted source text must not disable an eligible decoder'
