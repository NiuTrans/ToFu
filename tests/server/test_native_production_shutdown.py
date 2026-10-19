"""Production shutdown joins an active native model and a live SSE consumer.

Uses the real application/transport/storage owners and a private stalled HTTP
provider. No completed model response or external cost is claimed.
"""
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import time
import uuid

import httpx
import psutil
import pytest

from tests.tasks_pkg.test_native_production_prices import application, install_prices
from tests.tasks_pkg.test_native_production_recovery import wait_turn

pytestmark = [pytest.mark.api, pytest.mark.slow]


def test_shutdown_joins_active_native_model_before_storage_release(tmp_path, *, browser_observer=None):
    archive = os.environ.get('TOFU_NATIVE_TERMINAL_TEST_ARCHIVE')
    if not archive or not os.environ.get('TOFU_CHECKPOINT_CABI_TEST_LIBRARY'):
        pytest.skip('explicit verified native archive and matching engine required')
    (tmp_path / 'project').mkdir()
    entered, release, joined = threading.Event(), threading.Event(), threading.Event()
    calls = []
    provider_failures = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            if len(calls) != 1:
                self.send_error(500, 'Unresolved usage must prevent another dispatch')
                return
            try:
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"Partial model result"}}]}\n\n')
                self.wfile.flush()
                entered.set()
                deadline = time.monotonic() + 120
                while not release.wait(.25):
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Fixture provider was not released')
                    # Keep the real stream alive while the browser attaches;
                    # neither a finish marker nor usage has been delivered.
                    self.wfile.write(b': pending\n\n')
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass  # The application owns cancellation of this request.
            except Exception as error:
                provider_failures.append(str(error))
            finally:
                joined.set()

    provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    conversation = 'active-shutdown-' + uuid.uuid4().hex
    config = {'modelRef': {'creator_id': 'creator', 'model_id': 'alpha'},
        'routing': {'preferred_provider_id': 'provider-a'}, 'maxTokens': 64,
        'thinkingEnabled': False, 'memoryEnabled': False, 'projectEnabled': False,
        'searchMode': 'off', 'tools': {'programmaticCalling': 'off'},
        'executionLimits': {'max_tokens': 100000, 'max_tool_calls': 3,
            'deadline': time.time() + 180}}
    try:
        # The HTTP observer remains open until after the service has exited.
        with ExitStack() as observers:
            with application(tmp_path, archive, 1) as (process, client, health):
                port = client.base_url.port
                install_prices(client, f'http://127.0.0.1:{provider.server_port}/v1')
                response = client.post(f'/api/v4/conversations/{conversation}/turns', json={
                    'commandId': uuid.uuid4().hex,
                    'conversation': {'allowCreate': True, 'title': 'Active model shutdown', 'settings': {}},
                    'inputTurn': {'content': 'Wait for the complete provider result.'},
                    'config': config})
                assert response.status_code == 200, response.text
                path = f'/api/v4/conversations/{conversation}/turns/' + response.json()['turn']['turnId']
                assert entered.wait(20), 'Provider request did not start'
                assert client.get(path).json()['turn']['status'] == 'running'
                native = [child for child in psutil.Process(process.pid).children(recursive=True)
                          if 'run-http-agent' in child.cmdline()]
                assert native, 'No live native executor at shutdown'
                observer = observers.enter_context(httpx.Client(base_url=client.base_url,
                    trust_env=False, timeout=10))
                stream = observers.enter_context(observer.stream('GET',
                    f'/api/v4/conversations/{conversation}/events'))
                assert stream.status_code == 200
                assert stream.headers['content-type'].startswith('text/event-stream')
                assert next(stream.iter_lines())
                assert not release.is_set(), 'The provider completed before shutdown'
                if browser_observer:
                    browser_observer('before_shutdown', client, conversation, None)
                assert client.get(path).json()['turn']['status'] == 'running'
                assert all(child.is_running() for child in native)
            shutdown = json.loads((tmp_path / 'shutdown-1.json').read_text())
            assert set(child.pid for child in native) <= set(shutdown['observed_child_pids'])
            assert not shutdown['live_children_after_join']
            assert not release.is_set(), 'Provider completion masked cancellation'

        # Reopen the same authority. Unknown usage must fence even an explicit
        # Continue whose untrusted config claims the checkpoint was complete.
        with application(tmp_path, archive, 2, port=port) as (_, client, reopened):
            assert reopened['bootId'] != health['bootId']
            turn = wait_turn(client, path, {'failed', 'interrupted', 'completed'})
            assert turn['status'] == 'interrupted', turn
            assert turn['settlement']['cause'] == 'server_restart', turn['settlement']
            assert turn['settlement']['evidence'] == 'system_recovery', turn['settlement']
            assert turn['projection']['usageComplete'] is False
            assert len(calls) == 1, 'Restart replayed an unresolved provider request'
            response = client.post(path + '/attempts', json={
                'commandId': uuid.uuid4().hex, 'operation': 'continue',
                'expectedProjectionRevision': turn['projectionRevision'],
                'config': {**config, 'checkpointUsageComplete': True}})
            assert response.status_code == 200, response.text
            refused = wait_turn(client, path, {'failed', 'completed', 'interrupted'})
            assert refused['status'] == 'failed', refused
            assert refused['projection']['usageComplete'] is False
            assert len(calls) == 1, 'Unknown usage permitted another paid dispatch'
            error = refused['settlement']['error']
            assert error['kind'] == 'execution_token_usage_unknown'
            assert error['retryable'] is False
            assert error['titleKey'] == 'err.k.execution_token_usage_unknown.title'
            if browser_observer:
                browser_observer('recovery_refused', client, conversation, error)
            (tmp_path / 'active-shutdown-evidence.json').write_text(json.dumps({
                'native_pids_before_shutdown': [child.pid for child in native],
                'provider_requests': len(calls), 'provider_was_pending': True,
                'shutdown': shutdown, 'turn_after_restart': turn, 'refused_continue': refused,
            }, indent=2))
        with application(tmp_path, archive, 3, port=port) as (_, client, cold):
            assert cold['bootId'] != reopened['bootId']
            persisted = client.get(path).json()['turn']
            assert persisted['settlement']['error'] == error
            assert persisted['projection']['usageComplete'] is False
            assert len(calls) == 1
            if browser_observer:
                browser_observer('cold_replay', client, conversation, error)
            (tmp_path / 'cold-error.json').write_text(json.dumps(persisted, indent=2))
    finally:
        release.set()
        provider.shutdown(); provider.server_close(); thread.join(timeout=10)
        assert not thread.is_alive()
        if entered.is_set():
            assert joined.wait(10)
        assert not provider_failures, provider_failures
