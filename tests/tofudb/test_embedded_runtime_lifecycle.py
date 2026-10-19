"""Concurrent Python callers keep the native FFI handle alive through teardown."""
from concurrent.futures import ThreadPoolExecutor
import threading
import sys
from types import SimpleNamespace

import pytest

from tofu_db.embedded import EmbeddedRuntime, EmbeddedTransportError, _ffi

pytestmark = pytest.mark.unit


def test_close_drains_concurrent_calls_and_rejects_new_callers():
    runtime = EmbeddedRuntime.__new__(EmbeddedRuntime)
    runtime._handle = _ffi.cast('void*', 1)
    close_waiting = threading.Event()

    class ObservedCondition(threading.Condition):
        def wait(self, timeout=None):
            close_waiting.set()
            return super().wait(timeout)

    runtime._lifecycle = ObservedCondition()
    runtime._active_calls = 0
    runtime._closing = False
    entered = threading.Barrier(3)
    release = threading.Event()
    native_closed = threading.Event()
    close_started = threading.Event()

    def execute(*args, **kwargs):
        entered.wait(timeout=5)
        assert release.wait(timeout=5)
        assert not native_closed.is_set()
        return {'status': 0}

    runtime._execute_json = execute
    runtime._lib = SimpleNamespace(tofudb_embed_close=lambda handle: native_closed.set())

    def close():
        close_started.set()
        runtime.close()

    with ThreadPoolExecutor(max_workers=3) as pool:
        calls = [pool.submit(runtime.execute_json, 'record.get', {},
                             command_id=None, deadline_unix_ms=1) for _ in range(2)]
        entered.wait(timeout=5)
        closing = pool.submit(close)
        assert close_started.wait(timeout=5)
        assert close_waiting.wait(timeout=5)
        assert not native_closed.is_set()
        with pytest.raises(EmbeddedTransportError, match='closed'):
            runtime.execute_json('record.get', {}, command_id=None, deadline_unix_ms=1)
        release.set()
        assert all(call.result(timeout=5) == {'status': 0} for call in calls)
        closing.result(timeout=5)
    assert native_closed.is_set() and runtime.closed
    runtime.close()


@pytest.mark.parametrize('accelerated', [False, True])
@pytest.mark.parametrize('size', [64, 2 * 1024 * 1024])
def test_decoded_responses_own_values_after_buffer_reuse(monkeypatch, accelerated, size):
    import json
    import tofu_db.embedded as embedded
    monkeypatch.setattr(embedded, '_fast_json', pytest.importorskip('orjson') if accelerated else None)
    monkeypatch.setattr(embedded, '_thread_state', threading.local())
    runtime = EmbeddedRuntime.__new__(EmbeddedRuntime)
    runtime._handle = _ffi.cast('void*', 1)
    runtime._lifecycle = threading.Condition()
    runtime._active_calls = 0
    runtime._closing = False
    current = {'status': 0, 'payload': {'text': '汉字\\\"\n' + 'x' * size,
                                       'array': [None, True, -1, 1.25, {'nested': 'retained'}]}}
    def execute(handle, request, request_len, output, capacity):
        encoded = json.dumps(current, ensure_ascii=False).encode('utf-8')
        if len(encoded) > capacity:
            return -len(encoded)
        _ffi.memmove(output, encoded, len(encoded))
        return len(encoded)
    runtime._lib = SimpleNamespace(tofudb_embed_execute=execute)
    first = runtime.execute_json('record.get', {}, command_id=None, deadline_unix_ms=1)
    expected = json.loads(json.dumps(current))
    current['payload']['text'] = 'y' * size
    current['payload']['array'] = [False, {'nested': 'replaced'}]
    second = runtime.execute_json('record.get', {}, command_id=None, deadline_unix_ms=1)
    assert first == expected
    assert second == current
    second['payload']['array'][1]['nested'] = 'caller mutation'
    assert first == expected


@pytest.mark.parametrize('accelerated', [False, True])
@pytest.mark.parametrize('encoded', [b'{"payload":"\xff"}', b'{"payload":', b'{"payload": [1,]}'])
def test_invalid_native_response_still_raises(monkeypatch, accelerated, encoded):
    import tofu_db.embedded as embedded
    monkeypatch.setattr(embedded, '_fast_json', pytest.importorskip('orjson') if accelerated else None)
    runtime = EmbeddedRuntime.__new__(EmbeddedRuntime)
    runtime._handle = _ffi.cast('void*', 1)
    runtime._lifecycle = threading.Condition()
    runtime._active_calls = 0
    runtime._closing = False
    def execute(handle, request, request_len, output, capacity):
        _ffi.memmove(output, encoded, len(encoded))
        return len(encoded)
    runtime._lib = SimpleNamespace(tofudb_embed_execute=execute)
    with pytest.raises((ValueError, UnicodeError)):
        runtime.execute_json('record.get', {}, command_id=None, deadline_unix_ms=1)


@pytest.mark.parametrize('accelerated', [False, True])
def test_concurrent_response_reuse_preserves_each_callers_results(monkeypatch, accelerated):
    import json
    import tofu_db.embedded as embedded
    monkeypatch.setattr(embedded, '_fast_json', pytest.importorskip('orjson') if accelerated else None)
    monkeypatch.setattr(embedded, '_thread_state', threading.local())
    runtime = EmbeddedRuntime.__new__(EmbeddedRuntime)
    runtime._handle = _ffi.cast('void*', 1)
    runtime._lifecycle = threading.Condition()
    runtime._active_calls = 0
    runtime._closing = False
    concurrent = threading.Barrier(4)
    def execute(handle, request, request_len, output, capacity):
        body = json.loads(request[:request_len])['payload']
        concurrent.wait(timeout=5)
        encoded = json.dumps({'status': 0, 'payload': body}).encode('utf-8')
        _ffi.memmove(output, encoded, len(encoded))
        return len(encoded)
    runtime._lib = SimpleNamespace(tofudb_embed_execute=execute)
    def caller(identity):
        retained = []
        for turn in range(8):
            body = {'identity': identity, 'turn': turn, 'text': str(identity) * 65536}
            retained.append((runtime.execute_json('record.get', body, command_id=None,
                                                 deadline_unix_ms=1), body))
        for actual, expected in retained:
            assert actual == {'status': 0, 'payload': expected}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(caller, identity) for identity in range(4)]
        for future in futures:
            future.result(timeout=15)


@pytest.mark.parametrize('written', [65, -64, -(sys.maxsize + 1)])
def test_invalid_native_response_capacity_fails_without_read_or_reexecution(monkeypatch, written):
    import tofu_db.embedded as embedded
    calls = []
    runtime = EmbeddedRuntime.__new__(EmbeddedRuntime)
    runtime._handle = _ffi.cast('void*', 1)
    def execute(*args):
        calls.append(args[-1])
        return written
    runtime._lib = SimpleNamespace(tofudb_embed_execute=execute)
    out = _ffi.new('uint8_t[]', 64)
    monkeypatch.setattr(embedded, '_response_buffer', lambda: (out, 64))
    def forbidden(*args):
        raise AssertionError('invalid native capacity must not allocate, read or retry')
    monkeypatch.setattr(embedded, '_grow_response_buffer', forbidden)
    monkeypatch.setattr(embedded, '_loads', forbidden)
    with pytest.raises(EmbeddedTransportError):
        runtime._execute_json('system.health', {}, command_id=None, deadline_unix_ms=0)
    assert calls == [64]



def test_response_growth_has_no_private_byte_quota(monkeypatch):
    import tofu_db.embedded as embedded
    needed = 64 * 1024 * 1024 + 1
    capacities = []
    runtime = EmbeddedRuntime.__new__(EmbeddedRuntime)
    runtime._handle = _ffi.cast('void*', 1)
    def execute(handle, request, request_size, out, capacity):
        capacities.append(capacity)
        if capacity < needed:
            return -needed
        _ffi.buffer(out, 2)[:] = b'{}'
        return 2
    runtime._lib = SimpleNamespace(tofudb_embed_execute=execute)
    monkeypatch.setattr(embedded, '_thread_state', SimpleNamespace())
    assert runtime._execute_json('system.health', {}, command_id=None, deadline_unix_ms=0) == {}
    assert capacities == [embedded._RESPONSE_INITIAL_BYTES, needed]


def test_actual_response_allocation_failure_remains_visible_without_reexecution(monkeypatch):
    import tofu_db.embedded as embedded
    calls = []
    runtime = EmbeddedRuntime.__new__(EmbeddedRuntime)
    runtime._handle = _ffi.cast('void*', 1)
    def execute(*args):
        calls.append(args[-1])
        return -128
    runtime._lib = SimpleNamespace(tofudb_embed_execute=execute)
    monkeypatch.setattr(embedded, '_response_buffer', lambda: (_ffi.new('uint8_t[]', 64), 64))
    def fail_allocation(needed):
        raise MemoryError('injected allocation failure')
    monkeypatch.setattr(embedded, '_grow_response_buffer', fail_allocation)
    with pytest.raises(EmbeddedTransportError, match='allocation failed') as failure:
        runtime._execute_json('system.health', {}, command_id=None, deadline_unix_ms=0)
    assert isinstance(failure.value.__cause__, MemoryError)
    assert calls == [64]


@pytest.mark.parametrize('graceful', [False, True])
@pytest.mark.parametrize('last_port', ['storage', 'checkpoint'])
@pytest.mark.parametrize('last_fails', [False, True])
def test_teardown_waits_for_last_call_even_after_another_caller_returns(
    graceful, last_port, last_fails,
):
    """Both stopping callers join the final admitted call, including exceptions."""
    import json
    from tofu_db.generated_checkpoint_control import FORMAT

    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    stop_waiting = [threading.Event(), threading.Event()]
    native_stops = []

    class ObservedCondition(threading.Condition):
        waits = 0

        def wait(self, timeout=None):
            if self.waits < 2:
                stop_waiting[self.waits].set()
            self.waits += 1
            return super().wait(timeout)

    runtime = EmbeddedRuntime.__new__(EmbeddedRuntime)
    runtime._handle = _ffi.cast('void*', 1)
    runtime._lifecycle = ObservedCondition()
    runtime._active_calls = 0
    runtime._closing = False

    def operation(index):
        entered[index].set()
        assert release[index].wait(5)
        assert not native_stops
        if index == 1 and last_fails:
            raise RuntimeError('injected final call failure')
        return {'status': 0}

    runtime._execute_json = lambda _op, payload, **kw: operation(payload['index'])

    def checkpoint(handle, request, length, out, capacity):
        operation(1)
        encoded = json.dumps({'format': FORMAT, 'accepted': True}).encode()
        _ffi.memmove(out, encoded, len(encoded))
        return len(encoded)

    runtime._lib = SimpleNamespace(
        tofudb_embed_checkpoint_supported=lambda: 1,
        tofudb_embed_checkpoint_execute=checkpoint,
        tofudb_embed_close=lambda handle: native_stops.append('close'),
        tofudb_embed_abandon=lambda handle: native_stops.append('abandon'),
    )
    stop = runtime.close if graceful else runtime.abandon
    with ThreadPoolExecutor(max_workers=4) as pool:
        first = pool.submit(runtime.execute_json, 'record.get', {'index': 0},
                            command_id=None, deadline_unix_ms=1)
        if last_port == 'storage':
            last = pool.submit(runtime.execute_json, 'record.get', {'index': 1},
                               command_id=None, deadline_unix_ms=1)
        else:
            last = pool.submit(runtime.checkpoint_control, 'load', {'format': FORMAT},
                               deadline_unix_ms=1, response_bytes=4096)
        assert all(event.wait(5) for event in entered)
        stopping = pool.submit(stop)
        assert stop_waiting[0].wait(5)
        joining = pool.submit(stop)
        assert stop_waiting[1].wait(5)
        release[0].set()
        assert first.result(5) == {'status': 0}
        assert not native_stops and not stopping.done() and not joining.done()
        with pytest.raises(EmbeddedTransportError, match='closed'):
            runtime.execute_json('record.get', {}, command_id=None, deadline_unix_ms=1)
        release[1].set()
        if last_fails:
            with pytest.raises(RuntimeError, match='final call failure'):
                last.result(5)
        else:
            last.result(5)
        stopping.result(5)
        joining.result(5)
    assert native_stops == ['close' if graceful else 'abandon']
    assert runtime.closed


def test_failed_response_growth_preserves_previous_owned_buffer(monkeypatch):
    import tofu_db.embedded as embedded
    monkeypatch.setattr(embedded, '_thread_state', threading.local())
    old_buffer, old_capacity = embedded._response_buffer()
    _ffi.memmove(old_buffer, b'owned', 5)

    def exhausted(*args):
        raise MemoryError('injected allocation failure')

    with monkeypatch.context() as patch:
        patch.setattr(embedded, '_ffi', SimpleNamespace(new=exhausted))
        with pytest.raises(MemoryError, match='allocation failure'):
            embedded._grow_response_buffer(old_capacity + 1)
    retained_buffer, retained_capacity = embedded._response_buffer()
    assert retained_buffer is old_buffer and retained_capacity == old_capacity
    assert bytes(_ffi.buffer(retained_buffer, 5)) == b'owned'
    grown_buffer, grown_capacity = embedded._grow_response_buffer(old_capacity + 1)
    assert grown_buffer is not old_buffer and grown_capacity == old_capacity + 1
    assert bytes(_ffi.buffer(old_buffer, 5)) == b'owned'
