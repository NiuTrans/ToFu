"""Over-capacity RPC connections get a classified retryable rejection frame.

Regression pin for the 2026-08-19 SSE failures: the slot-overflow path used
to close the socket bare, so the client surfaced an opaque mid-frame EOF
with no retryable classification.  The server now answers in-band with
``database_unavailable`` (retryable) before closing.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from lib.storage.errors import StorageError
from lib.storage.client import StorageClient
from lib.storage.protocol import PROTOCOL_VERSION, recv_frame
from lib.storage_sidecar.server import create_server

pytestmark = pytest.mark.unit

TOKEN = 't' * 32


def test_idle_heap_trim_is_thresholded_cooled_and_measured():
    from lib.storage_sidecar.server import _IdleHeapTrimmer

    now = [100.0]
    rss_samples = iter((600, 400, 700, 500))
    timer_samples = iter((
        1_000_000_000, 1_003_000_000,
        2_000_000_000, 2_005_000_000,
    ))
    trim_calls = []
    trimmer = _IdleHeapTrimmer(
        threshold_bytes=512,
        cooldown_s=30.0,
        clock=lambda: now[0],
        rss_bytes=lambda: next(rss_samples),
        trim=lambda: trim_calls.append(True) or True,
        timer_ns=lambda: next(timer_samples),
    )

    assert trimmer.maybe_trim()['reclaimed_bytes'] == 200
    assert trimmer.maybe_trim() is None
    now[0] += 31.0
    assert trimmer.maybe_trim()['reclaimed_bytes'] == 200
    assert trim_calls == [True, True]
    assert trimmer.metrics() == {
        'idle_trim_attempts': 2,
        'idle_trim_successes': 2,
        'idle_trim_reclaimed_bytes': 400,
        'idle_trim_last_before_bytes': 700,
        'idle_trim_last_after_bytes': 500,
        'idle_trim_duration_ns_total': 8_000_000,
        'idle_trim_last_duration_ns': 5_000_000,
    }


def test_idle_heap_trim_skips_below_threshold():
    from lib.storage_sidecar.server import _IdleHeapTrimmer

    trim_calls = []
    trimmer = _IdleHeapTrimmer(
        threshold_bytes=512,
        cooldown_s=30.0,
        clock=lambda: 100.0,
        rss_bytes=lambda: 511,
        trim=lambda: trim_calls.append(True) or True,
    )

    assert trimmer.maybe_trim() is None
    assert trim_calls == []
    assert trimmer.metrics()['idle_trim_attempts'] == 0


def test_frame_byte_admission_is_weighted_and_reports_pressure():
    from lib.storage.frame_admission import FrameByteAdmission

    admission = FrameByteAdmission(capacity_bytes=10)
    assert admission.acquire(9, timeout_s=0.0)
    assert not admission.acquire(2, timeout_s=0.0)
    assert admission.metrics() == {
        'frame_bytes_inflight': 9,
        'frame_bytes_capacity': 10,
        'frame_bytes_peak': 9,
        'frame_admission_waiting': 0,
        'frame_admission_waits': 1,
        'frame_admission_rejections': 1,
        'frame_bytes_admitted_total': 9,
        'request_frame_bytes_total': 0,
        'request_frame_bytes_max': 0,
        'response_frame_bytes_total': 0,
        'response_frame_bytes_max': 0,
    }
    admission.release(9)
    assert admission.acquire(10, timeout_s=0.0)
    admission.release(10)
    assert admission.metrics()['frame_bytes_peak'] == 10


def test_frame_byte_admission_does_not_starve_large_fifo_head():
    from lib.storage.frame_admission import FrameByteAdmission

    admission = FrameByteAdmission(capacity_bytes=10)
    assert admission.acquire(10, timeout_s=0.0)
    order = []
    release_large = threading.Event()

    def wait_for_budget(name, size):
        assert admission.acquire(size, timeout_s=2.0)
        order.append(name)
        if name == 'large':
            assert release_large.wait(2.0)
        admission.release(size)

    large = threading.Thread(
        target=wait_for_budget, args=('large', 9), daemon=True)
    small = threading.Thread(
        target=wait_for_budget, args=('small', 2), daemon=True)
    large.start()
    deadline = time.monotonic() + 1.0
    while admission.metrics()['frame_admission_waiting'] != 1:
        if time.monotonic() >= deadline:
            pytest.fail('large frame never entered the admission queue')
        time.sleep(0.001)
    small.start()
    deadline = time.monotonic() + 1.0
    while admission.metrics()['frame_admission_waiting'] != 2:
        if time.monotonic() >= deadline:
            pytest.fail('small frame never queued behind the large frame')
        time.sleep(0.001)
    admission.release(10)
    deadline = time.monotonic() + 1.0
    while order != ['large'] and time.monotonic() < deadline:
        time.sleep(0.001)
    assert order == ['large']
    release_large.set()
    large.join(timeout=2.0)
    small.join(timeout=2.0)
    assert not large.is_alive() and not small.is_alive()
    assert order == ['large', 'small']


def test_frame_byte_admission_drains_completed_response_before_new_request():
    from lib.storage.frame_admission import FrameByteAdmission

    admission = FrameByteAdmission(capacity_bytes=10)
    assert admission.acquire(10, timeout_s=0.0)
    order = []
    release_response = threading.Event()

    def wait_for_budget(name, size, response_priority=False):
        acquired = admission.acquire(
            size, timeout_s=2.0, response_priority=response_priority)
        if not acquired:
            return
        order.append(name)
        if response_priority:
            release_response.wait(2.0)
        admission.release(size)

    request = threading.Thread(
        target=wait_for_budget, args=('request', 9), daemon=True)
    response = threading.Thread(
        target=wait_for_budget,
        args=('response', 2, True),
        daemon=True,
    )
    request.start()
    deadline = time.monotonic() + 1.0
    while admission.metrics()['frame_admission_waiting'] != 1:
        if time.monotonic() >= deadline:
            pytest.fail('request never entered the admission queue')
        time.sleep(0.001)
    response.start()
    deadline = time.monotonic() + 1.0
    while admission.metrics()['frame_admission_waiting'] != 2:
        if time.monotonic() >= deadline:
            pytest.fail('response never entered its priority queue')
        time.sleep(0.001)
    admission.release(10)
    deadline = time.monotonic() + 1.0
    while order != ['response'] and time.monotonic() < deadline:
        time.sleep(0.001)
    assert order == ['response']
    release_response.set()
    response.join(timeout=2.0)
    request.join(timeout=2.0)
    assert not response.is_alive() and not request.is_alive()
    assert order == ['response', 'request']


def _wait_for_active_handlers(server, *, timeout: float = 1.0) -> None:
    """Wait for the server thread's post-response slot release."""
    deadline = time.monotonic() + timeout
    while server.rpc_metrics()['active']:
        if time.monotonic() >= deadline:
            pytest.fail('RPC handler did not release its active slot')
        time.sleep(0.001)


class _Backend:
    """Minimal health/command authority for admission tests."""

    @staticmethod
    def health():
        return {'status': 'ok'}

    @staticmethod
    def command(
        _operation, _digest, _command_id, _priority, _callback, _deadline_at,
        **_kwargs,
    ):
        return {'stored': True}


@pytest.fixture
def server():
    srv = create_server(_Backend(), TOKEN)
    thread = threading.Thread(
        target=srv.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
    thread.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def test_over_capacity_rejection_is_classified_retryable(server):
    for _ in range(server.rpc_capacity):
        assert server._rpc_slots.acquire(blocking=False)
    try:
        port = server.server_address[1]
        with socket.create_connection(('127.0.0.1', port), timeout=5) as sock:
            sock.settimeout(5)
            response = recv_frame(sock)
        assert response['protocol'] == PROTOCOL_VERSION
        assert response['ok'] is False
        error = StorageError.from_payload(response['error'])
        assert error.code == 'database_unavailable'
        assert error.retryable is True
        assert error.retry_after_ms == 100
        assert error.request_not_dispatched is True
        assert server.rpc_metrics()['rejected'] == 1
    finally:
        for _ in range(server.rpc_capacity):
            server._rpc_slots.release()


def test_capacity_rejection_does_not_consume_a_slot_forever(server):
    port = server.server_address[1]
    for _ in range(server.rpc_capacity):
        assert server._rpc_slots.acquire(blocking=False)
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=5) as sock:
            sock.settimeout(5)
            recv_frame(sock)
    finally:
        for _ in range(server.rpc_capacity):
            server._rpc_slots.release()
    # The rejection path must not leak a slot: after releasing the manually
    # held ones, the full capacity is available again.
    assert server.rpc_metrics()['active'] == 0
    acquired = 0
    for _ in range(server.rpc_capacity):
        if server._rpc_slots.acquire(blocking=False):
            acquired += 1
    for _ in range(acquired):
        server._rpc_slots.release()
    assert acquired == server.rpc_capacity


def test_proven_predispatch_capacity_rejection_replays_command_safely(server):
    held = 0
    released = threading.Event()
    for _ in range(server.rpc_capacity):
        assert server._rpc_slots.acquire(blocking=False)
        held += 1

    def release_one_slot():
        server._rpc_slots.release()
        released.set()

    timer = threading.Timer(0.16, release_one_slot)
    timer.start()
    try:
        client = StorageClient(
            '127.0.0.1', server.server_address[1], TOKEN,
            timeout=2.0, read_attempts=3)
        assert client.command(
            'record.put', {
                'namespace': 'capacity-retry',
                'key': 'k',
                'value': {'ok': True},
            },
            command_id='capacity-retry-command',
        ) == {'stored': True}
        assert client.transport_metrics()[
            'pre_dispatch_command_retries'] == 1
        assert server.rpc_metrics()['rejected'] == 1
    finally:
        timer.cancel()
        timer.join(timeout=1.0)
        if released.is_set():
            held -= 1
        for _ in range(held):
            server._rpc_slots.release()
    _wait_for_active_handlers(server)


def test_short_burst_waits_for_a_released_slot_without_growing_capacity(server):
    held = 0
    for _ in range(server.rpc_capacity):
        assert server._rpc_slots.acquire(blocking=False)
        held += 1
    result = {}
    failure = {}

    def call_health():
        try:
            client = StorageClient(
                '127.0.0.1', server.server_address[1], TOKEN, timeout=2.0)
            result['value'] = client.health()
        except BaseException as exc:  # surfaced in the parent test thread
            failure['error'] = exc

    thread = threading.Thread(target=call_health, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 1.0
        while server.rpc_metrics()['waiting'] != 1:
            if time.monotonic() >= deadline:
                pytest.fail('request never entered the bounded admission wait')
            time.sleep(0.001)
        server._rpc_slots.release()
        held -= 1
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        assert failure == {}
        assert result['value'] == {'status': 'ok'}
        assert server.rpc_metrics()['rejected'] == 0
        _wait_for_active_handlers(server)
        assert server.rpc_metrics()['active'] == 0
    finally:
        for _ in range(held):
            server._rpc_slots.release()


def test_successful_rpc_releases_frame_bytes_and_records_both_directions(server):
    client = StorageClient(
        '127.0.0.1', server.server_address[1], TOKEN,
        timeout=2.0, read_attempts=1)

    assert client.health() == {'status': 'ok'}
    _wait_for_active_handlers(server)
    metrics = server.rpc_metrics()
    assert metrics['frame_bytes_inflight'] == 0
    assert metrics['request_frame_bytes_total'] > 0
    assert metrics['response_frame_bytes_total'] > 0


def test_frame_byte_pressure_rejects_before_dispatch_without_leaking_slot(
        server, monkeypatch):
    from lib.storage_sidecar import server as server_module

    monkeypatch.setattr(server_module, '_FRAME_BYTE_ADMISSION_WAIT_S', 0.0)
    capacity = server.rpc_metrics()['frame_bytes_capacity']
    assert server._frame_byte_admission.acquire(capacity, timeout_s=0.0)
    try:
        client = StorageClient(
            '127.0.0.1', server.server_address[1], TOKEN,
            timeout=2.0, read_attempts=1)
        with pytest.raises(StorageError) as captured:
            client.health()
        assert captured.value.code == 'database_unavailable'
        assert captured.value.retryable is True
    finally:
        server._frame_byte_admission.release(capacity)
    _wait_for_active_handlers(server)
    metrics = server.rpc_metrics()
    assert metrics['frame_bytes_inflight'] == 0
    assert metrics['frame_admission_rejections'] == 1


def test_distributed_preview_sidecar_rejects_commands_but_keeps_health_open():
    srv = create_server(_Backend(), TOKEN, read_only_preview=True)
    thread = threading.Thread(
        target=srv.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
    thread.start()
    try:
        client = StorageClient(
            '127.0.0.1', srv.server_address[1], TOKEN, timeout=2.0)
        assert client.health() == {'status': 'ok'}
        with pytest.raises(StorageError) as raised:
            client.command(
                'record.put',
                {'namespace': 'preview', 'key': 'blocked', 'value': True},
                'preview-command',
            )
        assert raised.value.code == 'database_unavailable'
        assert raised.value.retryable is True
        assert 'read-only' in raised.value.message
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)
