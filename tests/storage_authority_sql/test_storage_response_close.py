"""Complete replies survive partial socket writes and bounded peer closure."""
import socket
import threading
from types import SimpleNamespace

import pytest

from lib.storage.protocol import recv_frame, send_frame, MAX_FRAME_BYTES
from lib.storage_authority_sql.server import _StorageHandler, _StorageTCPServer
from tofu_db._loopback import create_connection

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('size', [30000, 2 * 1024 * 1024])
def test_complete_real_response_is_received_before_accepted_socket_teardown(monkeypatch, size):
    content = 'A' * size
    # The SQL operation is outside this transport test; framing, admission,
    # accepted socket, partial sendall, worker and teardown are production code.
    monkeypatch.setattr(_StorageHandler, '_dispatch', lambda self, request, request_id:
                        {'request_id': request_id, 'content': content})
    server = _StorageTCPServer(('127.0.0.1', 0), _StorageHandler, backend=None,
        token='isolated-test', rpc_capacity=2, read_only_preview=False)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        for index in range(5):
            with create_connection(server.server_address, timeout=5) as connection:
                send_frame(connection, {'request_id': str(index), 'operation': 'isolated.echo'})
                received = recv_frame(connection)
                assert received['request_id'] == str(index)
                assert received['content'] == content
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
    assert not worker.is_alive()


@pytest.mark.parametrize('peer_result', [b'', b'unexpected', socket.timeout(), OSError('peer lost')])
def test_peer_close_wait_reads_once_and_preserves_existing_timeout(peer_result):
    class Peer:
        def __init__(self):
            self.reads = 0
            self.half_closed = False
        def shutdown(self, direction):
            assert direction == socket.SHUT_WR
            self.half_closed = True
        def recv(self, size):
            assert self.half_closed and size == 1
            self.reads += 1
            if isinstance(peer_result, Exception):
                raise peer_result
            return peer_result[:1]
    peer = Peer()
    _StorageHandler._wait_for_peer_close(SimpleNamespace(request=peer))
    assert peer.reads == 1


@pytest.mark.parametrize('send_fails', [False, True])
def test_response_reservation_is_released_before_any_peer_close_wait(monkeypatch, send_fails):
    from lib.storage.frame_admission import FrameByteAdmission
    import lib.storage_authority_sql.server as owner
    admission = FrameByteAdmission(capacity_bytes=2 * MAX_FRAME_BYTES)
    handler = object.__new__(_StorageHandler)
    handler.request = object()
    handler.server = SimpleNamespace(_frame_byte_admission=admission)
    def send(_socket, response, *, before_payload):
        before_payload(96)
        assert admission.metrics()['frame_bytes_inflight'] == 96
        if send_fails:
            raise OSError('injected failed send')
        return 100
    monkeypatch.setattr(owner, 'send_frame', send)
    assert handler._send_response({'result': 'test'}, 'request-1', 'isolated.echo') == (not send_fails)
    assert admission.metrics()['frame_bytes_inflight'] == 0
