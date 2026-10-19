"""Real blocked socket reads must stop before the provider sends another byte."""
import threading

import pytest
import requests

from lib.llm import stream
from lib.llm import _transport
from lib.llm.llm_errors import AbortedError
from tests.sse.test_sse_http_delivery import _server

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('framing', ['close', 'length', 'chunked'])
@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('mode', ['stop', 'idle'])
def test_stop_interrupts_blocked_requests_read(monkeypatch, framing, legacy, mode):
    if legacy:
        from urllib3.response import HTTPResponse
        monkeypatch.setattr(HTTPResponse, 'shutdown', None, raising=False)
    if mode == 'idle':
        monkeypatch.setattr(_transport, 'IDLE_STREAM_TIMEOUT_S', .05)
    first = threading.Event()
    aborted = threading.Event()
    exited = threading.Event()
    results = []
    monkeypatch.setattr(_transport, 'ABORT_POLL_INTERVAL', .01)
    monkeypatch.setattr('lib.desktop.egress.route_request', lambda *a, **kw: 'direct')
    with _server(framing, False) as (url, release):
        with requests.Session() as session:
            session.trust_env = False
            class Session:
                def post(self, target, **kwargs):
                    return session.get(url, stream=True, timeout=(2, 5))
            monkeypatch.setattr(stream, 'get_sync_session', Session)
            def consume():
                try:
                    result = stream._stream_chat_once(
                        {'model': 'test', 'messages': [{'role': 'user', 'content': 'hi'}]},
                        base_url=url, on_content=lambda *args: first.set(),
                        abort_check=aborted.is_set)
                    results.append(result)
                except BaseException as error:
                    results.append(error)
                finally:
                    exited.set()
            worker = threading.Thread(target=consume, daemon=True)
            worker.start()
            try:
                assert first.wait(2), results
                if mode == 'stop':
                    aborted.set()
                early = exited.wait(.4)
            finally:
                release.set()
                worker.join(3)
            assert not worker.is_alive()
            assert early, 'Stop waited for the upstream to resume before read could exit'
            assert len(results) == 1
            result = results[0]
            if mode == 'stop':
                assert isinstance(result, AbortedError) or result.evidence.client_aborted
            else:
                assert result.usage.get('_missing_done') is True


@pytest.mark.parametrize('framing', ['close', 'length', 'chunked'])
@pytest.mark.parametrize('legacy', [False, True])
def test_error_diagnostics_expire_with_idle_timeout_disabled(monkeypatch, framing, legacy):
    from lib.llm import provider_error_body
    from lib.llm.llm_errors import RateLimitError
    if legacy:
        from urllib3.response import HTTPResponse
        monkeypatch.setattr(HTTPResponse, 'shutdown', None, raising=False)
    monkeypatch.setattr(_transport, 'IDLE_STREAM_TIMEOUT_S', 0)
    monkeypatch.setattr(provider_error_body, 'ERROR_BODY_TIMEOUT_S', .05)
    monkeypatch.setattr('lib.desktop.egress.route_request', lambda *a, **kw: 'direct')
    exited = threading.Event()
    results = []
    with _server(framing, False) as (url, release):
        with requests.Session() as session:
            session.trust_env = False
            class Session:
                def post(self, target, **kwargs):
                    response = session.get(url, stream=True, timeout=(2, 5))
                    response.status_code = 503
                    return response
            monkeypatch.setattr(stream, 'get_sync_session', Session)
            def consume():
                try:
                    results.append(stream._stream_chat_once(
                        {'model': 'test', 'messages': [{'role': 'user', 'content': 'hi'}]},
                        base_url=url))
                except BaseException as error:
                    results.append(error)
                finally:
                    exited.set()
            worker = threading.Thread(target=consume, daemon=True)
            worker.start()
            try:
                early = exited.wait(.5)
            finally:
                release.set()
                worker.join(3)
            assert not worker.is_alive()
            assert early, 'Known provider failure waited for the upstream to finish diagnostics'
            assert len(results) == 1
            assert isinstance(results[0], RateLimitError)
            assert results[0].status_code == 503
            assert 'hello' in str(results[0])


@pytest.mark.parametrize('already_running', [False, True])
def test_diagnostic_deadline_uses_one_watcher_and_wakes_long_wait(monkeypatch, already_running):
    created = []
    original = threading.Thread
    def track_thread(*args, **kwargs):
        worker = original(*args, **kwargs)
        created.append(worker)
        return worker
    monkeypatch.setattr(_transport.threading, 'Thread', track_thread)
    expired = threading.Event()
    watcher = _transport.StreamIdleWatchdog(
        heartbeat_interval=60 if already_running else 0,
        on_beat=lambda *args: None)
    watcher.start()
    watcher.arm_read_deadline(.03, expired.set)
    try:
        assert expired.wait(.4)
        assert watcher.read_deadline_expired
        assert len(created) == 1
    finally:
        watcher.cancel()
        for worker in created:
            worker.join(.3)
    assert all(not worker.is_alive() for worker in created)
