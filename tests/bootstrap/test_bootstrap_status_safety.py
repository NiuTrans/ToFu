"""Bounded bootstrap control-plane regressions; no installs or app services."""
import http.client
import io
import json
import os
import stat
import threading
import time

import pytest

from bootstrap_pkg import launcher, runtime, status_page
from tofu_dotenv import parse_dotenv

pytestmark = pytest.mark.unit


def test_event_bus_replay_and_subscribers_are_bounded():
    bus = runtime.EventBus()
    for n in range(1000):
        bus.emit('log', str(n))
    completed = threading.Event()
    queues = []

    def subscribe():
        queues.append(bus.subscribe())
        completed.set()

    threading.Thread(target=subscribe, daemon=True).start()
    assert completed.wait(1), 'replay deadlocked after 201 events'
    assert queues[0].qsize() == bus.MAX_EVENTS
    assert queues[0].get_nowait()['data'] == '800'
    for _ in range(bus.MAX_SUBSCRIBERS - 1):
        queues.append(bus.subscribe())
    with pytest.raises(RuntimeError):
        bus.subscribe()
    bus.emit('log', 'new')  # full slow subscribers never block producers
    for q in queues:
        bus.unsubscribe(q)
    for _ in range(100):
        bus.emit('log', '界' * 30000)
    assert bus._history_bytes <= bus.MAX_BYTES
    assert len(bus._history) <= bus.MAX_EVENTS
    q = bus.subscribe()
    assert len(q.get_nowait()['data'].encode()) <= bus.MAX_EVENT_BYTES


@pytest.fixture
def page(tmp_path, monkeypatch):
    monkeypatch.setattr(status_page, 'BASE_DIR', str(tmp_path))
    monkeypatch.setattr(runtime, '_bus', runtime.EventBus())
    monkeypatch.setattr(runtime, '_restart_requested', False)
    for name in ('LLM_API_KEYS', 'LLM_BASE_URL', 'LLM_MODEL'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(status_page.providers, '_load_provider_templates', lambda: [])
    monkeypatch.setattr(status_page.providers, '_bootstrap_discover_models', lambda *a, **k: [])
    monkeypatch.setattr(status_page.providers, '_bootstrap_persist_provider', lambda *a, **k: None)
    server = status_page._start_status_server('0.0.0.0', 0)
    assert server.server_address[0] == '127.0.0.1'
    yield server, tmp_path
    status_page._stop_status_server(server)


def request(server, body=None, headers=None, path='/bootstrap/save-config'):
    conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
    supplied = {'Origin': f'http://127.0.0.1:{server.server_port}', 'Content-Type': 'application/json'}
    supplied.update(headers or {})
    conn.request('POST', path, json.dumps(body) if body is not None else '{}', supplied)
    response = conn.getresponse()
    result = response.status, response.read()
    conn.close()
    return result


@pytest.mark.parametrize('body', [[], {'api_key': 123}, {'api_key': 'a\nPORT=9'},
    {'api_key': 'a\rX=y'}, {'api_key': 'a\x00'}, {'api_key': 'a\u2028X=y'},
    {'api_key': 'a' * 4097}, {'api_key': '"quoted"'},
    {'api_key': 'ok', 'custom_model': 'yes'}, {'api_key': 'ok', 'port': 0},
    {'api_key': 'ok', 'base_url': 'http://localhost:65536/v1'},
    {'api_key': 'ok', 'base_url': 'file:///tmp/config'}])
def test_invalid_fields_never_write(page, body):
    server, root = page
    assert request(server, body)[0] == 400
    assert not (root / '.env').exists()
    assert not runtime.consume_restart_request()


@pytest.mark.parametrize('headers', [
    {'Host': 'evil.test'}, {'Origin': 'http://evil.test'},
    {'Origin': 'null'}, {'Sec-Fetch-Site': 'cross-site'},
])
def test_cross_origin_and_rebinding_are_denied(page, headers):
    server, root = page
    assert request(server, {'api_key': 'ok'}, headers)[0] == 403
    assert not (root / '.env').exists()


def test_body_limit_and_missing_origin(page):
    server, root = page
    assert request(server, {'api_key': 'x' * 20000})[0] == 413
    conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
    conn.request('POST', '/bootstrap/save-config', '{}', {'Content-Type': 'application/json'})
    assert conn.getresponse().status == 403
    conn.close()
    assert not (root / '.env').exists()


def test_save_and_shutdown_with_active_sse(page):
    server, root = page
    path = root / '.env'
    path.write_text('# keep\nOTHER="a=b # data"\nLLM_API_KEYS=old\nLLM_API_KEYS=older\nLAST=end')
    path.chmod(0o644)
    conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
    conn.request('GET', '/bootstrap/events')
    response = conn.getresponse()
    assert response.status == 200
    code, payload = request(server, {'api_key': 'new', 'model': 'private'})
    assert code == 200 and json.loads(payload)['ok']
    text = path.read_text()
    assert '# keep\nOTHER="a=b # data"\n' in text
    assert parse_dotenv(text) == {'OTHER': 'a=b # data', 'LAST': 'end', 'LLM_API_KEYS': 'new', 'LLM_MODEL': 'private'}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    start = time.monotonic()
    server.shutdown()
    assert time.monotonic() - start < 2
    deadline = time.monotonic() + 2
    while runtime._bus._subscribers and time.monotonic() < deadline:
        time.sleep(.01)
    assert not runtime._bus._subscribers
    conn.close()


def test_http_concurrency_has_a_hard_cap(page):
    import socket
    server, _ = page
    sockets = []
    try:
        for _ in range(server.MAX_HANDLERS):
            sockets.append(socket.create_connection(server.server_address, timeout=2))
        deadline = time.monotonic() + 2
        while len(server._connections) < server.MAX_HANDLERS and time.monotonic() < deadline:
            time.sleep(.01)
        assert len(server._connections) == server.MAX_HANDLERS
        extra = socket.create_connection(server.server_address, timeout=2)
        try:
            assert extra.recv(1) == b''
        finally:
            extra.close()
        assert len(server._connections) == server.MAX_HANDLERS
        server.shutdown()
    finally:
        for connection in sockets:
            connection.close()


def test_atomic_save_failure_preserves_original(tmp_path, monkeypatch):
    monkeypatch.setattr(status_page, 'BASE_DIR', str(tmp_path))
    path = tmp_path / '.env'
    path.write_text('OTHER=original\n')
    def fail(*args):
        raise OSError('injected')
    monkeypatch.setattr(status_page.os, 'replace', fail)
    with pytest.raises(OSError):
        status_page._write_config({'LLM_API_KEYS': 'new'})
    assert path.read_text() == 'OTHER=original\n'
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize('rc', [1, 0, 130, 143, -9])
def test_stderr_tail_preserves_exit_behaviour(monkeypatch, rc):
    output = 'x' * 200000 + '\nModuleNotFoundError: final\n'
    class Child:
        stderr = io.StringIO(output)
        def wait(self):
            return rc
    monkeypatch.setattr(launcher.subprocess, 'Popen', lambda *a, **k: Child())
    monkeypatch.setattr(launcher, 'install_process_resource_defaults', lambda env: None)
    monkeypatch.setattr(launcher.sys, 'stderr', io.StringIO())
    if rc in (0, 130, 143):
        with pytest.raises(SystemExit) as exc:
            launcher._try_start_server()
        assert exc.value.code == 0
    else:
        ok, tail, result = launcher._try_start_server()
        assert not ok and result == rc
        assert len(tail) == 65536
        assert tail.endswith('ModuleNotFoundError: final\n')
    assert launcher.sys.stderr.getvalue() == output


def test_diagnostic_fallback_never_reconfigures_application_port(monkeypatch):
    from types import SimpleNamespace
    choices = []
    class Server:
        server_port = 23457
        def serve_forever(self): pass
    def bind(address, handler):
        choices.append(address)
        if len(choices) == 1: raise OSError('busy')
        return Server()
    monkeypatch.setattr(status_page, '_QuietServer', bind)
    monkeypatch.setattr(status_page, '_find_free_port', lambda *a: 23457)
    monkeypatch.setattr(status_page.threading, 'Thread', lambda **k: SimpleNamespace(start=lambda: None))
    monkeypatch.setenv('PORT', '23456')
    assert status_page._start_status_server('0.0.0.0', 23456).server_port == 23457
    assert os.environ['PORT'] == '23456'
    assert choices == [('127.0.0.1', 23456), ('127.0.0.1', 23457)]
