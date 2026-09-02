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
    trim_calls = []
    trimmer = _IdleHeapTrimmer(
        threshold_bytes=512,
        cooldown_s=30.0,
        clock=lambda: now[0],
        rss_bytes=lambda: next(rss_samples),
        trim=lambda: trim_calls.append(True) or True,
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


def _wait_for_active_handlers(server, *, timeout: float = 1.0) -> None:
    """Wait for the server thread's post-response slot release."""
    deadline = time.monotonic() + timeout
    while server.rpc_metrics()['active']:
        if time.monotonic() >= deadline:
            pytest.fail('RPC handler did not release its active slot')
        time.sleep(0.001)


class _Backend:
    """Only health is reachable in the short-burst admission test."""

    @staticmethod
    def health():
        return {'status': 'ok'}


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
