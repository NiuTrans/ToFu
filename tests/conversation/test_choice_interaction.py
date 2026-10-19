"""Native choices reuse ask_human's durable answer_guidance attempt authority."""
import uuid

import pytest

pytestmark = pytest.mark.unit
pytest_plugins = ('tests._chat_sidecar',)


def test_declared_choices_use_server_round_not_request_options():
    from lib.human_gate_contract import require_declared_choice_response
    question = {'guidanceType': 'choice', 'guidanceOptions': [
        {'label': 'Client'}, {'label': 'Server', 'description': 'Owned option'}]}
    assert require_declared_choice_response(question, 'Server') == 'Server'
    assert require_declared_choice_response(question, 'Client\nuser_note: why')
    with pytest.raises(ValueError, match='declared choice'):
        require_declared_choice_response(question, 'Model invented another option')
    assert require_declared_choice_response({'guidanceType': 'free_text'}, 'anything')


def test_durable_choice_owner_denial_ack_loss_replay_and_dedup(chat_sidecar, monkeypatch):
    import lib.conversation_sync.command_service as commands
    import lib.conversation_sync.workspace as workspace
    from lib.turn_lifecycle import (
        LifecycleNotFound, abort_attempt, bind_task, create_turn_pair,
        get_attempt, get_turn, record_task_event,
    )
    conv_id = 'choice-' + uuid.uuid4().hex[:12]
    created = create_turn_pair(
        conv_id, command_id='initial-' + conv_id,
        input_projection={'content': 'Ask a choice'}, config={}, user_id=1,
        conversation_defaults={'allowCreate': True, 'title': 'Choice', 'settings': {}},
    )
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    bind_task(attempt_id, 'task-' + conv_id, user_id=1)
    task = {'id': 'task-' + conv_id, '_attemptId': attempt_id, '_userId': 1,
            'status': 'running', 'content': '', 'thinking': '', 'config': {},
            'toolRounds': [{
                'roundNum': 1, 'llmRound': 0, 'toolCallId': 'call-ask',
                'toolName': 'ask_human', 'toolArgs': {'question': 'Choose scope'},
                'toolContent': None, 'status': 'awaiting_human',
                'guidanceId': 'hg_server', 'guidanceQuestion': 'Choose scope',
                'guidanceType': 'choice',
                'guidanceOptions': [{'label': 'Client'}, {'label': 'Server'}],
            }]}
    assert record_task_event(task, {'type': 'tool_result'})
    abort_attempt(attempt_id, user_id=1)
    settled = get_turn(conv_id, turn_id, user_id=1)
    monkeypatch.setattr(workspace, 'resolve_workspace_config', lambda config, *_: config)
    service = commands.ConversationTurnCommandService(
        build_user_message=lambda *_: {}, was_aborted_after=lambda *_: False,
        start_task=lambda *_: pytest.fail('test must not dispatch a model'),
    )
    starts = []
    monkeypatch.setattr(service, '_start_accepted_attempt', lambda result, *_args, **_kwargs: starts.append(result))
    body = {'commandId': 'answer-' + conv_id, 'operation': 'answer_guidance',
            'expectedProjectionRevision': settled['projectionRevision'],
            'humanResponse': 'Server', 'config': {}}
    with pytest.raises(LifecycleNotFound):
        service.create_attempt(conv_id, turn_id, 2, body, request_started_at=1.0)
    with pytest.raises(ValueError, match='declared choice'):
        service.create_attempt(conv_id, turn_id, 1,
                               {**body, 'humanResponse': 'Undeclared'}, request_started_at=1.0)
    accepted = service.create_attempt(conv_id, turn_id, 1, body, request_started_at=1.0).value
    assert accepted['idempotentReplay'] is False
    durable = get_attempt(accepted['attempt']['attemptId'], user_id=1, include_execution_config=True)
    assert durable['config']['_humanGuidanceAnswer'] == {
        'guidanceId': 'hg_server', 'toolCallId': 'call-ask', 'response': 'Server'}
    # Commit/ACK loss: current settlement no longer offers the operation, but
    # exact same owner/command/answer must recover its accepted attempt.
    assert not get_turn(conv_id, turn_id, user_id=1).get('settlement')
    replay = service.create_attempt(conv_id, turn_id, 1, body, request_started_at=2.0).value
    assert replay['idempotentReplay'] is True
    assert replay['attempt']['attemptId'] == accepted['attempt']['attemptId']
    assert replay['attempt']['commandId'] == body['commandId']
    with pytest.raises(ValueError, match='different response'):
        service.create_attempt(conv_id, turn_id, 1,
                               {**body, 'humanResponse': 'Client'}, request_started_at=3.0)
    # Public replies never leak accepted execution config.
    assert 'config' not in replay['attempt']


def test_live_choice_survives_waiter_loss_reload_duplicate_conflict_and_stop(chat_sidecar, monkeypatch):
    import lib.conversation_sync.command_service as commands
    import lib.tasks_pkg.human_guidance as guidance
    from lib.storage import StorageError
    from lib.turn_lifecycle import (
        LifecycleConflict, LifecycleNotFound, abort_attempt, bind_task,
        create_turn_pair, get_turn, record_task_event,
    )
    conv = 'live-choice-' + uuid.uuid4().hex[:12]
    created = create_turn_pair(
        conv, command_id='initial-' + conv,
        input_projection={'content': 'Ask'}, config={}, user_id=1,
        conversation_defaults={'allowCreate': True, 'title': 'Live', 'settings': {}},
    )
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    bind_task(attempt_id, 'task-' + conv, user_id=1)
    task = {'id': 'task-' + conv, '_attemptId': attempt_id, '_userId': 1,
            '_turnId': turn_id, 'convId': conv, 'status': 'running',
            'content': '', 'thinking': '', 'config': {}, 'toolRounds': [{
                'roundNum': 1, 'llmRound': 0, 'toolCallId': 'call-ask',
                'toolName': 'ask_human', 'toolArgs': {'question': 'Choose'},
                'toolContent': None, 'status': 'awaiting_human',
                'guidanceId': 'hg_live', 'guidanceQuestion': 'Choose',
                'guidanceType': 'choice', 'guidanceOptions': ['Client', 'Server'],
            }]}
    assert record_task_event(task, {'type': 'tool_result'})
    before = get_turn(conv, turn_id, user_id=1)
    assert before['status'] == 'running'
    assert before['projection']['toolRounds'][0]['status'] == 'awaiting_human'
    service = commands.ConversationTurnCommandService(
        build_user_message=lambda *_: {}, was_aborted_after=lambda *_: False,
        start_task=lambda *_: pytest.fail('live answer must not dispatch'),
    )
    body = {'commandId': 'answer-live', 'operation': 'answer_guidance',
            'guidanceId': 'hg_live', 'humanResponse': 'Server', 'config': {},
            'expectedProjectionRevision': before['projectionRevision']}
    def submit(owner=1, **changes):
        return service.create_attempt(conv, turn_id, owner, {**body, **changes},
                                      request_started_at=1.0).value
    with pytest.raises(LifecycleNotFound):
        submit(2)
    with pytest.raises(LifecycleNotFound):
        submit(guidanceId='foreign-or-absent')
    with pytest.raises(StorageError, match='declared choice'):
        submit(humanResponse='invented')
    # Exercise the fixed HTTP route with the generated scoped request contract.
    import asyncio
    from quart import Quart, g, request
    from lib.api_keys import AuthContext
    from lib.identity import PrincipalContext
    import routes.conversation_sync_v3 as route
    app = Quart(__name__)
    app.config['TESTING'] = True
    @app.before_request
    def auth():
        owner = int(request.headers['X-Test-Owner'])
        g.auth_ctx = AuthContext(key_id='choice-test', name='choice-test',
                                scopes=frozenset({'chat'}), owner_user_id=owner)
        g.principal_context = PrincipalContext.user(subject_id='choice-test',
                                                     owner_user_id=owner, scopes={'chat'})
    app.register_blueprint(route.conversation_sync_v3_bp)
    monkeypatch.setattr(route, 'conversation_turn_commands', service)
    async def post(owner=1, **changes):
        return await app.test_client().post(
            f'/api/v4/conversations/{conv}/turns/{turn_id}/attempts',
            json={**body, **changes}, headers={'X-Test-Owner': str(owner)})
    assert asyncio.run(post(2)).status_code == 404
    assert asyncio.run(post(humanResponse='Invented')).status_code == 400
    # Simulate disappearance of the process-local waiter. Commit still succeeds.
    monkeypatch.setattr(guidance, 'resolve_human_guidance', lambda *_a, **_kw: False)
    accepted = submit()
    assert accepted['attempt']['attemptId'] == attempt_id
    assert accepted['turn']['status'] == 'running'
    persisted = get_turn(conv, turn_id, user_id=1)['projection']['toolRounds'][0]
    assert persisted['guidanceResponse'] == 'Server'
    assert persisted['guidanceAnswerCommandId'] == 'answer-live'
    assert persisted['status'] == 'completed'
    # Drop the ACK, reload through a new command service, retry exactly.
    assert submit()['idempotentReplay'] is True
    assert submit(commandId='same-answer-new-command')['idempotentReplay'] is True

    assert asyncio.run(post()).status_code == 200
    assert asyncio.run(post(commandId='http-conflict', humanResponse='Client')).status_code == 409
    assert guidance.request_human_guidance('hg_live', task=task) == 'Server'
    with pytest.raises((LifecycleConflict, StorageError), match='different|conflict'):
        submit(commandId='conflict', humanResponse='Client')
    assert guidance._durable_guidance_response(task, 'hg_live', 1) == 'Server'
    # A stale in-process task snapshot must never erase the accepted round.
    task['content'] = 'Next narration'
    assert record_task_event(task, {'type': 'tool_result'})
    assert get_turn(conv, turn_id, user_id=1)['projection']['toolRounds'][0]['guidanceResponse'] == 'Server'
    abort_attempt(attempt_id, user_id=1)
    stopped = get_turn(conv, turn_id, user_id=1)
    assert stopped['projection']['toolRounds'][0]['guidanceResponse'] == 'Server'
    assert submit()['idempotentReplay'] is True
    # Replay checkpoint contains a completed ask_human result, not an unanswered gate.
    from lib.tool_results.tool_round_replay import scan_replayable_tool_round_prefix
    rounds = stopped['projection']['toolRounds']
    assert scan_replayable_tool_round_prefix(rounds).rounds[0]['toolContent'] == 'Human response: Server'


def test_legacy_live_gate_commits_the_owned_question_before_resolving(chat_sidecar):
    from lib.tasks_pkg.human_gate_registry import GATE_GUIDANCE, human_gate_registry
    from lib.tasks_pkg.human_guidance import resolve_human_guidance
    from lib.turn_lifecycle import create_turn_pair, bind_task, record_task_event, get_turn, LifecycleConflict
    from lib.storage import StorageError
    conv = 'legacy-live-' + uuid.uuid4().hex[:12]
    created = create_turn_pair(conv, command_id='initial-' + conv,
        input_projection={'content': 'Ask'}, config={}, user_id=1,
        conversation_defaults={'allowCreate': True, 'title': 'Live', 'settings': {}})
    turn_id = created['turn']['turnId']
    attempt_id = created['attempt']['attemptId']
    bind_task(attempt_id, 'task-' + conv, user_id=1)
    guidance_id = 'hg_legacy_' + uuid.uuid4().hex[:12]
    task = {'id': 'task-' + conv, '_attemptId': attempt_id, '_userId': 1,
            '_turnId': turn_id, 'convId': conv, 'status': 'running', 'content': '',
            'thinking': '', 'config': {}, 'toolRounds': [{
                'roundNum': 1, 'llmRound': 0, 'toolCallId': 'call-legacy', 'toolName': 'ask_human',
                'toolArgs': {'question': 'Choose'}, 'status': 'awaiting_human',
                'guidanceId': guidance_id, 'guidanceType': 'choice',
                'guidanceQuestion': 'Choose', 'guidanceOptions': ['Client', 'Server'],
            }]}
    assert record_task_event(task, {'type': 'tool_result'})
    entry = human_gate_registry.register(GATE_GUIDANCE, guidance_id, owner_user_id=1,
                                        durable_scope=(conv, turn_id))
    try:
        assert not resolve_human_guidance(guidance_id, 'Server', owner_user_id=2)
        with pytest.raises(StorageError, match='declared choice'):
            resolve_human_guidance(guidance_id, 'Invented', owner_user_id=1)
        assert not entry.resolved
        assert resolve_human_guidance(guidance_id, 'Server', owner_user_id=1)
        receipt = get_turn(conv, turn_id, user_id=1)['projection']['toolRounds'][0]
        assert receipt['guidanceResponse'] == 'Server'
        assert receipt['toolContent'] == 'Human response: Server'
        assert entry.resolved and entry.response == 'Server'
        with pytest.raises((LifecycleConflict, StorageError), match='different'):
            resolve_human_guidance(guidance_id, 'Client', owner_user_id=1)
    finally:
        human_gate_registry.take(GATE_GUIDANCE, guidance_id, entry)
