"""Real native SQL admission failures retain failure attribution through Goal UI."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import time
import uuid

import pytest

from tests.tasks_pkg.test_native_production_prices import application, install_prices
from tests.tasks_pkg.test_native_sql_production_recovery import sql_application

pytestmark = [pytest.mark.api, pytest.mark.slow]


def test_goal_checkpoint_refusal_is_failed_before_provider_dispatch(tmp_path, sql_application):
    archive = os.environ.get('TOFU_NATIVE_TERMINAL_TEST_ARCHIVE')
    if not archive:
        pytest.skip('explicit verified native archive required')
    requests = []
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            requests.append(self.path)
            self.send_error(500, 'Provider must not run after checkpoint refusal')
    provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    project = tmp_path/'project'; project.mkdir()
    definition = dict(schema='tofu.orchestration/v1', name='Refused checkpoint', nodes=[
        dict(id='start', type='control', kind='start'),
        dict(id='writer', type='role', role='worker', params=dict(objective='Write ledger.txt', model_override='alpha', tools=['write_file'])),
        dict(id='review', type='role', role='virtual_user', params=dict(objective='Verify ledger.txt')),
        dict(id='stop', type='control', kind='stop')],
        edges=[{'from': a, 'to': b} for a,b in [('start','writer'),('writer','review'),('review','stop')]])
    config = dict(modelRef={'creator_id':'creator','model_id':'alpha'}, routing={'preferred_provider_id':'provider-a'},
        maxTokens=1024, thinkingEnabled=False, memoryEnabled=False, projectEnabled=True, projectPath=str(project),
        autoApply=True, searchMode='off', preferencesEnabled=False, tools={'programmaticCalling':'off'},
        flowDefinition=definition, executionLimits={'max_tokens':100000,'max_tool_calls':8,'deadline':time.time()+120})
    conversation = 'goal-refusal-'+uuid.uuid4().hex
    try:
        # Valid application policy exceeds this selected SQL authority's explicit
        # identity ceiling. Its real native create rejects before model effects.
        bounds = dict(header_bytes=8<<20, snapshot_bytes=8<<20, identity_bytes=8192, evidence_bytes=8<<20)
        with application(tmp_path, archive, 1, goal_checkpoint=True, goal_checkpoint_bounds=bounds,
                         transport='daemon', storage_backend=sql_application) as (_, client, _health):
            install_prices(client, f'http://127.0.0.1:{provider.server_port}/v1')
            response = client.post(f'/api/v4/conversations/{conversation}/turns', json={
                'commandId':uuid.uuid4().hex, 'conversation':{'allowCreate':True,'title':'Checkpoint refusal',
                    'settings':{'projectPath':str(project)}}, 'inputTurn':{'content':'Write ledger.txt'}, 'config':config})
            assert response.status_code == 200, response.text
            admitted = response.json()
            path = f'/api/v4/conversations/{conversation}/turns/'+admitted['turn']['turnId']
            expires = time.monotonic()+40
            while True:
                turn = client.get(path).json()['turn']
                if turn['status'] in {'completed','failed','interrupted'}: break
                assert time.monotonic() < expires, turn
                time.sleep(.1)
            (tmp_path/'refused-turn.json').write_text(json.dumps(turn, indent=2))
            assert not requests and not (project/'ledger.txt').exists()
            assert turn['status'] == 'failed', turn
            assert turn['settlement']['cause'] != 'user_abort'
            assert turn['settlement']['error'], turn
    finally:
        provider.shutdown(); provider.server_close(); thread.join(timeout=10)
        assert not thread.is_alive()
