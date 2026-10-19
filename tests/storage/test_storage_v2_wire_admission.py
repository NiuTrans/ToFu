"""storage.v2 shared response buffer ownership and demand-driven streams.

One process planner accounts each frame and assembled payload. The memory hint
selects RAM or anonymous-file overflow; it is not a byte/count admission quota.
Malformed sequences and transport errors retire the session and release buffers.
"""

from __future__ import annotations

import json
import socket
import threading

import pytest

from lib.storage.errors import StorageError
from lib.storage.frame_admission import (
    FrameByteAdmission,
    process_response_frame_admission,
)
from lib.storage import client as v1_client_mod
from lib.storage import v2_client as v2_module
from lib.storage.v2_client import StorageV2Client
from tofu_db.generated_contract import STORAGE_SCHEMA_VERSION
from tofu_db.wire import (
    FIELD_CORRELATION_ID,
    FIELD_ERROR_CODE,
    FIELD_ERROR_MESSAGE,
    FIELD_KIND,
    FIELD_PAYLOAD,
    FIELD_PROTOCOL_VERSION,
    FIELD_RETRYABLE,
    FIELD_SCHEMA_ID,
    FIELD_STATUS,
    FIELD_CHUNK_INDEX,
    FIELD_FINAL,
    FIELD_STREAM_ID,
    KIND_RESPONSE,
    KIND_RESPONSE_CHUNK,
    PROTOCOL_VERSION,
    StorageV2AdmissionRejected,
    StorageV2Session,
    decode_message,
    encode_message,
    pack_frame,
    recv_frame,
)


pytestmark = pytest.mark.unit

_SECRET = b's' * 48
_CHUNK = 1024 * 1024


class _FakeDaemon:
    """Single-connection scripted storage.v2 peer over a real loopback socket."""

    def __init__(self, respond):
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(('127.0.0.1', 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self._respond = respond
        self._thread = threading.Thread(
            target=self._serve, name='test-fake-daemon', daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            conn, _ = self._listener.accept()
            conn.settimeout(10)
            with conn:
                hello = recv_frame(conn)
                correlation = hello[FIELD_CORRELATION_ID]
                conn.sendall(pack_frame(encode_message({
                    FIELD_KIND: KIND_RESPONSE,
                    FIELD_CORRELATION_ID: correlation,
                    FIELD_SCHEMA_ID: STORAGE_SCHEMA_VERSION,
                    FIELD_STATUS: 0,
                })))
                request = recv_frame(conn)
                self._respond(conn, request[FIELD_CORRELATION_ID])
        except OSError:
            pass
        finally:
            self._listener.close()

    def join(self, timeout=10):
        self._thread.join(timeout=timeout)


def _response_frame(correlation, payload: bytes):
    return pack_frame(encode_message({
        FIELD_KIND: KIND_RESPONSE,
        FIELD_PROTOCOL_VERSION: PROTOCOL_VERSION,
        FIELD_CORRELATION_ID: correlation,
        FIELD_SCHEMA_ID: STORAGE_SCHEMA_VERSION,
        FIELD_PAYLOAD: payload,
        FIELD_STATUS: 0,
        FIELD_ERROR_CODE: None,
        FIELD_ERROR_MESSAGE: None,
        FIELD_RETRYABLE: False,
    }))


def _chunk_frame(correlation, stream_id, index, data: bytes, final: bool):
    return pack_frame(encode_message({
        FIELD_KIND: KIND_RESPONSE_CHUNK,
        FIELD_PROTOCOL_VERSION: PROTOCOL_VERSION,
        FIELD_CORRELATION_ID: correlation,
        FIELD_SCHEMA_ID: STORAGE_SCHEMA_VERSION,
        FIELD_PAYLOAD: data,
        FIELD_STREAM_ID: stream_id,
        FIELD_CHUNK_INDEX: index,
        FIELD_FINAL: final,
    }))


def _single_response(payload: bytes):
    def respond(conn, correlation):
        conn.sendall(_response_frame(correlation, payload))
    return respond


def _session(port, admission, *, timeout=5.0):
    return StorageV2Session(
        '127.0.0.1', port, _SECRET,
        owner_id=7, tenant_id=3, schema_ids=(STORAGE_SCHEMA_VERSION,),
        timeout=timeout, response_admission=admission)


def test_process_admission_is_one_shared_singleton_for_both_clients():
    shared = process_response_frame_admission()
    assert process_response_frame_admission() is shared
    # v1's private accessor resolves to the same object it always used.
    assert v1_client_mod._process_response_frame_admission() is shared
    # A v2 client built without an explicit budget uses the same one.
    client = StorageV2Client(
        '127.0.0.1', 1, _SECRET, owner_id=1, tenant_id=None)
    assert client._response_frame_admission is shared
    client.close()


def test_response_within_budget_succeeds_and_releases():
    payload = json.dumps({'ok': True}).encode()
    daemon = _FakeDaemon(_single_response(payload))
    admission = FrameByteAdmission(capacity_bytes=4 * _CHUNK)
    session = _session(daemon.port, admission)
    try:
        response = session.request(
            'conversation.count', {}, command_id=None,
            deadline_unix_ms=0)
        assert response.status == 0
        assert response.json() == {'ok': True}
        metrics = admission.metrics()
        assert metrics['frame_bytes_inflight'] == 0  # released on return
        assert metrics['frame_admission_rejections'] == 0
        assert metrics['frame_bytes_admitted_total'] > 0
    finally:
        session.close()
        daemon.join()


def test_response_beyond_memory_capacity_uses_file_buffer():
    payload = b'x' * (6 * _CHUNK)
    daemon = _FakeDaemon(_single_response(payload))
    admission = FrameByteAdmission(capacity_bytes=4 * _CHUNK)
    session = _session(daemon.port, admission, timeout=0.3)
    try:
        response = session.request('conversation.count', {}, command_id=None, deadline_unix_ms=0)
        assert response.payload == payload
        metrics = admission.metrics()
        assert metrics['frame_admission_rejections'] == 0
        assert metrics['frame_buffer_spill_bytes_total'] > 0
        assert metrics['frame_bytes_inflight'] == 0
    finally:
        session.close()
        daemon.join()


def test_streamed_chunks_are_accounted_as_they_accumulate():
    chunks = [b'x' * _CHUNK, b'y' * _CHUNK, b'z' * _CHUNK]

    def respond(conn, correlation):
        stream_id = b'\x01' * 16
        for index, data in enumerate(chunks):
            conn.sendall(_chunk_frame(
                correlation, stream_id, index, data,
                index + 1 == len(chunks)))
        conn.sendall(_response_frame(correlation, b""))

    daemon = _FakeDaemon(respond)
    # Capacity admits the first chunk frame (~1 MiB) but never the second.
    admission = FrameByteAdmission(capacity_bytes=int(1.5 * _CHUNK))
    session = _session(daemon.port, admission, timeout=0.3)
    try:
        response = session.request('artifact.get', {}, command_id=None, deadline_unix_ms=0)
        assert response.payload == b''.join(chunks)
        metrics = admission.metrics()
        assert metrics['frame_admission_rejections'] == 0
        assert metrics['frame_buffer_spill_bytes_total'] > 0
        assert metrics['frame_bytes_inflight'] == 0
    finally:
        session.close()
        daemon.join()


def test_concurrent_large_responses_are_bounded_by_one_budget():
    # Two simultaneous ~2.5 MiB responses against a 4 MiB process budget:
    # both must complete (the second waits for the first's release), the
    # peak stays under the capacity, and nothing is rejected.
    payload = b'x' * (5 * _CHUNK // 2)

    def slow_response(conn, correlation):
        frame = _response_frame(correlation, payload)
        midpoint = len(frame) // 2
        conn.sendall(frame[:midpoint])
        threading.Event().wait(0.15)  # hold the reservation across the peer
        conn.sendall(frame[midpoint:])

    daemons = [_FakeDaemon(slow_response), _FakeDaemon(slow_response)]
    admission = FrameByteAdmission(capacity_bytes=4 * _CHUNK)
    results: dict[int, object] = {}

    def read(index, port):
        session = _session(port, admission, timeout=5.0)
        try:
            response = session.request(
                'artifact.get', {}, command_id=None, deadline_unix_ms=0)
            results[index] = len(response.payload)
        except StorageV2AdmissionRejected as exc:
            results[index] = exc
        finally:
            session.close()

    readers = [
        threading.Thread(target=read, args=(index, daemon.port))
        for index, daemon in enumerate(daemons)
    ]
    for reader in readers:
        reader.start()
    for reader in readers:
        reader.join(timeout=15)
    for daemon in daemons:
        daemon.join()
    assert results[0] == len(payload), results
    assert results[1] == len(payload), results
    metrics = admission.metrics()
    assert metrics['frame_bytes_peak'] > 4 * _CHUNK
    assert metrics['frame_buffer_spill_bytes_total'] > 0
    assert metrics['frame_bytes_inflight'] == 0


def test_client_maps_rejection_to_retryable_unavailable_and_retires():
    rejected_sessions = []

    class _RejectingSession:
        calls = 0

        def __init__(self, *args, **kwargs):
            pass

        def set_timeout(self, _seconds):
            pass

        def request(self, operation, payload, *, command_id, deadline_unix_ms):
            type(self).calls += 1
            raise StorageV2AdmissionRejected('budget')

        def close(self):
            rejected_sessions.append(self)

        def probe_liveness(self):
            return True

    admission = FrameByteAdmission(capacity_bytes=4 * _CHUNK)
    import lib.storage.v2_client as v2_mod
    original = v2_mod.StorageV2Session
    v2_mod.StorageV2Session = _RejectingSession
    try:
        client = StorageV2Client(
            '127.0.0.1', 1, _SECRET, owner_id=7, tenant_id=3,
            response_frame_admission=admission, read_attempts=1)
        with pytest.raises(StorageError) as excinfo:
            client.query('conversation.count', {}, deadline=1.0)
        error = excinfo.value
        assert error.code == 'database_unavailable'
        assert error.retryable is True
        assert error.retry_after_ms == 50
        # The desynced session was retired (closed, not returned to the pool).
        assert len(rejected_sessions) == 1
        client.close()
    finally:
        v2_mod.StorageV2Session = original


def test_streamed_response_exceeds_former_byte_and_chunk_quotas_and_releases():
    def respond(conn, correlation):
        stream_id = b'\x02' * 16
        chunk = b'x' * _CHUNK
        for index in range(70):
            conn.sendall(_chunk_frame(correlation, stream_id, index, chunk, False))
        conn.sendall(_chunk_frame(correlation, stream_id, 70, b'end', True))
        conn.sendall(_response_frame(correlation, b''))
    daemon = _FakeDaemon(respond)
    admission = FrameByteAdmission(capacity_bytes=4 * _CHUNK)
    session = _session(daemon.port, admission, timeout=30.0)
    try:
        response = session.request('record.get', {}, command_id=None, deadline_unix_ms=0)
        assert response.status == 0
        assert len(response.payload) == 70 * _CHUNK + 3
        assert response.payload.endswith(b'end')
        assert response.payload[:-3].count(b'x') == 70 * _CHUNK
        assert admission.metrics()['frame_bytes_inflight'] == 0
        assert admission.metrics()['frame_admission_rejections'] == 0
    finally:
        session.close()
        daemon.join(timeout=30)
        assert not daemon._thread.is_alive()


def test_streamed_request_exceeds_former_byte_and_chunk_quotas():
    from tofu_db import wire as wire_module
    class RequestPeer(_FakeDaemon):
        chunks = 0
        failure = None
        def _serve(self):
            try:
                conn, _ = self._listener.accept()
                conn.settimeout(30)
                with conn:
                    hello = recv_frame(conn)
                    conn.sendall(_response_frame(hello[FIELD_CORRELATION_ID], b''))
                    payload = bytearray()
                    correlation = None
                    while True:
                        message = recv_frame(conn)
                        if message[FIELD_KIND] == wire_module.KIND_REQUEST:
                            assert not message.get(FIELD_PAYLOAD, b'')
                            break
                        assert message[FIELD_KIND] == wire_module.KIND_BLOB_CHUNK
                        assert message[FIELD_CHUNK_INDEX] == self.chunks
                        correlation = message[FIELD_CORRELATION_ID]
                        payload.extend(message[FIELD_PAYLOAD])
                        self.chunks += 1
                        if message[FIELD_FINAL]:
                            assert len(payload) == message[wire_module.FIELD_TOTAL_PAYLOAD_BYTES]
                    assert message[FIELD_CORRELATION_ID] == correlation
                    value = json.loads(payload)
                    assert len(value['blob']) == 70 * _CHUNK
                    assert value['blob'].count('x') == 70 * _CHUNK
                    conn.sendall(_response_frame(correlation, b'{"ok":true}'))
            except BaseException as error:
                self.failure = error
            finally:
                self._listener.close()
    daemon = RequestPeer(None)
    admission = FrameByteAdmission(capacity_bytes=4 * _CHUNK)
    session = _session(daemon.port, admission, timeout=30)
    try:
        response = session.request('record.put', {'blob':'x' * (70 * _CHUNK)},
                                   command_id=None, deadline_unix_ms=0)
        assert response.json() == {'ok':True}
    finally:
        session.close()
        daemon.join(timeout=30)
        assert not daemon._thread.is_alive()
    assert daemon.failure is None, daemon.failure
    assert daemon.chunks > 64
