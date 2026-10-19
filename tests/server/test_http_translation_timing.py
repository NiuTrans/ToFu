"""Translation-scoped HTTP evidence without changing Requests hook authority."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler
from types import SimpleNamespace
import threading

import pytest
import requests
from requests.hooks import dispatch_hook

from lib.http_transport import client as http_client
from lib.llm.diagnostic_timing import DiagnosticTiming, bind_timing
from tests.support.loopback import LoopbackThreadingHTTPServer

pytestmark = pytest.mark.unit


def test_response_headers_observed_before_nonstream_body_is_released():
    release_body, headers_seen = threading.Event(), threading.Event()
    records = []
    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_POST(self):
            self.send_response(200)
            self.send_header('Content-Length', '2')
            self.end_headers(); self.wfile.flush()
            if release_body.wait(5):
                self.wfile.write(b'ok')
    peer = LoopbackThreadingHTTPServer(('127.0.0.1', 0), Peer)
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    def emit(record):
        records.append(record)
        if record['event'] == 'phase_observation' and record.get('headers_observed'):
            headers_seen.set()
    timing = DiagnosticTiming(kind='synthetic', emit=emit)
    def request():
        with bind_timing(timing):
            return http_client.http_post(f'http://127.0.0.1:{peer.server_port}/',
                headers={'M-TraceId': 'physical-test-trace'}, data=b'', use_proxy=False, timeout=5)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(request)
            assert headers_seen.wait(3), 'hook did not observe headers before body'
            assert not pending.done(), 'nonstream request completed before body release'
            release_body.set()
            assert pending.result().content == b'ok'
        sample = next(row for row in timing.snapshot()['samples'] if row['phase'] == 'http_request')
        assert sample['headers_observed'] is True and sample['body_complete'] is True
        assert sample['trace_id'] == 'physical-test-trace'
        assert sample['response_body_ms'] >= 0 and sample['response_headers_ms'] >= 0
        assert 'first_content_ms' not in sample  # JSON response is not measured TTFT
    finally:
        release_body.set(); peer.shutdown(); peer.server_close(); thread.join()


@pytest.mark.parametrize('supplied', ['callable', 'list', 'empty', 'none'])
def test_hook_composition_preserves_replacement_and_caller_containers(monkeypatch, supplied):
    now, seen = [0.0], []
    replacement = SimpleNamespace(status_code=201)
    def caller(response, **_kwargs):
        seen.append(response.status_code)
        return replacement
    supplied_hooks = ({'response': caller} if supplied == 'callable' else
                      {'response': [caller]} if supplied == 'list' else
                      {'response': []} if supplied == 'empty' else None)
    original_list = list(supplied_hooks['response']) if supplied in ('list', 'empty') else None
    class Session:
        cookies = SimpleNamespace(clear=lambda: None)
        hooks = {'response': [caller]}
        def request(self, *_args, **kwargs):
            now[0] = 0.2
            response = dispatch_hook('response', kwargs['hooks'], SimpleNamespace(status_code=200))
            now[0] = 0.7
            return response
    session = Session()
    monkeypatch.setattr(http_client, '_sync_session', lambda: session)
    timing = DiagnosticTiming(kind='synthetic', clock=lambda: now[0], emit=lambda _record: None)
    with bind_timing(timing):
        result = http_client._sync_request('POST', 'https://example.invalid/', hooks=supplied_hooks)
    assert result is replacement and seen == [200]
    assert session.hooks == {'response': [caller]}
    if original_list is not None:
        assert supplied_hooks['response'] == original_list
    sample = next(row for row in timing.snapshot()['samples'] if row['phase'] == 'http_request')
    assert sample['response_headers_ms'] == 200
    assert sample['response_body_ms'] == 500
    assert sample['duration_ms'] == 700

    assert sample['status_code'] == 200  # actual header, not caller's 201 replacement
    assert timing.snapshot()['counters']['http_response_headers'] == 1


@pytest.mark.parametrize('failure_stage', ['connect', 'body'])
def test_failed_attempt_never_claims_body_completion(monkeypatch, failure_stage):
    class Session:
        cookies = SimpleNamespace(clear=lambda: None)
        hooks = {}
        def request(self, *_args, **kwargs):
            if failure_stage == 'body':
                dispatch_hook('response', kwargs['hooks'], SimpleNamespace(status_code=200))
            raise requests.exceptions.ReadTimeout('private response and credentials')
    monkeypatch.setattr(http_client, '_sync_session', Session)
    records = []
    timing = DiagnosticTiming(kind='synthetic', emit=records.append)
    with bind_timing(timing), pytest.raises(requests.exceptions.ReadTimeout):
        http_client._sync_request('POST', 'https://example.invalid/?secret=private')
    sample = next(row for row in timing.snapshot()['samples'] if row['phase'] == 'http_request')
    assert sample['body_complete'] is False and sample['outcome'] == 'error'
    assert sample['headers_observed'] is (failure_stage == 'body')
    assert 'private' not in str(records)


def test_stream_return_is_not_body_completion_and_redirect_headers_are_counted(monkeypatch):
    now = [0.0]
    class Session:
        cookies = SimpleNamespace(clear=lambda: None)
        hooks = {}
        def request(self, *_args, **kwargs):
            now[0] = 0.1
            dispatch_hook('response', kwargs['hooks'], SimpleNamespace(status_code=302))
            now[0] = 0.3
            result = dispatch_hook('response', kwargs['hooks'], SimpleNamespace(status_code=200))
            return result
    monkeypatch.setattr(http_client, '_sync_session', Session)
    timing = DiagnosticTiming(kind='synthetic', clock=lambda: now[0], emit=lambda _record: None)
    with bind_timing(timing):
        http_client._sync_request('GET', 'https://example.invalid/', stream=True)
    sample = next(row for row in timing.snapshot()['samples'] if row['phase'] == 'http_request')
    assert sample['header_observations'] == 2
    assert sample['first_response_headers_ms'] == 100 and sample['response_headers_ms'] == 300
    assert sample['body_complete'] is False and 'response_body_ms' not in sample


def test_broken_diagnostic_sink_does_not_change_hook_delivery(monkeypatch):
    result = SimpleNamespace(status_code=200)
    class Session:
        cookies = SimpleNamespace(clear=lambda: None)
        hooks = {}
        def request(self, *_args, **kwargs):
            return dispatch_hook('response', kwargs['hooks'], result)
    monkeypatch.setattr(http_client, '_sync_session', Session)
    def fail(_record):
        raise ValueError('sink failed')
    with bind_timing(DiagnosticTiming(kind='synthetic', emit=fail)):
        assert http_client._sync_request('POST', 'https://example.invalid/') is result
