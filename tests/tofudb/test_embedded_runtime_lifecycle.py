"""Concurrent Python callers keep the native FFI handle alive through teardown."""
from concurrent.futures import ThreadPoolExecutor
import threading
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
