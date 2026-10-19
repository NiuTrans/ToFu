"""Whole production service loss while a paid file batch awaits checkpoint ACK.

Real HTTP admission/Continue, selected embedded storage and native execution;
only the provider response and explicit crash barrier are synthetic.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import threading
import time
import uuid

import psutil
import pytest

from tests.tasks_pkg.test_native_production_prices import application, install_prices

pytestmark = [pytest.mark.api, pytest.mark.slow]


def wait_turn(client, path, statuses):
    expires = time.monotonic() + 45
    while True:
        response = client.get(path)
        assert response.status_code == 200, response.text
        turn = response.json()['turn']
        if turn['status'] in statuses:
            return turn
        assert time.monotonic() < expires, turn
        time.sleep(.1)


@pytest.mark.parametrize('revise_accepted', [False, True])
def test_pending_file_survives_full_service_loss_and_public_continue(tmp_path, revise_accepted, *, browser_observer=None, mutate_file=False, hide_file_evidence=False, ordinary_tool=False, unreconciled_batch=False, storage_backend='tofudb'):
    assert not mutate_file or not revise_accepted
    assert not hide_file_evidence or not (revise_accepted or mutate_file)
    assert not (ordinary_tool or unreconciled_batch) or not (revise_accepted or mutate_file or hide_file_evidence)
    assert not (ordinary_tool and unreconciled_batch)
    archive = os.environ.get('TOFU_NATIVE_TERMINAL_TEST_ARCHIVE')
    if not archive or (storage_backend == 'tofudb' and not os.environ.get('TOFU_CHECKPOINT_CABI_TEST_LIBRARY')):
        pytest.skip('explicit verified native archive and matching engine required')
    project = tmp_path / 'project'
    project.mkdir()
    bodies = []
    ledger_path = 'ledger.html' if revise_accepted else 'ledger.txt'
    ledger_content = ('<!doctype html><html><head><title>Publication ledger</title></head>'
        '<body><p>published once</p></body></html>\n') if revise_accepted else 'published once\n'
    def report(region):
        return ('<!doctype html><html><head><title>Regional report</title></head>'
                f'<body><p>Report region: {region}</p></body></html>\n')

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert body['model'] == 'alpha-wire-a'
            bodies.append(body)
            delta = {'tool_calls': [{'index': 0, 'id': 'publish-once', 'type': 'function',
                'function': {'name': 'write_file', 'arguments': json.dumps({
                    'description': 'Append the delivery ledger once', 'path': ledger_path,
                    'content': ledger_content, 'append': True})}}]} if len(bodies) == 1 else {
                        'content': 'File publication verified after service restart.'}
            if revise_accepted:
                if len(bodies) == 1:
                    delta['tool_calls'].append({'index': 1, 'id': 'report-west', 'type': 'function',
                        'function': {'name': 'write_file', 'arguments': json.dumps({
                            'description': 'Publish the requested west-region report',
                            'path': 'report.html', 'content': report('west')})}})
                elif len(bodies) == 2:
                    assert 'Report region: east' in json.dumps(body), 'Changed owner requirement did not reach the provider'
                    delta = {'tool_calls': [{'index': 0, 'id': 'report-east', 'type': 'function',
                        'function': {'name': 'write_file', 'arguments': json.dumps({
                            'description': 'Revise only the report for the changed region',
                            'path': 'report.html', 'content': report('east')})}}]}
                else:
                    delta = {'content': 'Report region: east. Original ledger preserved and verified.'}
            read = {'index': 0, 'id': 'inspect-ledger', 'type': 'function',
                'function': {'name': 'read_files', 'arguments': json.dumps({'path': ledger_path})}}
            if ordinary_tool and len(bodies) == 2:
                delta = {'tool_calls': [read]}
            if unreconciled_batch and len(bodies) == 1:
                delta['tool_calls'].append(dict(read, index=1))
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
    conversation = 'active-service-' + uuid.uuid4().hex
    config = {'modelRef': {'creator_id': 'creator', 'model_id': 'alpha'},
        'routing': {'preferred_provider_id': 'provider-a'}, 'maxTokens': 1024,
        'thinkingEnabled': False, 'memoryEnabled': False, 'projectEnabled': True,
        'projectPath': str(project), 'autoApply': True, 'searchMode': 'off',
        'tools': {'programmaticCalling': 'off'},
        'executionLimits': {'max_tokens': 100000, 'max_tool_calls': 3,
            'deadline': time.time() + 180}}
    try:
        with application(tmp_path, archive, 1, file_checkpoint=True, crash_pending=True,
                unreconciled_batches=ordinary_tool or unreconciled_batch, storage_backend=storage_backend,
                transport='embedded' if storage_backend == 'tofudb' else 'daemon') as (process, client, first_health):
            port = client.base_url.port
            install_prices(client, f'http://127.0.0.1:{provider.server_port}/v1')
            response = client.post(f'/api/v4/conversations/{conversation}/turns', json={
                'commandId': uuid.uuid4().hex,
                'conversation': {'allowCreate': True, 'title': 'Active service recovery',
                    'settings': {'projectPath': str(project)}},
                'inputTurn': {'content': f'Append published once to {ledger_path} once, then verify the file.' +
                    (' Also produce report.html for Report region: west.' if revise_accepted else '')},
                'config': config})
            assert response.status_code == 200, response.text
            admitted = response.json()
            path = f'/api/v4/conversations/{conversation}/turns/' + admitted['turn']['turnId']
            (tmp_path / 'admission.json').write_text(json.dumps(admitted, indent=2))
            expires = time.monotonic() + 45
            marker = tmp_path / 'pending-file.json'
            while not marker.exists():
                turn = client.get(path).json()['turn']
                assert turn['status'] not in {'failed', 'completed', 'interrupted'}, turn
                assert process.poll() is None
                assert time.monotonic() < expires, turn
                time.sleep(.1)
            pending = json.loads(marker.read_text())
            assert pending['consumption'] == [5, 2 if revise_accepted or unreconciled_batch else 1, True]
            assert len(bodies) == 1
            assert (project / ledger_path).read_text() == ledger_content
            original = (project / ledger_path).stat()
            before = None
            if revise_accepted:
                from tests.support.native_production_review import DeliveryReview
                before = DeliveryReview(client, conversation, pending['turn_id']).before_crash(
                    {'ledger': ledger_content, 'report': report('west')})
                (tmp_path / 'accepted-before-crash.json').write_text(json.dumps(before, indent=2))
                if browser_observer:
                    browser_observer('accepted_before_crash', client, conversation, pending['turn_id'], before)
            children = psutil.Process(process.pid).children(recursive=True)
            os.killpg(process.pid, signal.SIGKILL)
            assert process.wait(timeout=10) == -signal.SIGKILL
            _, alive = psutil.wait_procs(children, timeout=10)
            assert not [child for child in alive if child.status() != psutil.STATUS_ZOMBIE]

        if mutate_file:
            (project / ledger_path).write_text('external edit after process loss\n')
            original = (project / ledger_path).stat()
        if hide_file_evidence:
            evidence = Path(pending['file_evidence_directory']).resolve()
            assert evidence.is_relative_to(tmp_path.resolve()), 'Fault injection must stay in the private test authority'
            evidence.rename(evidence.with_name(evidence.name + '.unavailable'))

        with application(tmp_path, archive, 2, file_checkpoint=True, port=port,
                unreconciled_batches=ordinary_tool or unreconciled_batch, storage_backend=storage_backend,
                transport='embedded' if storage_backend == 'tofudb' else 'daemon') as (process, client, second_health):
            assert first_health['bootId'] != second_health['bootId']
            interrupted = wait_turn(client, path, {'interrupted', 'failed'})
            assert interrupted['status'] == 'interrupted', interrupted
            assert len(bodies) == 1, 'Restart automatically replayed paid work'
            inspection = None
            if hide_file_evidence:
                reference = pending['tool_rounds'][0]['executionEffect']['inspection']
                response = client.get(path + '/tool-effects/' + reference['operationId'],
                    params={'ownerScope': reference['ownerScope']})
                assert response.status_code == 200, response.text
                inspection = response.json()
                assert inspection['outcome'] == 'unknown', inspection
            if revise_accepted:
                review = DeliveryReview(client, conversation, pending['turn_id'])
                changed = review.revise(before)
                (tmp_path / 'revised-requirement.json').write_text(json.dumps(changed, indent=2))
                if browser_observer:
                    browser_observer('requirement_revised', client, conversation, pending['turn_id'], before)
            response = client.post(path + '/attempts', json={'commandId': uuid.uuid4().hex,
                'operation': 'continue', 'expectedProjectionRevision': interrupted['projectionRevision'],
                'config': config})
            assert response.status_code == 200, response.text
            final = wait_turn(client, path, {'completed', 'failed', 'interrupted'})
            (tmp_path / 'final-turn.json').write_text(json.dumps(final, indent=2))
            if hide_file_evidence or unreconciled_batch:
                assert final['status'] == 'failed', final
                assert len(bodies) == 1, 'Missing publication proof permitted paid replay'
                assert (project / ledger_path).read_text() == ledger_content
                assert final['projection']['usage']['total_tokens'] == 5
                assert final['projection']['usageComplete'] is False
                assert len(final['projection']['apiRounds']) == 1
                fee = final['projection']['apiRounds'][0]
                assert (fee['taskId'], fee['nativeCredits']['total']) == (pending['task_id'], 42)
                assert final['settlement']['error']['retryable'] is False
                current = (project / ledger_path).stat()
                assert (current.st_ino, current.st_mtime_ns) == (original.st_ino, original.st_mtime_ns)
                (tmp_path / 'unknown-evidence.json').write_text(json.dumps({
                    'provider_requests': 1, 'paid_tokens': 5, 'file_preserved': True,
                    'missing_publication_proof': hide_file_evidence, 'inspection': inspection,
                    'unreconciled_batch': unreconciled_batch,
                    'final_turn': final}, indent=2))
                return
            assert final['status'] == 'completed', final
            assert final['projection']['content'] == ('Report region: east. Original ledger preserved and verified.'
                if revise_accepted else 'File publication verified after service restart.')
            expected_requests = 3 if revise_accepted or ordinary_tool else 2
            assert final['projection']['usage']['total_tokens'] == 5 * expected_requests
            assert len(bodies) == expected_requests
            if ordinary_tool:
                tool_rows = final['projection']['toolRounds']
                assert [row['toolName'] for row in tool_rows] == ['write_file', 'read_files']
                assert 'published once' in tool_rows[1]['toolContent']
                assert final['projection']['usageComplete'] is True
            assert (project / ledger_path).read_text() == (
                'external edit after process loss\n' if mutate_file else ledger_content)
            current = (project / ledger_path).stat()
            assert (current.st_ino, current.st_mtime_ns) == (original.st_ino, original.st_mtime_ns)
            restored = json.loads((tmp_path / 'restored-checkpoint.json').read_text())
            assert restored['tools_pending'] is True
            assert restored['tool_recovery']['producer_task_id'] == pending['task_id']
            rows = final['projection']['apiRounds']
            assert len(rows) == expected_requests
            assert [row['nativeCredits']['total'] for row in rows] == [42] * expected_requests
            assert rows[0]['taskId'] == pending['task_id']
            assert all(row['taskId'] != pending['task_id'] for row in rows[1:])
            accepted = None
            if revise_accepted:
                assert (project / 'report.html').read_text() == report('east')
                review.inspect(before['artifacts']['report'], report('west'))
                accepted = review.finish(before, {'ledger': ledger_content, 'report': report('east')})
                if browser_observer:
                    browser_observer('accepted_after_recovery', client, conversation, pending['turn_id'], accepted)
            (tmp_path / 'recovery-evidence.json').write_text(json.dumps({
                'provider_requests': len(bodies), 'paid_tokens': 5 * expected_requests,
                'file_replayed': False, 'pending_checkpoint_restored': True,
                'original_task_id': pending['task_id'], 'final_turn': final,
                'changed_requirement': revise_accepted, 'final_owner_review': accepted,
                'later_file_edit_preserved': mutate_file, 'provider_bodies': bodies,
                'ordinary_tool_after_recovery': ordinary_tool,
                'boot_ids': [first_health['bootId'], second_health['bootId']],
                'scope': 'synthetic provider; active paid file batch; full production service crash'}, indent=2))
    finally:
        provider.shutdown(); provider.server_close(); thread.join(timeout=10)
        assert not thread.is_alive()


def test_later_file_edit_survives_public_resume_without_replaying_original_write(tmp_path):
    test_pending_file_survives_full_service_loss_and_public_continue(tmp_path, False, mutate_file=True)


def test_missing_publication_evidence_refuses_resume_without_paid_replay(tmp_path):
    test_pending_file_survives_full_service_loss_and_public_continue(tmp_path, False, hide_file_evidence=True)


def test_file_recovery_allows_next_live_ordinary_tool_batch(tmp_path):
    test_pending_file_survives_full_service_loss_and_public_continue(tmp_path, False, ordinary_tool=True)


def test_unreconciled_mixed_batch_refuses_public_continue_without_paid_replay(tmp_path):
    test_pending_file_survives_full_service_loss_and_public_continue(tmp_path, False, unreconciled_batch=True)
