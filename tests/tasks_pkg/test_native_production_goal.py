"""Full production Goal startup, native actor capture and cold continuation."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import signal
import threading
import time
import traceback
import uuid

import psutil
import pytest

from tests.tasks_pkg.test_native_production_prices import application, install_prices
from tests.tasks_pkg.test_native_production_recovery import wait_turn

pytestmark = [pytest.mark.api, pytest.mark.slow]


@pytest.mark.parametrize('storage_transport,durability,kill_scope', [
    ('embedded', 'strict', 'group'), ('embedded', 'strict', 'application'),
    ('daemon', 'strict', 'application'), ('daemon', 'normal', 'application'),
])
def test_public_goal_recovers_published_actor_after_full_service_loss(tmp_path, storage_transport, durability, kill_scope, *, storage_backend='tofudb'):
    archive = os.environ.get('TOFU_NATIVE_TERMINAL_TEST_ARCHIVE')
    if not archive or (storage_backend == 'tofudb' and not os.environ.get('TOFU_CHECKPOINT_CABI_TEST_LIBRARY')):
        pytest.skip('explicit verified native archive and matching engine required')
    project = tmp_path / 'project'
    project.mkdir()
    bodies, errors = [], []
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                bodies.append(body)
                if len(bodies) == 1:
                    delta = {'tool_calls': [{'index': 0, 'id': 'publish-ledger', 'type': 'function',
                        'function': {'name': 'write_file', 'arguments': json.dumps(dict(
                            path='ledger.txt', append=True, content='once\n', description='Publish the verified ledger'))}}]}
                else:
                    assert (project / 'ledger.txt').read_text() == 'once\n'
                    delta = {'content': 'Ledger verified. [VERDICT: STOP]'}
            except BaseException:
                errors.append(traceback.format_exc())
                delta = {'content': 'Controlled provider fixture failed'}
            wire = ('data: ' + json.dumps({'choices': [{'delta': delta,
                'finish_reason': 'tool_calls' if 'tool_calls' in delta else 'stop'}],
                'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}})
                + '\n\ndata: [DONE]\n\n').encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(wire)))
            self.end_headers(); self.wfile.write(wire)

    provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    conversation = 'goal-production-' + uuid.uuid4().hex
    definition = dict(schema='tofu.orchestration/v1', name='Recover verified ledger', nodes=[
        dict(id='start', type='control', kind='start'),
        dict(id='writer', type='role', role='worker', params=dict(
            objective='Append once to ledger.txt and verify it.', model_override='alpha', tools=['write_file'])),
        dict(id='review', type='role', role='virtual_user', params=dict(objective='Verify the ledger.')),
        dict(id='stop', type='control', kind='stop')],
        edges=[{'from': a, 'to': b} for a, b in [('start', 'writer'), ('writer', 'review'), ('review', 'stop')]])
    config = dict(modelRef={'creator_id': 'creator', 'model_id': 'alpha'},
        routing={'preferred_provider_id': 'provider-a'}, maxTokens=1024,
        thinkingEnabled=False, memoryEnabled=False, projectEnabled=True,
        projectPath=str(project), autoApply=True, searchMode='off', preferencesEnabled=False,
        tools={'programmaticCalling': 'off'}, flowDefinition=definition,
        executionLimits={'max_tokens': 100000, 'max_tool_calls': 8, 'deadline': time.time() + 240})
    try:
        with application(tmp_path, archive, 1, goal_checkpoint=True, crash_pending=True,
                         transport=storage_transport, durability=durability, storage_backend=storage_backend) as (process, client, first_health):
            port = client.base_url.port
            install_prices(client, f'http://127.0.0.1:{provider.server_port}/v1')
            response = client.post(f'/api/v4/conversations/{conversation}/turns', json={
                'commandId': uuid.uuid4().hex,
                'conversation': {'allowCreate': True, 'title': 'Recover a running Goal',
                    'settings': {'projectPath': str(project)}},
                'inputTurn': {'content': 'Publish ledger.txt once and verify the result.'}, 'config': config})
            assert response.status_code == 200, response.text
            admitted = response.json()
            (tmp_path / 'admission.json').write_text(json.dumps(admitted, indent=2))
            path = f'/api/v4/conversations/{conversation}/turns/' + admitted['turn']['turnId']
            expires = time.monotonic() + 40
            while not (tmp_path / 'goal-pending.json').is_file():
                turn = client.get(path).json()['turn']
                (tmp_path / 'observed-turn.json').write_text(json.dumps(turn, indent=2))
                assert turn['status'] not in {'completed', 'failed', 'interrupted'}, turn
                assert time.monotonic() < expires, turn
                assert not errors, errors
                time.sleep(.1)
            pending = json.loads((tmp_path / 'goal-pending.json').read_text())
            assert pending['run_id']
            assert pending['billing_user'] is None
            assert (project / 'ledger.txt').read_text() == 'once\n'
            published = (project / 'ledger.txt').stat()
            children = psutil.Process(process.pid).children(recursive=True)
            observed = [dict(pid=child.pid, name=child.name()) for child in children]
            if storage_transport == 'daemon' and storage_backend == 'tofudb':
                assert any(child['name'] == 'tofu-db' for child in observed), observed
            alive = children
            try:
                (os.killpg if kill_scope == 'group' else os.kill)(process.pid, signal.SIGKILL)
                assert process.wait(timeout=10) == -signal.SIGKILL
                _, alive = psutil.wait_procs(children, timeout=15)
                live = [child.pid for child in alive if child.status() != psutil.STATUS_ZOMBIE]
                (tmp_path / 'owner-loss.json').write_text(json.dumps(dict(kill_scope=kill_scope,
                    transport=storage_transport, durability=durability, children=observed,
                    live_children_after_wait=live, observer_killed_children_before_observation=(kill_scope == 'group')), indent=2))
                assert not live, live
            finally:
                # A failed qualification still cleans up only these owned children.
                cleanup = []
                for child in alive:
                    try:
                        if child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
                            child.kill(); cleanup.append(child)
                    except psutil.NoSuchProcess: pass
                psutil.wait_procs(cleanup, timeout=10)
        with application(tmp_path, archive, 2, port=port, goal_checkpoint=True,
                         transport=storage_transport, durability=durability, storage_backend=storage_backend) as (_, client, second_health):
            assert first_health['bootId'] != second_health['bootId']
            original = wait_turn(client, path, {'interrupted'})
            expires = time.monotonic() + 45
            while True:
                response = client.get(f'/api/v4/conversations/{conversation}/sync')
                assert response.status_code == 200, response.text
                snapshot = response.json()
                (tmp_path / 'recovered-snapshot.json').write_text(json.dumps(snapshot, indent=2))
                successors = [turn for turn in snapshot['turns']
                    if turn['laneId'] == original['laneId'] and turn['actor'] == 'assistant'
                    and turn['ordinal'] > original['ordinal']]
                if successors:
                    recovered = max(successors, key=lambda turn: turn['ordinal'])
                    if recovered['status'] in {'completed', 'failed', 'interrupted'}:
                        break
                assert time.monotonic() < expires, snapshot
                time.sleep(.1)
            (tmp_path / 'recovered-turn.json').write_text(json.dumps(recovered, indent=2))
            assert recovered['status'] == 'completed', recovered
            assert (project / 'ledger.txt').read_text() == 'once\n'
            current = (project / 'ledger.txt').stat()
            assert (current.st_ino, current.st_mtime_ns) == (published.st_ino, published.st_mtime_ns)
            assert list(tmp_path.glob('restored-*.json'))
            fees = recovered['projection']['apiRounds']
            assert len(fees) == len(bodies) >= 3
            assert len({(row['taskId'], row['round']) for row in fees}) == len(fees)
            assert sum(row['usage']['total_tokens'] for row in fees) == 5 * len(bodies)
            assert [row['nativeCredits']['total'] for row in fees] == [42] * len(bodies)
            assert any(row['taskId'] == pending['child_id'] for row in fees)
            assert not errors, errors
    finally:
        provider.shutdown(); provider.server_close(); thread.join(timeout=10)
        assert not thread.is_alive()
        (tmp_path / 'provider-evidence.json').write_text(json.dumps(dict(bodies=bodies, errors=errors), indent=2))
