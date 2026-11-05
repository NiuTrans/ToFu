"""Session handoff uses real Turn transactions on SQL and native authorities."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from tests.turn_lifecycle.test_task_event_delivery import storage as storage
from lib.conversation_sync.command_service import ConversationTurnCommandService
from lib.conversation_sync.execution_identity import capture_execution_identity
from lib.conversations.coordinator_tools import execute_coordinator_tool
from lib.identity import PrincipalContext

pytestmark = pytest.mark.unit


@pytest.fixture(params=['continue', 'checkpoint_resume'])
def session_runtime(storage, monkeypatch, tmp_path, request):
    from lib import turn_lifecycle as turns
    from lib.conversations import project_coordination
    from lib.conversation_sync import runtime

    project = str(tmp_path)
    principal = PrincipalContext.user(subject_id='owner', owner_user_id=1)
    config = capture_execution_identity({'model': 'target-model', 'projectPath': project,
                                          'thinkingEnabled': True}, 1, principal)

    def running(conversation, task_id):
        pair = turns.create_turn_pair(conversation, command_id=task_id + ':input',
            input_projection={'content': 'Continue managing the selected Session'},
            config=deepcopy(config), user_id=1, conversation_defaults={
                'allowCreate': True, 'title': conversation, 'settings': {'projectPath': project}})
        turns.bind_task(pair['attempt']['attemptId'], task_id, user_id=1)
        task = {'id': task_id, 'convId': conversation, '_turnId': pair['turn']['turnId'],
                '_attemptId': pair['attempt']['attemptId'], '_userId': 1, '_attended': True,
                '_principalContext': principal.to_payload(), 'config': deepcopy(config),
                'status': 'running', 'model': 'target-model', 'content': 'Preserved partial answer',
                'thinking': '', 'toolRounds': []}
        turns.record_task_event(task, {'type': 'delta', 'content': task['content']})
        return pair, task

    source, task = running('coordinator', 'coordinator-task')
    target, target_task = running('session', 'target-task')
    if request.param == 'continue':
        target_task.update(content='', thinking='Preserved reasoning')
    target_task.update(status='done', finishReason='length')
    turns.record_task_event(target_task, {'type': 'done', 'finishReason': 'length'})
    monkeypatch.setattr(project_coordination, 'ProjectCoordinationRepository',
        lambda owner, **kwargs: SimpleNamespace(read=lambda path: {
            'availability': 'available', 'conversationId': 'coordinator', 'revision': 1,
            'projectPath': project}))
    started = []

    def start(conv, cfg, request, abort_after, registered, **kwargs):
        task_id = f'resumed-{len(started)}'
        registered(task_id)
        started.append((conv, deepcopy(cfg)))
        return task_id, None

    commands = ConversationTurnCommandService(build_user_message=lambda *a: {},
        was_aborted_after=lambda *a: False, start_task=start)
    monkeypatch.setattr(runtime, 'conversation_turn_commands', commands)
    target_turn = turns.get_turn('session', target['turn']['turnId'], user_id=1)
    args = {'conversation_id': 'session', 'turn_id': target_turn['turnId'],
            'attempt_id': target_turn['currentAttemptId'], 'action': request.param,
            'expected_revision': target_turn['projectionRevision'], 'idempotency_key': 'resume'}
    args['resume_anchor'] = next(option['anchor'] for option in target_turn['settlement']['resumeOptions']
                                 if option['operation'] == request.param)
    return SimpleNamespace(turns=turns, task=task, target=target, source=source,
                           args=args, started=started, commands=commands,
                           call=lambda name, args: json.loads(execute_coordinator_tool(task, name, args)))


def test_real_continue_replay_and_stale_stop_preserve_conversation(session_runtime, monkeypatch):
    s = session_runtime
    status = s.call('get_conversation_status', {'conversation_id': 'session'})
    assert any(t['turnId'] == s.args['turn_id'] for t in status['turns']), status
    before = s.turns.get_turn('session', s.args['turn_id'], user_id=1)
    assert s.args['action'] in {o['operation'] for o in before['settlement']['resumeOptions']}
    from lib.storage.errors import StorageError
    create = s.commands.create_attempt
    lost = False

    def lose_first_ack(*args, **kwargs):
        nonlocal lost
        result = create(*args, **kwargs)
        if not lost:
            lost = True
            raise StorageError('database_unavailable', 'Accepted command ACK was lost')
        return result

    monkeypatch.setattr(s.commands, 'create_attempt', lose_first_ack)
    uncertain = s.call('control_conversation', s.args)
    assert uncertain['execution_started'] is None
    assert len(s.started) == 1
    first = s.call('control_conversation', s.args)
    assert 'error' not in first, first
    second = s.call('control_conversation', s.args)
    assert 'error' not in second, second
    assert first['attempt']['attemptId'] == second['attempt']['attemptId']
    assert len(s.started) == 1
    assert s.started[0][0] == 'session'
    assert s.started[0][1]['model'] == 'target-model'
    assert s.started[0][1]['thinkingEnabled'] is True
    after = s.turns.get_turn('session', s.args['turn_id'], user_id=1)
    assert after['parentTurnId'] == before['parentTurnId']
    if s.args['action'] == 'continue':
        assert after['projection'].get('content', '') == before['projection']['content']
    else:
        assert after['projection'].get('content', '') == ''
        assert before['projection']['content'] in json.dumps(after['projection']['rolledBack'])
    stale = s.call('control_conversation', {**s.args, 'action': 'stop'})
    assert 'error' in stale
    assert s.turns.get_attempt(first['attempt']['attemptId'], user_id=1)['status'] in {'pending', 'running'}
    stopped = s.call('control_conversation', {**s.args, 'action': 'stop',
        'attempt_id': first['attempt']['attemptId'], 'idempotency_key': 'stop'})
    assert 'error' not in stopped, stopped
    assert s.turns.get_attempt(first['attempt']['attemptId'], user_id=1)['status'] == 'interrupted'


def test_synthetic_source_and_stale_revision_cannot_launch(session_runtime):
    s = session_runtime
    stale = s.call('control_conversation', {**s.args, 'expected_revision': 1})
    assert 'error' in stale, stale
    assert not s.started
    s.task['_principalContext'] = PrincipalContext.system(subject_id='notice', owner_user_id=1,
        scopes={'chat'}).to_payload()
    refused = s.call('control_conversation', s.args)
    assert 'error' in refused, refused
    assert not s.started
