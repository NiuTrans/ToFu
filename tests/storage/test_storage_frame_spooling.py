"""Anonymous frame overflow, exact leases, and failure cleanup."""
import io
import mmap
import socket
import struct
import threading

import pytest
from lib.storage.frame_admission import FrameByteAdmission
from lib.storage import protocol
from tofu_db import _frame_body
from tofu_db import wire

pytestmark = pytest.mark.unit


def test_ram_and_file_owners_release_exactly_once_after_resize():
    budget = FrameByteAdmission(capacity_bytes=100)
    first = budget.reserve_frame(80)
    second = budget.reserve_frame(80)
    assert not first.spilled and second.spilled
    assert budget.metrics()['frame_buffer_memory_bytes'] == 80
    assert budget.metrics()['frame_buffer_file_bytes'] == 80
    budget.resize(1)
    first.close(); first.close()
    assert budget.metrics()['frame_buffer_file_bytes'] == 80
    second.close(); second.close()
    assert budget.metrics()['frame_bytes_inflight'] == 0
    assert budget.metrics()['frame_admission_waits'] == 0


@pytest.mark.parametrize('fail_in', ['read', 'decode'])
def test_overflow_file_closes_after_partial_read_or_decode_failure(monkeypatch, fail_in):
    opened = []
    original = _frame_body.tempfile.TemporaryFile
    def create(*args, **kwargs):
        file = original(*args, **kwargs); opened.append(file); return file
    monkeypatch.setattr(_frame_body.tempfile, 'TemporaryFile', create)
    budget = FrameByteAdmission(capacity_bytes=1)
    reservation = budget.reserve_frame(130000)
    reads = 0
    def read(count):
        nonlocal reads
        reads += 1
        if fail_in == 'read' and reads == 2:
            raise OSError('injected interrupted peer')
        return b'x' * count
    try:
        with pytest.raises(OSError):
            with _frame_body.received_frame_body(read, reservation.size, reservation) as body:
                assert isinstance(body, mmap.mmap)
                raise OSError('injected decoder failure')
    finally:
        reservation.close()
    assert opened and all(file.closed for file in opened)
    assert budget.metrics()['frame_bytes_inflight'] == 0


def test_v1_file_decoding_returns_owned_values_and_cleans_after_bad_json():
    from lib.storage.errors import StorageError
    for payload in [b'{"value":"owned"}', b'{bad-json']:
        left, right = socket.socketpair()
        budget = FrameByteAdmission(capacity_bytes=1)
        reservations = []
        def reserve(size):
            lease = budget.reserve_frame(size); reservations.append(lease); return lease
        try:
            left.sendall(struct.pack('!I', len(payload)) + payload)
            if payload.startswith(b'{bad'):
                with pytest.raises(StorageError):
                    protocol.recv_frame(right, before_payload=reserve)
            else:
                assert protocol.recv_frame(right, before_payload=reserve) == {'value': 'owned'}
        finally:
            for lease in reservations: lease.close()
            left.close(); right.close()
        assert budget.metrics()['frame_bytes_inflight'] == 0
        assert budget.metrics()['frame_buffer_spill_bytes_total'] == len(payload)


def test_v2_file_checksum_failure_releases_body_and_lease(monkeypatch):
    opened = []
    original = _frame_body.tempfile.TemporaryFile
    def create(*args, **kwargs):
        file = original(*args, **kwargs); opened.append(file); return file
    monkeypatch.setattr(_frame_body.tempfile, 'TemporaryFile', create)
    left, right = socket.socketpair()
    budget = FrameByteAdmission(capacity_bytes=1)
    lease = budget.reserve_frame(4)
    try:
        left.sendall(struct.pack('!I', 4) + b'bad!' + struct.pack('!I', 0))
        with pytest.raises(wire.StorageV2ProtocolError, match='checksum'):
            wire.recv_frame(right, before_payload=lambda size: lease)
    finally:
        lease.close(); left.close(); right.close()
    assert all(file.closed for file in opened)
    assert budget.metrics()['frame_bytes_inflight'] == 0


def test_send_accounts_exact_encoded_body_before_any_write():
    class Socket:
        written = b''
        def sendall(self, data):
            assert seen == [len(data) - 4]
            self.written = data
    sock = Socket(); seen = []
    length = protocol.send_frame(sock, {'small': True}, before_payload=seen.append)
    assert length == len(sock.written)
    assert seen[0] < 64
