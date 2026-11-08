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


HUMAN_UI = {'kind': 'human-ui', 'ownerUserId': 1, 'subject': 'session-key', 'credential': 'session',
            'browser': 'fetch-metadata', 'userAgent': 'Mozilla/5.0', 'remoteAddr': '127.0.0.1'}


@pytest.fixture
def approvals(tmp_path, monkeypatch):
    from lib import lifecycle_approval as gate
    monkeypatch.setattr(gate, '_APPROVALS_FILE', str(tmp_path / 'lifecycle_approvals.json'))
    monkeypatch.setattr(gate, '_STATE_FILE', str(tmp_path / 'lifecycle_state.json'))
    return gate


def test_global_router_continues_and_stops_an_exact_attempt_in_another_project_after_a_restart(
        session_runtime, approvals, monkeypatch, tmp_path):
    """docs/ROUTER.md R9: under the owner's delegation, a Global Router request without
    an owner message of its own (a standing instruction) presses the owner's Continue
    and Stop on the exact Attempt a restart interrupted, in any project: the same Turn
    resumes with its accepted config; nothing regenerates and no assignment is created."""
    s, gate = session_runtime, approvals
    other = tmp_path / 'another-project'
    other.mkdir()
    principal = PrincipalContext.user(subject_id='owner', owner_user_id=1)
    config = capture_execution_identity({'model': 'other-model', 'projectPath': str(other)}, 1, principal)
    pair = s.turns.create_turn_pair('elsewhere', command_id='elsewhere:input',
        input_projection={'content': 'Run the long migration'}, config=deepcopy(config), user_id=1,
        conversation_defaults={'allowCreate': True, 'title': 'elsewhere',
                               'settings': {'projectPath': str(other)}})
    s.turns.bind_task(pair['attempt']['attemptId'], 'elsewhere-task', user_id=1)
    s.turns.record_task_event({
        'id': 'elsewhere-task', 'convId': 'elsewhere', '_turnId': pair['turn']['turnId'],
        '_attemptId': pair['attempt']['attemptId'], '_userId': 1, '_attended': True,
        '_principalContext': principal.to_payload(), 'config': deepcopy(config), 'status': 'running',
        'model': 'other-model', 'content': '', 'thinking': 'Half way through', 'toolRounds': []},
        {'type': 'thinking', 'content': 'Half way through'})
    # The restart: boot recovery settles every pre-boot attempt but the live router's own.
    s.turns.recover_running_attempts(exclude_task_ids=['coordinator-task'])
    interrupted = s.turns.get_turn('elsewhere', pair['turn']['turnId'], user_id=1)
    assert interrupted['status'] == 'interrupted', interrupted
    option = interrupted['settlement']['resumeOptions'][0]
    assert option['operation'] in {'continue', 'checkpoint_resume'}
    # The Global Router (any project) handling a nonhuman notice: no owner message of its own.
    monkeypatch.setattr('lib.router.policy.is_router_scope', lambda scope: True)
    s.task['_principalContext'] = PrincipalContext.system(
        subject_id='notice', owner_user_id=1, scopes={'chat'}).to_payload()
    trigger = {'kind': 'standing_instruction', 'turnId': 'owner-turn', 'instructionId': 'ri-1',
               'eventId': 'restart:1', 'text': 'After a restart, continue my interrupted work.'}
    monkeypatch.setattr('lib.router.owner_delegation.resolve_trigger', lambda task, scope, args: dict(trigger))
    authority = {'reason': 'The restart interrupted this migration', 'instruction_id': 'ri-1',
                 'event_id': 'restart:1'}
    args = {'conversation_id': 'elsewhere', 'turn_id': interrupted['turnId'],
            'attempt_id': interrupted['currentAttemptId'], 'action': option['operation'],
            'expected_revision': interrupted['projectionRevision'], 'idempotency_key': 'after-restart',
            'owner_delegation': authority, **({'resume_anchor': option['anchor']} if 'anchor' in option else {})}
    without_authority = s.call('control_conversation', {k: v for k, v in args.items() if k != 'owner_delegation'})
    assert 'error' in without_authority and not s.started
    refused = s.call('control_conversation', args)
    assert refused['code'] == 'owner_delegation_not_granted' and refused['execution_started'] is False
    assert not s.started and gate.delegated_actions(1) == []
    gate.set_delegation(1, True, provenance=HUMAN_UI, grantee={
        'kind': 'global_router', 'conversationId': 'coordinator', 'bindingRevision': 1, 'tenant': None})
    regenerate = s.call('control_conversation', {**{k: v for k, v in args.items() if k != 'resume_anchor'},
                                                 'action': 'regenerate', 'idempotency_key': 'regenerate'})
    assert 'error' in regenerate and not s.started
    resumed = s.call('control_conversation', args)
    assert 'error' not in resumed, resumed
    assert resumed['turn']['turnId'] == interrupted['turnId']
    assert resumed['attempt']['attemptId'] != interrupted['currentAttemptId']
    assert [(conversation, cfg['model']) for conversation, cfg in s.started] == [('elsewhere', 'other-model')]
    # The resumed Attempt runs as the interrupted execution's own accepted identity, never as the delegation.
    from lib.conversation_sync.execution_identity import EXECUTION_IDENTITY_CONFIG_KEY
    accepted = s.turns.get_attempt(resumed['attempt']['attemptId'], user_id=1, include_execution_config=True)
    assert accepted['config'][EXECUTION_IDENTITY_CONFIG_KEY]['principal']['subject_id'] == 'owner'
    replay = s.call('control_conversation', args)
    assert replay['attempt']['attemptId'] == resumed['attempt']['attemptId'] and len(s.started) == 1
    [action] = gate.delegated_actions(1)
    assert action['id'] == resumed['owner_delegation_action_id'] and action['status'] == 'completed'
    assert action['scope'] == 'conversation_control' and action['trigger'] == trigger
    assert action['target'] == {'kind': 'conversation', 'conversationId': 'elsewhere',
                                'turnId': interrupted['turnId'],
                                'attemptId': interrupted['currentAttemptId'], 'action': option['operation']}
    stop = {'conversation_id': 'elsewhere', 'turn_id': interrupted['turnId'],
            'attempt_id': resumed['attempt']['attemptId'], 'action': 'stop', 'idempotency_key': 'stop',
            'owner_delegation': authority}
    stopped = s.call('control_conversation', stop)
    assert 'error' not in stopped, stopped
    assert s.turns.get_attempt(resumed['attempt']['attemptId'], user_id=1)['status'] == 'interrupted'
    assert sorted(item['target']['action'] for item in gate.delegated_actions(1)) == sorted(
        ['stop', option['operation']])
