"""StorageClient retry contract: idempotent reads replay transient faults.

The transport is one-socket-per-RPC and the error taxonomy already marks
``database_timeout`` / ``database_busy`` / ``database_unavailable`` as
retryable; the client is where that contract is honored.  Commands are never
auto-retried (no server-side receipt → a replay could double-apply).
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from lib.storage.client import StorageClient
from lib.storage.errors import StorageError
from lib.storage.protocol import PROTOCOL_VERSION, recv_frame, send_frame


pytestmark = pytest.mark.unit

TOKEN = 't' * 32


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
    def reject(_req):
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
            },
        }

    server = _FakeSidecar([reject, _ok])
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
