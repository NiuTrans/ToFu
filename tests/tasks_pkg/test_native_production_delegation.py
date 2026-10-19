"""Public native delegation, live owner steering and durable reviewed outputs.

The controlled HTTP provider supplies decisions only. Production admission,
spawn/poll, child execution, accounting, artifacts and review use their owners.
"""
from collections import Counter
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
from tests.tasks_pkg.test_native_production_recovery import wait_turn

pytestmark = [pytest.mark.api, pytest.mark.slow]


@pytest.mark.parametrize('crash_pending', [False, True])
def test_public_child_observes_live_requirements_and_preserves_reviewed_delivery(tmp_path, crash_pending):
    archive = os.environ.get('TOFU_NATIVE_TERMINAL_TEST_ARCHIVE')
    if not archive or not os.environ.get('TOFU_CHECKPOINT_CABI_TEST_LIBRARY'):
        pytest.skip('explicit verified native archive and matching engine required')
    project = tmp_path / 'project'
    project.mkdir()
    ledger = '<!doctype html><html><head><title>Ledger</title></head><body>published once</body></html>\n'

    def report(region):
        return ('<!doctype html><html><head><title>Regional report</title></head>'
                f'<body><p>Report region: {region}</p></body></html>\n')

    bodies, errors = [], []
    counts = {'root': 0, 'child': 0}
    lock = threading.Lock()
    root_ready, root_release = threading.Event(), threading.Event()
    child_ready, child_release = threading.Event(), threading.Event()

    def call(name, arguments, identifier, index=0):
        return {'index': index, 'id': identifier, 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments)}}

    def delta_for(body):
        names = {tool['function']['name'] for tool in body.get('tools', [])}
        actor = 'root' if 'spawn_agents' in names else 'child'
        with lock:
            bodies.append({'actor': actor, 'body': body})
            counts[actor] += 1
            number = counts[actor]
        assert body['model'] == 'alpha-wire-a'
        if actor == 'root':
            if number == 1:
                return {'tool_calls': [
                    call('write_file', dict(description='Publish ledger once', path='ledger.html',
                        content=ledger, append=True), 'publish-ledger'),
                    call('write_file', dict(description='Publish initial west report', path='report.html',
                        content=report('west')), 'publish-west', 1)]}
            if number == 2:
                root_ready.set()
                assert root_release.wait(30), 'Initial owner review did not finish'
                return {'tool_calls': [call('spawn_agents', {'agents': [{'id': 'report-worker',
                    'role': 'general', 'tools': ['read_files', 'write_file'],
                    'objective': 'Update report.html for the current owner requirement. Original '
                        'assignment: Report region: west. Honor superseding owner requirements, '
                        'preserve ledger.html, and return concise verification.'}]}, 'delegate-report')]}
            if number == 3:
                assert 'report-worker' in json.dumps(body)
                return {'tool_calls': [call('poll', {'id': 'report-worker', 'wait_s': 30}, 'join-report')]}
            assert number == 4, f'Unexpected root request {number}'
            assert 'Delegated east report verified' in json.dumps(body)
            return {'content': 'Report region: east. Original ledger preserved and verified.'}
        assert {'read_files', 'write_file'} <= names, sorted(names)
        if number == 1:
            assert 'Report region: west' in json.dumps(body)
            child_ready.set()
            assert child_release.wait(30), 'Live owner change did not finish'
            return {'tool_calls': [call('read_files', {'path': 'report.html'}, 'inspect-report')]}
        assert 'Report region: east' in json.dumps(body), 'Current owner scope missing from child admission'
        if number == 2:
            return {'tool_calls': [call('write_file', dict(description='Apply current owner region',
                path='report.html', content=report('east')), 'publish-east')]}
        assert number == 3, f'Unexpected child request {number}'
        return {'content': 'Delegated east report verified'}

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                delta = delta_for(body)
            except BaseException:
                errors.append(traceback.format_exc())
                root_ready.set(); child_ready.set()
                delta = {'content': 'Controlled provider fixture failed'}
            wire = ('data: ' + json.dumps({'choices': [{'delta': delta,
                'finish_reason': 'tool_calls' if 'tool_calls' in delta else 'stop'}],
                'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}})
                + '\n\ndata: [DONE]\n\n').encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(wire)))
            try:
                self.end_headers(); self.wfile.write(wire)
            except (BrokenPipeError, ConnectionResetError):
                if not crash_pending:
                    errors.append(traceback.format_exc())

    provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    conversation = 'public-delegation-' + uuid.uuid4().hex
    config = {'modelRef': {'creator_id': 'creator', 'model_id': 'alpha'},
        'routing': {'preferred_provider_id': 'provider-a'}, 'maxTokens': 1024,
        'thinkingEnabled': False, 'memoryEnabled': False, 'projectEnabled': True,
        'projectPath': str(project), 'autoApply': True, 'searchMode': 'off',
        'tools': {'programmaticCalling': 'off'},
        'executionLimits': {'max_tokens': 100000, 'max_tool_calls': 12,
            'deadline': time.time() + 180}}
    try:
        with application(tmp_path, archive, 1) as (process, client, first_health):
            port = client.base_url.port
            install_prices(client, f'http://127.0.0.1:{provider.server_port}/v1')
            response = client.post(f'/api/v4/conversations/{conversation}/turns', json={
                'commandId': uuid.uuid4().hex,
                'conversation': {'allowCreate': True, 'title': 'Public delegated correction',
                    'settings': {'projectPath': str(project)}},
                'inputTurn': {'content': 'Publish ledger.html once and report.html for Report region: west. '
                    'Delegate report verification and apply current owner corrections.'}, 'config': config})
            assert response.status_code == 200, response.text
            admitted = response.json()
            turn_id = admitted['turn']['turnId']
            path = f'/api/v4/conversations/{conversation}/turns/{turn_id}'
            (tmp_path / 'admission.json').write_text(json.dumps(admitted, indent=2))
            try:
                assert root_ready.wait(30), 'Root did not publish initial outputs'
                assert not errors, errors
                review = DeliveryReview(client, conversation, turn_id)
                before = review.before_crash({'ledger': ledger, 'report': report('west')})
                original = (project / 'ledger.html').stat()
                root_release.set()
                assert child_ready.wait(30), 'Public spawn did not dispatch child'
                assert not errors, errors
                changed = review.revise(before)
                if not crash_pending:
                    child_release.set()
                    final = wait_turn(client, path, {'completed', 'failed', 'interrupted'})
                    (tmp_path / 'final-turn.json').write_text(json.dumps(final, indent=2))
                    assert not errors, errors
                    assert final['status'] == 'completed', final
                    assert counts == {'root': 4, 'child': 3}, counts
                    assert final['projection']['usage']['total_tokens'] == 20  # Root's own provider calls.
                    assert final['projection']['usageComplete'] is True
                    assert len(final['projection']['apiRounds']) == 7
                    assert sum(row['usage']['total_tokens'] for row in final['projection']['apiRounds']) == 35
                    assert sorted(Counter(
                        row['taskId'] for row in final['projection']['apiRounds']).values()) == [3, 4]
                    assert [row['nativeCredits']['total'] for row in final['projection']['apiRounds']] == [42] * 7
                    accepted = review.finish(before, {'ledger': ledger, 'report': report('east')})
                    review.inspect(before['artifacts']['report'], report('west'))
                    (tmp_path / 'reviewed-delivery.json').write_text(json.dumps({
                        'before': before, 'changed': changed, 'accepted': accepted}, indent=2))
                assert (project / 'ledger.html').read_text() == ledger
                current = (project / 'ledger.html').stat()
                assert (current.st_ino, current.st_mtime_ns) == (original.st_ino, original.st_mtime_ns)
                before_restart = dict(counts)
                children = psutil.Process(process.pid).children(recursive=True)
                os.killpg(process.pid, signal.SIGKILL)
                assert process.wait(timeout=10) == -signal.SIGKILL
                _, alive = psutil.wait_procs(children, timeout=10)
                assert not [child for child in alive if child.status() != psutil.STATUS_ZOMBIE]
            finally:
                root_release.set(); child_release.set()
        with application(tmp_path, archive, 2, port=port) as (_, client, second_health):
            assert first_health['bootId'] != second_health['bootId']
            restored = client.get(path)
            assert restored.status_code == 200, restored.text
            review = DeliveryReview(client, conversation, turn_id)
            if crash_pending:
                interrupted = wait_turn(client, path, {'interrupted', 'failed'})
                assert interrupted['status'] == 'interrupted', interrupted
                assert counts == before_restart, 'Restart launched a worker without native takeover'
                response = client.post(path + '/attempts', json={'commandId': uuid.uuid4().hex,
                    'operation': 'continue', 'expectedProjectionRevision': interrupted['projectionRevision'],
                    'config': config})
                assert response.status_code == 200, response.text
                refused = wait_turn(client, path, {'completed', 'failed', 'interrupted'})
                (tmp_path / 'refused-turn.json').write_text(json.dumps(refused, indent=2))
                assert refused['status'] == 'failed', refused
                assert counts == before_restart, 'Native child resumed without an acknowledged checkpoint'
                assert refused['settlement']['error']['kind'] == 'execution_token_usage_unknown'
                assert refused['projection']['usageComplete'] is False
                assert [row['nativeCredits'] for row in refused['projection']['apiRounds']] == [
                    row['nativeCredits'] for row in interrupted['projection']['apiRounds']]
                assert (project / 'report.html').read_text() == report('west')
                assert review.artifact_review(before['artifacts']['ledger']) == before['ledger']
            else:
                assert restored.json()['turn'] == final
                assert review.get(review.path) == accepted['accepted']
                for name, content in {'ledger': ledger, 'report': report('east')}.items():
                    review.inspect(accepted['artifacts'][name], content)
                assert counts == before_restart, 'Restart replayed completed delegated work'
            assert (project / 'ledger.html').read_text() == ledger
            current = (project / 'ledger.html').stat()
            assert (current.st_ino, current.st_mtime_ns) == (original.st_ino, original.st_mtime_ns)
    finally:
        root_release.set(); child_release.set()
        provider.shutdown(); provider.server_close(); thread.join(timeout=10)
        assert not thread.is_alive()
        (tmp_path / 'provider-evidence.json').write_text(json.dumps({
            'counts': counts, 'errors': errors, 'bodies': bodies}, indent=2))
