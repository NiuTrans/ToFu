"""Real SQL/native acceptance proves exact predecessor and Stop precedence."""
from __future__ import annotations

from uuid import uuid4

import pytest

from tests.storage.test_storage_sidecar_contract import storage
from tests.tofudb.test_tofudb_differential import tofudb_binary, tofudb_daemon

pytestmark = [pytest.mark.unit, pytest.mark.serial]


@pytest.fixture(params=['sql', 'native'])
def authority_client(request, monkeypatch, storage):  # noqa: F811 - pytest requires the parametrized fixture by name
    if request.param == 'sql':
        client = storage.client
    else:
        driver = request.getfixturevalue('tofudb_daemon')
        class Client:
            def invoke(self, operation, payload, command_id=None):
                from lib.storage import StorageError
                outcome = driver.execute(operation, payload, command_id)
                if not outcome.ok:
                    raise StorageError(outcome.error_code, outcome.error_message)
                return outcome.value
            def query(self, operation, payload, **kwargs):
                return self.invoke(operation, payload)
            def command(self, operation, payload, command_id, **kwargs):
                return self.invoke(operation, payload, command_id)
        client = Client()
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: client)
    return client


def _bound(attempt_id, task_id):
    from lib.turn_lifecycle import claim_attempt_start, bind_task, mark_task_started
    assert claim_attempt_start(attempt_id, user_id=11)
    assert bind_task(attempt_id, task_id, user_id=11)['taskId'] == task_id
    assert mark_task_started(attempt_id, task_id, user_id=11)['status'] == 'running'


def _settle(attempt_id, task_id, reason):
    from lib.turn_lifecycle import record_task_event
    task = {'id': task_id, '_attemptId': attempt_id, '_userId': 11,
            'status': 'aborted', 'aborted': True, '_abort_reason': reason,
            'finishReason': 'aborted', 'config': {'model': 'fixture'},
            'content': 'Partial synthetic evidence', 'thinking': '', 'toolRounds': []}
    assert record_task_event(task, {'type': 'aborted', 'finishReason': 'aborted'})


@pytest.mark.parametrize('restart_mode', ['shutdown', 'cold'])
def test_real_attempt_acceptance_overwrites_spoofed_source_and_fences_intervening_stop(authority_client, restart_mode):
    from lib.turn_lifecycle import create_turn_pair, create_attempt, get_attempt, get_turn
    from lib.swarm.restart_recovery import continue_recovery_allowed, restart_source_attempt
    conv = 'restart-lineage-' + uuid4().hex
    pair = create_turn_pair(conv, command_id='create-' + conv,
        input_projection={'content': 'Resume descendants safely'}, config={}, user_id=11,
        conversation_defaults={'allowCreate': True, 'title': 'Synthetic recovery'})
    original = pair['attempt']['attemptId']
    turn_id = pair['turn']['turnId']
    _bound(original, 'original-task')
    if restart_mode == 'shutdown':
        _settle(original, 'original-task', 'server_shutdown')
    else:
        from lib.turn_lifecycle import record_task_event
        live = {'id': 'original-task', '_attemptId': original, '_userId': 11,
                'status': 'running', 'config': {'model': 'fixture'}, 'content': '',
                'thinking': 'Partial checkpoint thinking', 'toolRounds': []}
        assert record_task_event(live, {'type': 'thinking', 'content': 'Partial checkpoint thinking'})
        result = authority_client.command('turn.recover', {'user_id': 11}, 'cold-recover-' + conv)
        assert result['recovered'] >= 1
    source = get_attempt(original, user_id=11)
    assert source['status'] == 'interrupted' and 'settlement' not in source
    row = {'swarm_key': conv, 'conv_id': conv, 'task_id': 'original-task', 'status': 'running',
           'config': {'user_id': 11, 'parent_cfg': {
               '_swarmParentTurnId': turn_id, '_swarmParentAttemptId': original,
               '_swarmParentTaskId': 'original-task'}}}
    assert restart_source_attempt(row, user_id=11)['attemptId'] == original
    def accept(command):
        current = get_turn(conv, turn_id, user_id=11)
        return create_attempt(conv, turn_id, command_id=command, operation='continue',
            expected_projection_revision=current['projectionRevision'], user_id=11,
            config={'_resumeSource': {'attemptId': 'spoofed', 'cause': 'server_restart'}})
    resumed = accept('first-continue-' + conv)
    resume_id = resumed['attempt']['attemptId']
    _bound(resume_id, 'resume-task')
    accepted = get_attempt(resume_id, user_id=11, include_execution_config=True)
    assert accepted['config']['_resumeSource'] == {
        'attemptId': original, 'taskId': 'original-task', 'status': 'interrupted', 'cause': 'server_restart'}
    assert get_turn(conv, turn_id, user_id=11)['settlement'] == {}
    task = {'id': 'resume-task', 'convId': conv, '_userId': 11,
            'config': {'_turnId': turn_id, '_attemptId': resume_id, '_turnOperation': 'continue'}}
    assert continue_recovery_allowed(row, task, user_id=11)
    task['id'] = 'wrong-task'
    assert not continue_recovery_allowed(row, task, user_id=11)
    task['id'] = 'resume-task'
    _settle(resume_id, 'resume-task', 'user_abort')
    second = accept('second-continue-' + conv)
    second_id = second['attempt']['attemptId']
    _bound(second_id, 'second-task')
    task.update(id='second-task')
    task['config']['_attemptId'] = second_id
    assert get_attempt(second_id, user_id=11, include_execution_config=True)['config']['_resumeSource'] == {
        'attemptId': resume_id, 'taskId': 'resume-task', 'status': 'interrupted', 'cause': 'user_abort'}
    assert not continue_recovery_allowed(row, task, user_id=11)
    row['config']['parent_cfg'].update(_swarmParentAttemptId=resume_id, _swarmParentTaskId='resume-task')
    assert not continue_recovery_allowed(row, task, user_id=11)
