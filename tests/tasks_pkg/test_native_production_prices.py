"""Actual production HTTP native prices and durable output across process loss.

Uses synthetic provider/model facts admitted through the public routing CAS.
No production storage, authentication, pricing or execution owner is mocked.
This is a short completed-Turn restart proof, not a long-running model trial.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid

import httpx
import psutil
import pytest

from tests.model_routing.test_model_routing_contract import _full_document
from tests.support.model_registration import complete_registration

pytestmark = [pytest.mark.api, pytest.mark.slow]
ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def application(directory, archive, generation, *, file_checkpoint=False, crash_pending=False, port=None, unreconciled_batches=False, goal_checkpoint=False, goal_checkpoint_round=None, transport='embedded', durability='strict', storage_backend='tofudb'):
    assert storage_backend in {'tofudb', 'sqlite', 'postgres'}
    assert storage_backend == 'tofudb' or (transport, durability) == ('daemon', 'strict')
    assert (transport, durability) in {('embedded', 'strict'), ('daemon', 'strict'), ('daemon', 'normal')}
    if port is None:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
    config = directory / f'launch-{generation}.json'
    closed = directory / f'closed-{generation}.json'
    catalog_ready = directory / f'catalog-ready-{generation}'
    config.write_text(json.dumps({'directory': str(directory), 'archive': archive,
        'port': port, 'closed': str(closed), 'catalog_ready': str(catalog_ready),
        'file_checkpoint': file_checkpoint, 'crash_pending': crash_pending,
        'unreconciled_batches': unreconciled_batches, 'goal_checkpoint': goal_checkpoint,
        'goal_checkpoint_round': goal_checkpoint_round, 'generation': generation,
        'storage_backend': storage_backend}))
    environment = dict(os.environ, PYTHONPATH=str(ROOT), TOFU_DATA_DIR=str(directory),
        TOFU_STORAGE_PROJECT_ROOT=str(directory), TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE='1',
        TOFU_AUTH_MODE='open', TOFU_PERSONAL_STORAGE_BACKEND=('tofudb' if storage_backend == 'tofudb' else 'sqlite'),
        TOFUDB_TRANSPORT=transport, TOFU_DB_DURABILITY=durability, TOFU_PORT=str(port), TOFU_HOST='127.0.0.1',
        TOFU_MCP_CACHE_DIR=str(directory / 'mcp-cache'),
        UV_CACHE_DIR=str(directory / 'uv-cache'), UV_OFFLINE='1',
        npm_config_cache=str(directory / 'npm-cache'), npm_config_offline='true')
    if storage_backend == 'tofudb':
        environment.pop('TOFU_STORAGE_TEST_BACKEND', None)
    else:
        environment['TOFU_STORAGE_TEST_BACKEND'] = storage_backend
    for key in ('TOFU_STORAGE_CONNECTION_FILE', 'TOFU_STORAGE_TOKEN', 'TOFU_STORAGE_PARENT_PID',
                'LLM_API_KEY', 'LLM_API_KEYS'):
        environment.pop(key, None)
    log = directory / f'server-{generation}.log'
    with log.open('w') as output:
        process = subprocess.Popen([sys.executable, '-m', 'tests.support.native_production_server',
            str(config)], cwd=ROOT, env=environment, stdout=output,
            stderr=subprocess.STDOUT, start_new_session=True)
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=10) as client:
            expires = time.monotonic() + 90
            while True:
                assert process.poll() is None, log.read_text()[-5000:]
                assert time.monotonic() < expires, log.read_text()[-5000:]
                try:
                    response = client.get('/api/health')
                    health = response.json()
                    assert health.get('boot', {}).get('status') != 'failed', health
                    if health.get('boot', {}).get('status') == 'ready' and catalog_ready.is_file():
                        break
                except httpx.TransportError:
                    pass
                time.sleep(.2)
            storage = json.loads(catalog_ready.with_suffix('.storage.json').read_text())
            assert storage['transport'] == transport
            if storage_backend == 'tofudb':
                assert storage['durability'] == durability
            else:
                assert storage['backend'] == storage_backend and storage['protocol'] == 'storage.v1'
            assert health['boot']['phaseTotal'] == len(health['bootProfile']['phases'])
            yield process, client, health
    finally:
        if process.poll() is None:
            children = psutil.Process(process.pid).children(recursive=True)
            started = time.monotonic()
            process.send_signal(signal.SIGTERM)
            try:
                assert process.wait(timeout=30) == 0, log.read_text()[-5000:]
                assert json.loads(closed.read_text()) == {'nativeClients': 0, 'imageClosed': True}
                _, alive = psutil.wait_procs(children, timeout=10)
                live_children = [child.pid for child in alive if child.status() != psutil.STATUS_ZOMBIE]
                assert not live_children, live_children
                (directory / f'shutdown-{generation}.json').write_text(json.dumps({
                    'elapsed_seconds': time.monotonic() - started, 'exit_code': process.returncode,
                    'signal_target': 'application process', 'forced_group_kill': False,
                    'observed_child_pids': [child.pid for child in children],
                    'live_children_after_join': live_children,
                    'closed': json.loads(closed.read_text()),
                }, indent=2))
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=10)


def install_prices(client, endpoint, *, invalid=False):
    current = client.get('/api/v4/model-routing').json()['data']
    document = complete_registration(_full_document())
    document['connections'][0]['base_url'] = endpoint
    document['connections'][1]['enabled'] = False
    # Preserve the startup-owned catalogue; only private fixture identities
    # and offerings are replaced between generations of this test.
    for collection, identity in [('creators', ('creator_id',)), ('models', ('creator_id', 'model_id'))]:
        supplied = {tuple(row[key] for key in identity) for row in document[collection]}
        document[collection].extend(row for row in current['model_routing'][collection]
            if tuple(row[key] for key in identity) not in supplied)
    if invalid:
        document['offerings'][0]['actual_pricing'].update(input=0, cache_read=1)
    response = client.put('/api/v4/model-routing', json={
        'expected_revision': current['revision'], 'model_routing': document})
    assert response.status_code == 200, response.text
    assert all(row['registration_state'] == 'ready'
               for row in response.json()['data']['model_routing']['models'])


def execute(client, conversation):
    body = {'commandId': uuid.uuid4().hex,
        'inputTurn': {'content': 'Return the synthetic application result'},
        'conversation': {'allowCreate': True, 'title': 'Native production price check', 'settings': {}},
        'config': {'modelRef': {'creator_id': 'creator', 'model_id': 'alpha'},
            'routing': {'preferred_provider_id': 'provider-a'}, 'maxTokens': 64,
            'thinkingEnabled': False, 'memoryEnabled': False, 'projectEnabled': False,
            'searchMode': 'off', 'tools': {'programmaticCalling': 'off'},
            'executionLimits': {'max_tokens': 100000, 'max_tool_calls': 3,
                'deadline': time.time() + 60}}}
    response = client.post(f'/api/v4/conversations/{conversation}/turns', json=body)
    assert response.status_code == 200, response.text
    admitted = response.json()
    path = f'/api/v4/conversations/{conversation}/turns/' + admitted['turn']['turnId']
    expires = time.monotonic() + 45
    while True:
        response = client.get(path)
        assert response.status_code == 200, response.text
        turn = response.json()['turn']
        if turn['status'] in {'completed', 'failed', 'interrupted'}:
            return path, turn
        assert time.monotonic() < expires, turn
        time.sleep(.2)


def test_native_default_prices_and_completed_turn_survive_full_application_crash(tmp_path):
    archive = os.environ.get('TOFU_NATIVE_TERMINAL_TEST_ARCHIVE')
    if not archive or not os.environ.get('TOFU_CHECKPOINT_CABI_TEST_LIBRARY'):
        pytest.skip('explicit verified native archive and engine required')
    assert (ROOT / 'static/vite/manifest.json').is_file(), 'Run npm run build:frontend before the full-service test'
    (tmp_path / 'project').mkdir()
    requests = []
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert request['model'] == 'alpha-wire-a'
            requests.append(request)
            body = ('data: ' + json.dumps({'choices': [{'delta': {'content': 'native application ready'},
                'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 3, 'completion_tokens': 2,
                'total_tokens': 5}}) + '\n\ndata: [DONE]\n\n').encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)
    provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    thread = threading.Thread(target=provider.serve_forever, daemon=True); thread.start()
    endpoint = f'http://127.0.0.1:{provider.server_port}/v1'
    try:
        with application(tmp_path, archive, 1) as (process, client, first_health):
            first_pid = process.pid
            install_prices(client, endpoint)
            path, first = execute(client, 'service-' + uuid.uuid4().hex)
            (tmp_path / 'first-turn.json').write_text(json.dumps(first, indent=2))
            assert first['status'] == 'completed', first
            assert first['projection']['content'] == 'native application ready'
            assert first['projection']['usage']['total_tokens'] == 5
            assert len(requests) == 1
            assert first['projection']['apiRounds'][0]['nativePrice']['input'] == 5
            assert first['projection']['apiRounds'][0]['nativePrice']['output'] == 10
            assert not psutil.Process(process.pid).children(recursive=True)
            os.killpg(process.pid, signal.SIGKILL)
            assert process.wait(timeout=10) == -signal.SIGKILL
        with application(tmp_path, archive, 2) as (process, client, second_health):
            assert second_health['bootId'] != first_health['bootId']
            assert client.get(path).json()['turn'] == first
            _, after = execute(client, 'reopened-' + uuid.uuid4().hex)
            assert after['status'] == 'completed' and len(requests) == 2
            install_prices(client, endpoint, invalid=True)
            _, refused = execute(client, 'unrepresentable-' + uuid.uuid4().hex)
            assert refused['status'] == 'failed'
            assert len(requests) == 2, 'Unrepresentable rate reached paid model I/O'
            (tmp_path / 'rehearsal.json').write_text(json.dumps({
                'provider': 'synthetic', 'application_pids': [first_pid, process.pid],
                'boot_ids': [first_health['bootId'], second_health['bootId']],
                'startup_phases': [first_health['boot']['phaseTotal'], second_health['boot']['phaseTotal']],
                'completed_turn_unchanged_after_sigkill': True,
                'new_turn_completed_after_restart': after['status'] == 'completed',
                'provider_requests': len(requests), 'invalid_price_provider_requests': 0,
                'turn_before_crash': first, 'turn_after_restart': after,
                'refused_turn': refused,
            }, indent=2))
    finally:
        provider.shutdown(); provider.server_close(); thread.join(timeout=10)
        assert not thread.is_alive()
