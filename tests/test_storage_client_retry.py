"""StorageClient retry contract: idempotent reads replay transient faults.

The transport is one-socket-per-RPC and the error taxonomy already marks
``database_timeout`` / ``database_busy`` / ``database_unavailable`` as
retryable; the client is where that contract is honored. Commands replay only
when the server proves the request was not dispatched; ambiguous failures stay
single-attempt because a replay could double-apply.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from lib.storage.client import StorageClient
from lib.storage.errors import StorageError
from lib.storage.frame_admission import FrameByteAdmission
from lib.storage.protocol import PROTOCOL_VERSION, recv_frame, send_frame


pytestmark = pytest.mark.unit

TOKEN = 't' * 32


def test_default_response_frame_budget_is_process_shared_and_manifest_bound(
        monkeypatch):
    from lib.storage import client as client_module

    monkeypatch.setenv('TOFU_STORAGE_RPC_INFLIGHT_MAX_MIB', '256')
    monkeypatch.setattr(client_module, '_PROCESS_RESPONSE_ADMISSION', None)
    first = StorageClient('127.0.0.1', 1, TOKEN)
    second = StorageClient('127.0.0.1', 2, TOKEN)

    assert first._response_frame_admission is second._response_frame_admission
    assert first.transport_metrics()[
        'response_frame_bytes_capacity'] == 256 * 1024 * 1024


class _FakeSidecar:
    """Minimal scripted storage.v1 peer.

    ``script`` holds one handler per accepted connection, invoked with the
    decoded request frame; it may return a response dict or ``None`` to
    stall the connection past the client's timeout.
    """

    def __init__(self, script):
        self._script = list(script)
        self.requests = []
        self._lock = threading.Lock()
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(('127.0.0.1', 0))
        self._server.listen(16)
        self.port = self._server.getsockname()[1]
        self._closed = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._closed:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(
                target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        with conn:
            try:
                request = recv_frame(conn)
            except Exception:
                return
            with self._lock:
                self.requests.append(request)
                handler = (self._script.pop(0) if self._script
                           else lambda _req: None)
            if handler == 'CLOSE':
                # Simulated crash/overload: the peer closes WITHOUT a frame.
                return
            response = handler(request)
            if response is None:
                time.sleep(30)  # wedged server: never answer in time
                return
            send_frame(conn, response)

    def close(self):
        self._closed = True
        self._server.close()


def _ok(request):
    return {
        'protocol': PROTOCOL_VERSION,
        'request_id': request['request_id'],
        'ok': True,
        'result': {'rows': 1},
    }


def _error(request, code, retryable, retry_after_ms=0):
    return {
        'protocol': PROTOCOL_VERSION,
        'request_id': request['request_id'],
        'ok': False,
        'error': {
            'code': code,
            'message': code,
            'retryable': retryable,
            'retry_after_ms': retry_after_ms,
            'operation_id': request['request_id'],
        },
    }


def _pre_dispatch_rejection(_request):
    return {
        'protocol': PROTOCOL_VERSION,
        'request_id': '',
        'ok': False,
        'error': {
            'code': 'database_unavailable',
            'message': 'Storage sidecar is at capacity',
            'retryable': True,
            'retry_after_ms': 1,
            'operation_id': '',
            'request_not_dispatched': True,
        },
    }


def test_query_recovers_after_classified_retryable_error():
    server = _FakeSidecar([
        lambda req: _error(req, 'database_timeout', True, 1),
        _ok,
    ])
    try:
        client = StorageClient('127.0.0.1', server.port, TOKEN, timeout=2.0)
        assert client.query('turn.events.list', {'attempt_id': 'a'}) == {'rows': 1}
        assert len(server.requests) == 2
    finally:
        server.close()


def test_client_response_frame_budget_releases_after_json_decode():
    admission = FrameByteAdmission(capacity_bytes=1024)
    server = _FakeSidecar([_ok])
    try:
        client = StorageClient(
            '127.0.0.1', server.port, TOKEN, timeout=2.0,
            response_frame_admission=admission)
        assert client.query(
            'turn.events.list', {'attempt_id': 'a'}) == {'rows': 1}
        metrics = client.transport_metrics()
        assert metrics['response_frame_bytes_inflight'] == 0
        assert metrics['response_frame_bytes_observed_total'] > 0
        assert metrics['response_frame_bytes_observed_max'] > 0
    finally:
        server.close()


def test_client_response_pressure_retries_read_but_not_ambiguous_command(
        monkeypatch):
    from lib.storage import client as client_module

    monkeypatch.setattr(
        client_module, '_RESPONSE_FRAME_ADMISSION_WAIT_S', 0.0)

    read_admission = FrameByteAdmission(capacity_bytes=64)
    assert read_admission.acquire(64, timeout_s=0.0)
    read_server = _FakeSidecar([_ok] * 3)
    try:
        client = StorageClient(
            '127.0.0.1', read_server.port, TOKEN, timeout=2.0,
            read_attempts=3, response_frame_admission=read_admission)
        with pytest.raises(StorageError) as captured:
            client.query('turn.events.list', {'attempt_id': 'a'})
        assert captured.value.code == 'database_unavailable'
        assert len(read_server.requests) == 3
        assert client.transport_metrics()[
            'response_frame_admission_rejections'] == 3
    finally:
        read_admission.release(64)
        read_server.close()

    command_admission = FrameByteAdmission(capacity_bytes=64)
    assert command_admission.acquire(64, timeout_s=0.0)
    command_server = _FakeSidecar([_ok, _ok])
    try:
        client = StorageClient(
            '127.0.0.1', command_server.port, TOKEN, timeout=2.0,
            response_frame_admission=command_admission)
        with pytest.raises(StorageError) as captured:
            client.command('turn.event.append', {'x': 1}, command_id='c1')
        assert captured.value.request_not_dispatched is False
        assert len(command_server.requests) == 1
    finally:
        command_admission.release(64)
        command_server.close()


def test_query_recovers_after_silent_socket_timeout():
    server = _FakeSidecar([lambda _req: None, _ok])
    try:
        client = StorageClient('127.0.0.1', server.port, TOKEN, timeout=0.2)
        assert client.query('turn.events.list', {'attempt_id': 'a'}) == {'rows': 1}
        assert len(server.requests) == 2
    finally:
        server.close()


def test_query_gives_up_after_bounded_attempts():
    server = _FakeSidecar([lambda _req: None] * 5)
    try:
        client = StorageClient(
            '127.0.0.1', server.port, TOKEN, timeout=0.2, read_attempts=3)
        started = time.monotonic()
        with pytest.raises(StorageError) as excinfo:
            client.query('turn.events.list', {'attempt_id': 'a'})
        assert excinfo.value.code == 'database_timeout'
        assert len(server.requests) == 3
        # Total budget stays bounded by attempts * (timeout + socket grace).
        assert time.monotonic() - started < 4.5
    finally:
        server.close()


def test_command_is_never_retried():
    server = _FakeSidecar([lambda _req: None, _ok])
    try:
        client = StorageClient('127.0.0.1', server.port, TOKEN, timeout=0.2)
        with pytest.raises(StorageError) as excinfo:
            client.command('turn.event.append', {'x': 1}, command_id='c1')
        assert excinfo.value.code == 'database_timeout'
        assert len(server.requests) == 1
    finally:
        server.close()


def test_command_retries_only_when_server_proves_request_not_dispatched():
    server = _FakeSidecar([_pre_dispatch_rejection, _ok])
    try:
        client = StorageClient('127.0.0.1', server.port, TOKEN, timeout=2.0)
        assert client.command(
            'turn.event.append', {'x': 1}, command_id='c1') == {'rows': 1}
        assert len(server.requests) == 2
        metrics = client.transport_metrics()
        assert metrics['pre_dispatch_command_retries'] == 1
        assert metrics['pre_dispatch_command_retry_exhaustions'] == 0
    finally:
        server.close()


def test_uncertain_command_failure_never_inherits_predispatch_retry():
    def unproven_rejection(request):
        response = _pre_dispatch_rejection(request)
        response['error'].pop('request_not_dispatched')
        return response

    server = _FakeSidecar([unproven_rejection, _ok])
    try:
        client = StorageClient('127.0.0.1', server.port, TOKEN, timeout=2.0)
        with pytest.raises(StorageError) as captured:
            client.command('turn.event.append', {'x': 1}, command_id='c1')
        assert captured.value.request_not_dispatched is False
        assert len(server.requests) == 1
        assert client.transport_metrics()[
            'pre_dispatch_command_retries'] == 0
        malformed = StorageError.from_payload({
            'code': 'database_unavailable',
            'retryable': True,
            'request_not_dispatched': 'true',
        })
        assert malformed.request_not_dispatched is False
    finally:
        server.close()


def test_predispatch_command_retry_exhaustion_is_finite_and_observable():
    server = _FakeSidecar([_pre_dispatch_rejection] * 4)
    try:
        client = StorageClient(
            '127.0.0.1', server.port, TOKEN,
            timeout=2.0, read_attempts=3)
        with pytest.raises(StorageError) as captured:
            client.command('turn.event.append', {'x': 1}, command_id='c1')
        assert captured.value.request_not_dispatched is True
        assert len(server.requests) == 3
        metrics = client.transport_metrics()
        assert metrics['pre_dispatch_command_retries'] == 2
        assert metrics['pre_dispatch_command_retry_exhaustions'] == 1
    finally:
        server.close()


def test_non_retryable_error_is_not_retried():
    server = _FakeSidecar([
        lambda req: _error(req, 'database_conflict', False),
        _ok,
    ])
    try:
        client = StorageClient('127.0.0.1', server.port, TOKEN, timeout=2.0)
        with pytest.raises(StorageError) as excinfo:
            client.query('turn.events.list', {'attempt_id': 'a'})
        assert excinfo.value.code == 'database_conflict'
        assert len(server.requests) == 1
    finally:
        server.close()


def test_query_recovers_when_the_peer_closes_mid_frame():
    # A sidecar crash/restart or capacity close used to surface as a
    # non-retryable protocol error; idempotent reads must ride over it.
    server = _FakeSidecar(['CLOSE', _ok])
    try:
        client = StorageClient('127.0.0.1', server.port, TOKEN, timeout=2.0)
        assert client.query('turn.events.list', {'attempt_id': 'a'}) == {'rows': 1}
        assert len(server.requests) == 2
    finally:
        server.close()


def test_command_mid_frame_close_is_classified_transient_but_never_retried():
    server = _FakeSidecar(['CLOSE', _ok])
    try:
        client = StorageClient('127.0.0.1', server.port, TOKEN, timeout=2.0)
        with pytest.raises(StorageError) as excinfo:
            client.command('turn.event.append', {'x': 1}, command_id='c1')
        assert excinfo.value.code == 'database_unavailable'
        assert excinfo.value.retryable is True
        assert len(server.requests) == 1
    finally:
        server.close()


def test_uncorrelated_classified_rejection_frame_is_honored():
    # The capacity-rejection path answers before it may read the request
    # frame, so it cannot echo the request id; the carried retryable
    # classification must still drive the read retry loop.
    server = _FakeSidecar([_pre_dispatch_rejection, _ok])
    try:
        client = StorageClient('127.0.0.1', server.port, TOKEN, timeout=2.0)
        assert client.query('turn.events.list', {'attempt_id': 'a'}) == {'rows': 1}
        assert len(server.requests) == 2
    finally:
        server.close()


def test_correlation_check_keeps_its_strength_for_ok_responses():
    def wrong_id(_req):
        return {
            'protocol': PROTOCOL_VERSION,
            'request_id': 'someone-elses-id',
            'ok': True,
            'result': {'rows': 1},
        }

    server = _FakeSidecar([wrong_id])
    try:
        client = StorageClient('127.0.0.1', server.port, TOKEN, timeout=2.0)
        with pytest.raises(StorageError) as excinfo:
            client.query('turn.events.list', {'attempt_id': 'a'})
        assert excinfo.value.code == 'database_protocol_error'
        assert 'correlation' in str(excinfo.value)
    finally:
        server.close()
