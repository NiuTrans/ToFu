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


def test_frame_memory_threshold_selects_file_without_waiting():
    from lib.storage.frame_admission import FrameByteAdmission
    admission = FrameByteAdmission(capacity_bytes=10)
    first = admission.reserve_frame(9)
    second = admission.reserve_frame(2)
    assert not first.spilled and second.spilled
    assert admission.metrics()['frame_bytes_inflight'] == 11
    assert admission.metrics()['frame_admission_rejections'] == 0
    first.close(); second.close()
    assert admission.metrics()['frame_bytes_inflight'] == 0


def test_large_body_does_not_queue_smaller_independent_body():
    from lib.storage.frame_admission import FrameByteAdmission
    admission = FrameByteAdmission(capacity_bytes=10)
    large = admission.reserve_frame(100)
    small = admission.reserve_frame(2)
    assert large.spilled and not small.spilled
    assert admission.metrics()['frame_admission_waiting'] == 0
    large.close(); small.close()


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


def test_rpc_burst_starts_every_handler_beyond_legacy_capacity_hint(monkeypatch):
    count = 32
    entered = threading.Barrier(count)
    handlers = []
    def health():
        handlers.append(threading.current_thread())
        entered.wait(5)
        return {'status': 'ok'}
    srv = create_server(_Backend(), TOKEN, rpc_capacity=1)
    monkeypatch.setattr(srv.backend, 'health', health)
    server_thread = threading.Thread(target=srv.serve_forever,
                                     kwargs={'poll_interval': .02}, daemon=True)
    server_thread.start()
    from concurrent.futures import ThreadPoolExecutor
    start = time.perf_counter()
    try:
        with ThreadPoolExecutor(max_workers=count) as callers:
            def call(_):
                return StorageClient('127.0.0.1', srv.server_address[1], TOKEN,
                                     timeout=10, read_attempts=1).health()
            responses = list(callers.map(call, range(count)))
        _wait_for_active_handlers(srv, timeout=3)
        for handler in handlers: handler.join(3)
        assert all(not handler.is_alive() for handler in handlers)
        assert responses == [{'status': 'ok'}] * count
        metrics = srv.rpc_metrics()
        assert metrics['peak_active'] == count
        assert metrics['capacity'] == metrics['waiting'] == metrics['rejected'] == 0
        assert metrics['active'] == 0
        print('sql-sidecar-demand-burst handlers=32 hint=1 settled_ms=%.2f' % ((time.perf_counter()-start)*1000))
    finally:
        srv.shutdown(); srv.server_close(); server_thread.join(5)


def test_unpublished_worker_failure_releases_charge_and_classifies_no_dispatch(server, monkeypatch):
    from lib.storage_sidecar import server as owner
    request, peer = socket.socketpair()
    original = owner.threading.Thread.start
    def fail(thread):
        raise RuntimeError('injected worker creation failure')
    monkeypatch.setattr(owner.threading.Thread, 'start', fail)
    try:
        server.process_request(request, ('127.0.0.1', 1))
        response = recv_frame(peer)
        error = StorageError.from_payload(response['error'])
        assert error.request_not_dispatched and error.retryable
        assert server.rpc_metrics()['active'] == 0
        assert server.rpc_metrics()['rejected'] == 1
    finally:
        monkeypatch.setattr(owner.threading.Thread, 'start', original)
        request.close(); peer.close()


def test_started_worker_keeps_cleanup_ownership_if_start_raises(server, monkeypatch):
    from lib.storage_sidecar import server as owner
    from lib.storage.protocol import send_frame
    request, peer = socket.socketpair()
    peer.settimeout(3)
    original = owner.threading.Thread.start
    def publish_then_raise(thread):
        original(thread)
        raise RuntimeError('injected post-publication failure')
    monkeypatch.setattr(owner.threading.Thread, 'start', publish_then_raise)
    try:
        server.process_request(request, ('127.0.0.1', 1))
        send_frame(peer, {'protocol': PROTOCOL_VERSION, 'request_id': 'published',
                         'auth': TOKEN, 'kind': 'health', 'operation': 'system.health',
                         'payload': {}, 'deadline_unix_ms': int((time.time()+2)*1000)})
        assert recv_frame(peer)['result'] == {'status': 'ok'}
        peer.close()
        _wait_for_active_handlers(server)
        assert server.rpc_metrics()['active'] == 0
        assert server.rpc_metrics()['rejected'] == 0
    finally:
        monkeypatch.setattr(owner.threading.Thread, 'start', original)
        request.close(); peer.close()


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


def test_frame_memory_pressure_spills_request_and_keeps_reply_available(server):
    capacity = server.rpc_metrics()['frame_bytes_capacity']
    server._frame_byte_admission.acquire(capacity, timeout_s=0)
    try:
        client = StorageClient('127.0.0.1', server.server_address[1], TOKEN,
                               timeout=2, read_attempts=1)
        assert client.health() == {'status': 'ok'}
    finally:
        server._frame_byte_admission.release(capacity)
    _wait_for_active_handlers(server)
    metrics = server.rpc_metrics()
    assert metrics['frame_bytes_inflight'] == 0
    assert metrics['frame_admission_rejections'] == 0
    assert metrics['frame_buffer_spill_bytes_total'] > 0


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


def test_fragmented_response_finishes_before_bounded_handler_release(server,monkeypatch):
    from lib.storage.protocol import send_frame
    payload={'text':'x'*40000}
    monkeypatch.setattr(server.backend,'health',lambda:payload)
    with socket.create_connection(server.server_address,timeout=2.0) as channel:
        channel.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,4096)
        send_frame(channel,{'protocol':PROTOCOL_VERSION,'request_id':'fragmented',
            'auth':TOKEN,'kind':'health','operation':'system.health','payload':{},
            'deadline_unix_ms':int((time.time()+2)*1000)})
        time.sleep(0.02)
        assert recv_frame(channel)['result']==payload
        assert channel.recv(1)==b''
        assert server.rpc_metrics()['active']==1
        assert server.rpc_metrics()['frame_bytes_inflight']==0
    _wait_for_active_handlers(server)
    assert server.rpc_metrics()['frame_bytes_inflight']==0


def test_client_that_keeps_reply_socket_open_cannot_retain_handler(server,monkeypatch):
    from lib.storage.protocol import send_frame
    from lib.storage_sidecar import server as server_module
    original=server_module._StorageHandler._wait_for_peer_close
    def bounded(handler):
        handler.request.settimeout(0.05)
        return original(handler)
    monkeypatch.setattr(server_module._StorageHandler,'_wait_for_peer_close',bounded)
    with socket.create_connection(server.server_address,timeout=2.0) as channel:
        send_frame(channel,{'protocol':PROTOCOL_VERSION,'request_id':'slow-close',
            'auth':TOKEN,'kind':'health','operation':'system.health','payload':{},
            'deadline_unix_ms':int((time.time()+2)*1000)})
        assert recv_frame(channel)['ok'] is True
        _wait_for_active_handlers(server)
        assert server.rpc_metrics()['active']==0
        assert server.rpc_metrics()['frame_bytes_inflight']==0


def test_idle_heap_trim_cannot_hold_new_requests_behind_accounting_lock(server, monkeypatch):
    entered, resume = threading.Event(), threading.Event()
    server._idle_heap_trimmer.threshold_bytes = 1
    monkeypatch.setattr(server._idle_heap_trimmer, '_rss_bytes', lambda: 100)
    def slow_trim():
        entered.set()
        assert resume.wait(5)
        return True
    monkeypatch.setattr(server._idle_heap_trimmer, '_trim', slow_trim)
    client = StorageClient('127.0.0.1', server.server_address[1], TOKEN,
                           timeout=2, read_attempts=1)
    try:
        assert client.health() == {'status': 'ok'}
        assert entered.wait(2)
        assert client.health() == {'status': 'ok'}
        assert not resume.is_set()
    finally:
        resume.set()
    _wait_for_active_handlers(server)


@pytest.mark.parametrize('observed, expected', [('4096', 4096), ('0', 128), ('bad', 128), ('99999999999999', 128)])
def test_listen_backlog_probe_uses_live_capability_or_platform_fallback(monkeypatch, observed, expected):
    from tofu_db import _loopback
    import io
    monkeypatch.setattr(_loopback.sys, 'platform', 'linux')
    monkeypatch.setattr(_loopback.socket, 'SOMAXCONN', 128)
    monkeypatch.setattr(_loopback.Path, 'open', lambda *args, **kwargs: io.StringIO(observed))
    assert _loopback.listen_backlog_capacity() == expected


def test_listen_backlog_probe_failure_retains_portable_fallback(monkeypatch):
    from tofu_db import _loopback
    monkeypatch.setattr(_loopback.sys, 'platform', 'linux')
    monkeypatch.setattr(_loopback.socket, 'SOMAXCONN', 128)
    def unavailable(*args, **kwargs): raise OSError('unavailable kernel observation')
    monkeypatch.setattr(_loopback.Path, 'open', unavailable)
    assert _loopback.listen_backlog_capacity() == 128
