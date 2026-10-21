"""Real loopback HTTP failures recover on a later admitted tool occurrence."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import ExitStack
import threading

import httpx
import pytest
import requests

from lib.tasks_pkg.executor._finalize import _finalize_tool_round
from lib.tools.result_envelope import tool_result_error
from tests.tasks_pkg.test_tool_outcome_finality import dispatch, observations as observations
from tests.tasks_pkg.test_tool_timeout_recovery import recovery_task

pytestmark = pytest.mark.api


@pytest.mark.parametrize('client_kind', ['httpx', 'requests'])
@pytest.mark.parametrize('failure_kind', ['disconnect', 'timeout', '429', '503'])
@pytest.mark.parametrize('mode', ['legacy', 'v2'])
def test_real_http_failure_preserves_progress_and_allows_next_read(
        client_kind, failure_kind, mode, observations, record_property):
    release, failed_request_finished = threading.Event(), threading.Event()
    received, workers, failures = [], [], []
    lock = threading.Lock()

    class Server(ThreadingHTTPServer):
        daemon_threads = False  # server_close joins every owned request.

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            with lock:
                received.append(self.path)
                workers.append(threading.current_thread())
                first = len(received) == 1
            if first:
                try:
                    if failure_kind == 'timeout':
                        release.wait(5)
                    elif failure_kind in {'429', '503'}:
                        self.send_response(int(failure_kind))
                        self.send_header('Retry-After', '0')
                        self.send_header('Content-Length', '0')
                        self.end_headers()
                    self.close_connection = True
                finally:
                    failed_request_finished.set()
                return
            body = b'recovered source evidence'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def handler(task, tc, name, call_id, arguments, number, row, *rest):
        try:
            response = client.get(url, timeout=.15 if failure_kind == 'timeout' else 2)
            response.raise_for_status()
        except (httpx.HTTPError, requests.exceptions.RequestException) as failure:
            failures.append(type(failure).__module__ + '.' + type(failure).__name__)
            raise
        result = response.text
        _finalize_tool_round(task, number, row, [{'content': result}])
        return call_id, result, False

    task = recovery_task(handler, 'read_only', mode)
    with ExitStack() as cleanup:
        client = cleanup.enter_context(
            httpx.Client(trust_env=False) if client_kind == 'httpx' else requests.Session())
        if client_kind == 'requests':
            client.trust_env = False
        server = Server(('127.0.0.1', 0), Handler)
        cleanup.callback(server.server_close)
        url = f'http://127.0.0.1:{server.server_port}/source'
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
        thread.start()
        cleanup.callback(thread.join, timeout=5)
        cleanup.callback(server.shutdown)
        cleanup.callback(release.set)
        failed_row, failed_messages, failed_settled = dispatch(task)
        failure = tool_result_error(failed_messages[0]['content'])
        expected = {
            'timeout': 'tool_execution_timeout', 'disconnect': 'tool_connection_failed',
            '429': 'tool_http_rate_limited', '503': 'tool_http_unavailable',
        }[failure_kind]
        assert failure.code == expected and failure.retryable
        if failure_kind in {'429', '503'}:
            assert 'Retry-After' in failure.next_action and '0 seconds' in failure.next_action
            assert 'normal' in failure.next_action and 'authorization' in failure.next_action
        assert failed_settled[0].execution_status == 'error'
        assert received == ['/source'], 'failure projection repeated an HTTP request'
        release.set()
        assert failed_request_finished.wait(5)
        row, messages, settled = dispatch(task, call_id='occurrence-2', round_num=1)
        assert 'recovered source evidence' in messages[0]['content']
        assert row['status'] == 'done' and settled[0].execution_status == 'ok'
        assert failed_row['status'] == 'error' and len(task['toolRounds']) == 2
        assert failed_row['executionEffect']['operationId'] != row['executionEffect']['operationId']
        assert received == ['/source', '/source'] and len(failures) == 1
        assert [event['outcome'] for event in observations[1]] == ['error', 'ok']
        assert not task.get('_activeToolContexts')
        record_property('actual_http_exception', failures[0])
    assert not thread.is_alive() and all(not worker.is_alive() for worker in workers)
