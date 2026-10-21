"""Real fetch responses keep recovery evidence without a second asset request."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from types import SimpleNamespace

import pytest

from lib.tools.result_envelope import tool_result_error
from lib.tools.result_value import tool_result_failure_verdict
from tests.tasks_pkg.test_tool_outcome_finality import observations as observations
from tests.tasks_pkg.test_tool_timeout_recovery import recovery_task

pytestmark = pytest.mark.api


@contextmanager
def unavailable_source(status=503, *, recover=False):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            requests.append(self.path)
            ok = self.path.endswith('?ok') or (recover and len(requests) > 3)
            body = b'{"evidence":"recovered-source"}' if ok else b''
            self.send_response(200 if ok else status)
            if not ok:
                self.send_header('Retry-After', '0' if recover else '3600')
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = False
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/source.json', requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.fixture
def fetch_environment(monkeypatch):
    import tofu_search.config as config
    from tofu_search.config import SearchConfig, bind_call_config
    from tofu_search.fetch import core, utils
    from lib.tasks_pkg.handlers.search import _core

    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy',
                'https_proxy', 'all_proxy', 'TOFU_SEARCH_PROXY_URL'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(utils, '_host_is_safe', lambda _host: True)
    monkeypatch.setattr(core, '_try_browser_fetch', lambda *_a, **_kw: None)
    monkeypatch.setattr(_core, 'filter_web_content', lambda content, **_kw: content)
    test_config = SearchConfig(proxy_url='', proxy_dual_attempt=False,
                               block_private_addresses=False)
    # Tool workers create their own execution contexts. Give those workers
    # the same owned offline configuration as direct calls, then restore it.
    monkeypatch.setattr(config, '_global_config', test_config)
    with bind_call_config(test_config):
        yield


def test_fetch_failure_keeps_server_wait_without_retrying_as_an_asset(fetch_environment):
    from lib.tasks_pkg.handlers.search._core import _fetch_url_one

    with unavailable_source() as (url, received):
        item = _fetch_url_one(url, 'read the source')
        assert received == ['/source.json'], 'asset fallback replayed an answered HTTP failure'
        assert not item['page_content'] and not item['is_asset']
        assert item['reason'] == 'fetch_failed:http_503'
        assert '3600 seconds' in item['error_msg'] and 'Retry-After' in item['error_msg']


def dispatch_fetch(task, arguments, number=0):
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    from lib.tasks_pkg.tool_display import _build_tool_round_entry

    call_id = f'fetch-{number}'
    serialized = json.dumps(arguments)
    _, row, _ = _build_tool_round_entry('fetch_url', arguments, call_id, serialized, number + 1, False)
    row['llmRound'] = number
    task.setdefault('toolRounds', []).append(row)
    call = {'id': call_id, 'type': 'function',
            'function': {'name': 'fetch_url', 'arguments': serialized}}
    messages, settled = [], []
    execute_tool_pipeline(task, [(call, 'fetch_url', call_id, arguments, number + 1, row, None)],
        cfg={'autoApply': True}, project_path=None, project_enabled=False, tool_list=[],
        messages=messages, all_search_results_text=[], round_num=number, model='test-model',
        internal_result_sink=SimpleNamespace(capture=lambda _id, value: settled.append(value)))
    return row, messages, settled


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('status', [429, 503])
@pytest.mark.parametrize('policy', ['read_only', 'idempotent', 'missing', 'reconcile'])
def test_actual_handler_keeps_failure_and_effect_authority(
        mode, status, policy, fetch_environment, observations):
    from lib.tasks_pkg.handlers.search._handlers import _handle_fetch_url

    task = recovery_task(_handle_fetch_url, policy, mode)
    with unavailable_source(status) as (url, received):
        row, messages, settled = dispatch_fetch(task, {'url': url})
    assert received == ['/source.json']
    error = tool_result_error(messages[0]['content'])
    repeatable = policy in {'read_only', 'idempotent'}
    expected = ('tool_http_rate_limited' if status == 429 else 'tool_http_unavailable')
    assert error.code == (expected if repeatable else 'tool_effect_unknown')
    assert error.retryable is repeatable
    if repeatable:
        assert '3600 seconds' in error.next_action and 'Retry-After' in error.next_action
    else:
        assert row['executionEffect']['operationId'] in error.message
    assert row['status'] == 'error' and row['_cacheableResult'] is False
    assert settled[0].execution_status == 'error'
    assert [a['outcome'] for a in observations[1]] == ['error']
    assert not task.get('_activeToolContexts')


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
def test_recovered_response_uses_a_new_occurrence_and_is_not_masked_by_failed_cache(
        mode, fetch_environment, observations):
    from lib.tasks_pkg.handlers.search._handlers import _handle_fetch_url

    task = recovery_task(_handle_fetch_url, 'read_only', mode)
    with unavailable_source(recover=True) as (url, received):
        failed, messages, _ = dispatch_fetch(task, {'url': url})
        assert tool_result_error(messages[0]['content']).code == 'tool_http_unavailable'
        assert received == ['/source.json'] * 3  # Existing two transport retries.
        success, messages, settled = dispatch_fetch(task, {'url': url}, 1)
    assert received == ['/source.json'] * 4
    assert 'recovered-source' in messages[0]['content']
    assert failed['status'] == 'error' and success['status'] == 'done'
    assert settled[0].execution_status == 'ok'
    assert failed['executionEffect']['operationId'] != success['executionEffect']['operationId']
    assert [a['outcome'] for a in observations[1]] == ['error', 'ok']


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('mixed', [False, True])
def test_batch_fetch_retains_every_response_and_marks_all_failed_batches(
        mode, mixed, fetch_environment, observations):
    from lib.tasks_pkg.handlers.search._handlers import _handle_fetch_url

    task = recovery_task(_handle_fetch_url, 'read_only', mode)
    with unavailable_source() as (url, received):
        row, messages, settled = dispatch_fetch(task, {'urls': [url, url + '?ok' if mixed else url]})
    assert sorted(received) == sorted(['/source.json', '/source.json?ok' if mixed else '/source.json'])
    assert '3600 seconds' in messages[0]['content'] and url in messages[0]['content']
    assert settled[0].execution_status == ('ok' if mixed else 'error')
    assert row['status'] == ('done' if mixed else 'error')
    assert len(row['results']) == 2
    if mixed:
        assert 'recovered-source' in messages[0]['content']


@pytest.mark.parametrize('lane', ['stream', 'paper'])
@pytest.mark.parametrize('batch', [False, True])
def test_fetch_adapters_preserve_failure_evidence(lane, batch, fetch_environment):
    from lib.tasks_pkg.handlers.search._handlers import _handle_fetch_url

    task = recovery_task(_handle_fetch_url, 'read_only')
    with unavailable_source() as (url, received):
        args = {'urls': [url, url]} if batch else {'url': url}
        if lane == 'stream':
            from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
            accumulator = StreamingToolAccumulator(task, project_path=None)
            try:
                content = accumulator._execute_one('fetch_url', args)
            finally:
                accumulator.close(wait=True)
            assert content.cacheable is False
        else:
            from lib.paper.tools import execute_paper_tool
            content = execute_paper_tool('fetch_url', json.dumps(args))[0]
    assert received == ['/source.json'] * (2 if batch else 1), str(content)[:400]
    assert tool_result_failure_verdict(content) == 'error'
    assert '3600 seconds' in content and 'Retry-After' in content
