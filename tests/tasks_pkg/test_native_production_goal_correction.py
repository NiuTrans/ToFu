"""Owner correction survives actual Goal takeover and a second service crash.

Only the provider decisions and checkpoint pause are controlled. Public task,
review, facts, artifacts, account and native recovery owners remain production.
"""
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

from tests.support.native_production_review import DeliveryReview
from tests.tasks_pkg.test_native_production_prices import application, install_prices

pytestmark = [pytest.mark.api, pytest.mark.slow]


def published_checkpoint(directory, generation, client, path, errors):
    receipt = directory / f'goal-pending-{generation}.json'
    deadline = time.monotonic() + 60
    while not receipt.exists():
        turn = client.get(path).json()['turn']
        assert turn['status'] not in {'completed', 'failed', 'interrupted'}, turn
        assert not errors, errors
        assert time.monotonic() < deadline, turn
        time.sleep(.1)
    return json.loads(receipt.read_text())


def lose_service(process):
    children = psutil.Process(process.pid).children(recursive=True)
    # A selected daemon is an independent writer. Kill and join that child while
    # its application parent can still reap it, then abruptly lose the app.
    writers = [child for child in children if child.name() == 'tofu-db']
    for child in writers: child.kill()
    _, alive = psutil.wait_procs(writers, timeout=10)
    assert not alive, 'Storage writer death must be observed, not inferred'
    os.killpg(process.pid, signal.SIGKILL)
    assert process.wait(timeout=10) == -signal.SIGKILL
    _, alive = psutil.wait_procs(children, timeout=10)
    assert not [child for child in alive if child.status() != psutil.STATUS_ZOMBIE]


def latest_successor(client, conversation, after, *, terminal=False):
    deadline = time.monotonic() + 60
    while True:
        response = client.get(f'/api/v4/conversations/{conversation}/sync')
        assert response.status_code == 200, response.text
        turns = [turn for turn in response.json()['turns'] if turn['laneId'] == 'main'
                 and turn['actor'] == 'assistant' and turn['ordinal'] > after]
        if turns:
            turn = max(turns, key=lambda row: row['ordinal'])
            if not terminal or turn['status'] in {'completed', 'failed', 'interrupted'}:
                return turn
        assert time.monotonic() < deadline, response.text
        time.sleep(.1)


@pytest.mark.parametrize('transport', ['embedded', 'daemon'])
def test_goal_preserves_owner_scope_and_accepted_outputs_across_two_service_losses(tmp_path, transport):
    _exercise_goal(tmp_path, transport)


@pytest.mark.parametrize('transport', ['embedded', 'daemon'])
def test_goal_shutdown_joins_resumed_flow_before_releasing_native_code(tmp_path, transport):
    _exercise_goal(tmp_path, transport, shutdown_after_resume=True)


def _exercise_goal(tmp_path, transport, *, shutdown_after_resume=False):
    archive = os.environ.get('TOFU_NATIVE_TERMINAL_TEST_ARCHIVE')
    if not archive or not os.environ.get('TOFU_CHECKPOINT_CABI_TEST_LIBRARY'):
        pytest.skip('explicit source-matched native engine/archive required')
    project = tmp_path / 'project'; project.mkdir()
    ledger = '<!doctype html><html><head><title>Ledger</title></head><body>published once</body></html>\n'
    def report(region):
        return f'<!doctype html><html><head><title>Report</title></head><body>Report region: {region}</body></html>\n'
    bodies, errors = [], []
    resumed, release = threading.Event(), threading.Event()
    def call(name, arguments, identifier, index=0):
        return {'index': index, 'id': identifier, 'type': 'function',
                'function': {'name': name, 'arguments': json.dumps(arguments)}}
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                bodies.append(body); number = len(bodies)
                if number == 1:
                    delta = {'tool_calls': [call('write_file', dict(path='ledger.html', content=ledger,
                        append=True, description='Publish ledger once'), 'ledger'),
                        call('write_file', dict(path='report.html', content=report('west'),
                        description='Publish west report'), 'west', 1)]}
                elif number == 2:
                    resumed.set(); assert release.wait(60), 'Owner correction did not complete'
                    delta = {'tool_calls': [call('read_files', {'path': 'report.html'}, 'inspect-original')]}
                else:
                    assert 'Report region: east' in json.dumps(body), 'Recovered actor lost current owner requirements'
                    assert number <= 5, f'Unexpected model replay {number}'
                    if number == 3:
                        delta = {'tool_calls': [call('write_file', dict(path='report.html', content=report('east'),
                            description='Apply owner correction'), 'east')]}
                    else:
                        assert (project / 'report.html').read_text() == report('east')
                        delta = {'content': 'Report region: east verified; original ledger preserved. [VERDICT: STOP]'}
            except BaseException:
                errors.append(traceback.format_exc()); resumed.set()
                delta = {'content': 'Controlled provider decision failed'}
            wire = ('data: ' + json.dumps({'choices': [{'delta': delta,
                'finish_reason': 'tool_calls' if 'tool_calls' in delta else 'stop'}],
                'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}})
                + '\n\ndata: [DONE]\n\n').encode()
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(wire))); self.end_headers()
            try: self.wfile.write(wire)
            except (BrokenPipeError, ConnectionResetError): pass
    provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    thread = threading.Thread(target=provider.serve_forever, daemon=True); thread.start()
    conversation = 'goal-correction-' + uuid.uuid4().hex
    definition = dict(schema='tofu.orchestration/v1', name='Recover corrected delivery', nodes=[
        dict(id='start', type='control', kind='start'),
        dict(id='writer', type='role', role='worker', params=dict(model_override='alpha',
            objective='Publish ledger.html once and report.html for Report region: west; apply owner corrections.',
            tools=['read_files', 'write_file'])),
        dict(id='review', type='role', role='virtual_user', params=dict(objective='Verify current owner requirements.')),
        dict(id='stop', type='control', kind='stop')],
        edges=[{'from': a, 'to': b} for a, b in [('start','writer'),('writer','review'),('review','stop')]])
    config = dict(modelRef={'creator_id':'creator','model_id':'alpha'}, routing={'preferred_provider_id':'provider-a'},
        maxTokens=1024, thinkingEnabled=False, memoryEnabled=False, preferencesEnabled=False, projectEnabled=True,
        projectPath=str(project), autoApply=True, searchMode='off', tools={'programmaticCalling':'off'},
        flowDefinition=definition, executionLimits={'max_tokens':100000,'max_tool_calls':12,'deadline':time.time()+420})
    try:
        with application(tmp_path, archive, 1, transport=transport, goal_checkpoint=True,
                         crash_pending=True, goal_checkpoint_round=1) as (process, client, health1):
            port = client.base_url.port; install_prices(client, f'http://127.0.0.1:{provider.server_port}/v1')
            response = client.post(f'/api/v4/conversations/{conversation}/turns', json={
                'commandId':uuid.uuid4().hex, 'conversation':{'allowCreate':True,'title':'Goal correction',
                    'settings':{'projectPath':str(project)}}, 'inputTurn':{'content':definition['nodes'][1]['params']['objective']},
                'config':config})
            assert response.status_code == 200, response.text
            original = response.json()['turn']; original_id = original['turnId']
            path = f'/api/v4/conversations/{conversation}/turns/{original_id}'
            first = published_checkpoint(tmp_path, 1, client, path, errors)
            review = DeliveryReview(client, conversation, original_id)
            before = review.before_crash({'ledger':ledger,'report':report('west')})
            original_file = (project / 'ledger.html').stat(); lose_service(process)
        with application(tmp_path, archive, 2, port=port, transport=transport, goal_checkpoint=True,
                         crash_pending=True, goal_checkpoint_round=3) as (process, client, health2):
            assert health1['bootId'] != health2['bootId']; assert resumed.wait(60), errors
            assert not errors, errors
            successor = latest_successor(client, conversation, original['ordinal'])
            review = DeliveryReview(client, conversation, original_id)
            changed = review.revise(before)
            if shutdown_after_resume:
                # Leave the recovered actor waiting on its actual HTTP model
                # request. application() sends SIGTERM and checks joined exit.
                return
            release.set()
            path2 = f"/api/v4/conversations/{conversation}/turns/{successor['turnId']}"
            second = published_checkpoint(tmp_path, 2, client, path2, errors)
            assert (project / 'report.html').read_text() == report('east')
            assert review.artifact_review(before['artifacts']['ledger']) == before['ledger']
            lose_service(process)
        with application(tmp_path, archive, 3, port=port, transport=transport, goal_checkpoint=True) as (_, client, health3):
            assert health2['bootId'] != health3['bootId']
            final = latest_successor(client, conversation, successor['ordinal'], terminal=True)
            (tmp_path / 'final-turn.json').write_text(json.dumps(final, indent=2))
            assert final['status'] == 'completed', final
            assert not errors, errors
            review = DeliveryReview(client, conversation, final['turnId'])
            inherited = review.get(review.path)
            assert next(row for row in inherited['criteria'] if row['id']=='ledger')['state'] == 'pass'
            accepted = review.finish(before, {'ledger':ledger,'report':report('east')})
            current = (project / 'ledger.html').stat()
            assert (current.st_ino,current.st_mtime_ns) == (original_file.st_ino,original_file.st_mtime_ns)
            fees = final['projection']['apiRounds']
            assert len(fees) == len(bodies) == 5
            assert sum(row['usage']['total_tokens'] for row in fees) == 25
            assert [row['nativeCredits']['total'] for row in fees] == [42]*5
            assert {first['child_id'],second['child_id']} <= {row['taskId'] for row in fees}
            (tmp_path / 'review-evidence.json').write_text(json.dumps(dict(before=before,changed=changed,
                inherited=inherited,accepted=accepted),indent=2))
        with application(tmp_path, archive, 4, port=port, transport=transport, goal_checkpoint=True) as (_, client, _):
            assert DeliveryReview(client,conversation,final['turnId']).get(review.path) == accepted['accepted']
            assert len(bodies) == 5
    finally:
        release.set(); provider.shutdown(); provider.server_close(); thread.join(timeout=10)
        assert not thread.is_alive()
        (tmp_path/'provider-evidence.json').write_text(json.dumps(dict(bodies=bodies,errors=errors),indent=2))
