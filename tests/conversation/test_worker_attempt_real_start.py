"""Project Worker attempts start through the real registry, executor and admission.

Every earlier Worker start spec replaced ``start_conversation_attempt_executor``
or the command service's ``start_task``, so the 2026-11-06 23:46 outage
("prepared task identity differs from conversation worker") shipped unnoticed.
Here delegation, boot recovery, Coordinator session control and the browser
Continue command, durable admission, the worker claim, task registration,
dispatch account binding and the isolated Git checkout are all production code
on a real storage authority. Only the model context and the root run loop's
provider outcome are controlled; no provider is called.
"""
import json
from pathlib import Path
import subprocess
import threading
import time
import uuid

import pytest

from lib.turn_lifecycle import build_api_messages as production_build_api_messages
from tests.support.conversation_workers import ConversationWorkers

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = [pytest.mark.api,
              pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)]

TENANT = 'real-path-tenant'


def _git(root, *args):
    result = subprocess.run(['git', '-c', 'core.fsmonitor=false', '-C', str(root), *args],
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _run_controlled(task, plans):
    """The provider outcome of one physical attempt, through its real account."""
    from lib.tasks_pkg.manager import (
        append_event, finalize_chat_task_aborted, finalize_chat_task_error,
        persist_task_result, stamp_chat_task_terminal,
    )
    from lib.agent_core.execution_budget import model_budget_admission
    from lib.agent_core.execution_limits import admit_tool_execution
    plan = plans[task['_attemptId']]
    plan['task'] = {key: task.get(key) for key in ('id', 'config', '_principalContext', '_workspaceWorkId')}
    for name, text in (plan.get('write') or {}).items():
        # The Worker's own edits, in the checkout it was bound to.
        (Path(task['config']['projectPath']) / name).write_text(text)
    if plan.get('observe'):
        plan['observed'] = plan['observe'](task)
    lease = model_budget_admission(task)(input_tokens=1, max_output_tokens=10)
    lease.mark_dispatched()
    lease.settle({'prompt_tokens': 1, 'completion_tokens': 9})
    plan['model_ordinal'] = lease.ordinal
    for number in range(plan.get('tools', 0)):
        call_id = f"{task['_attemptId']}-tool-{number}"
        assert admit_tool_execution(task) is None
        event = task['_executionBudget'].begin_tool_attempt(task, call_id, 'read_files', 1)
        task['_executionBudget'].finish_tool_attempt(event, phase='executor_returned')
        # The completed round the agent loop records; it is what a later
        # Continue replays, so the settled Turn offers Continue.
        task.setdefault('toolRounds', []).append({
            'roundNum': len(task['toolRounds']) + 1, 'llmRound': 1, 'toolCallId': call_id,
            'toolName': 'read_files', 'toolArgs': '{"path": "base.txt"}', 'toolContent': 'base\n',
            'status': 'done', 'attemptId': task['_attemptId'], 'taskId': task['id']})
    outcome = plan['outcome']
    if outcome == 'restart':
        # What server shutdown stamps on a running task before it stops.
        task['_abort_reason'] = 'server_shutdown'
        finalize_chat_task_aborted(task)
    elif outcome == 'fence':
        # What a storage fence stamps when the authority refuses the attempt's writes.
        task['_abort_reason'] = 'storage_frame_overflow'
        finalize_chat_task_aborted(task)
    elif outcome == 'provider_error':
        finalize_chat_task_error(task, {'kind': 'provider_error', 'message': 'HTTP 400 from the provider'})
    else:
        task['content'] = 'Worker result for ' + task['_attemptId']
        append_event(task, {'type': 'delta', 'content': task['content']})
        stamp_chat_task_terminal(task, status='done', finish_reason='stop')
        append_event(task, {'type': 'done', 'finishReason': 'stop'})
        assert persist_task_result(task)


@pytest.fixture
def platform(semantic_authority, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from lib import storage, turn_lifecycle as turns, integration_state_repository as state
    from lib import integration_control as ic
    from lib.conversation_sync import task_start
    from lib.conversations.coordinator_tools import execute_coordinator_tool
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.identity import PrincipalContext
    from lib.project_dispatch.delegation import capture_worker_policy
    from lib.project_dispatch.repository import ProjectDispatchRepository
    from lib.project_dispatch.request_budget import bind_source_account
    from lib.project_mod import delivery, modifications
    from lib.tasks_pkg.coordinator_policy import coordinator_scope

    factory, owner, _engine = semantic_authority
    for module, name in ((storage, 'get_storage_client'), (state, 'get_storage_client'),
                         (turns, '_turn_client')):
        monkeypatch.setattr(module, name, lambda **kwargs: factory(**kwargs))

    def forbidden_provider(*args, **kwargs):
        pytest.fail('Real-path start specs never call a provider')

    monkeypatch.setattr('lib.llm_dispatch.dispatch_chat', forbidden_provider)
    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', forbidden_provider)
    root = (tmp_path / 'project').resolve()
    root.mkdir()
    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.name', 'Test')
    _git(root, 'config', 'user.email', 'test@example.invalid')
    (root / 'base.txt').write_text('base\n')
    _git(root, 'add', 'base.txt')
    _git(root, 'commit', '-m', 'base')
    monkeypatch.setenv('TOFU_INTEGRATION_WORKSPACE_DIR', str(tmp_path / 'workspaces'))
    monkeypatch.delenv('TOFU_WORKSPACE_MODE', raising=False)
    monkeypatch.setattr(modifications, 'SESSIONS_DIR', str(tmp_path / 'sessions'))
    monkeypatch.setattr(modifications, '_live_change_listeners', [])
    monkeypatch.setattr(ic, '_start_or_wake_worker', lambda: None)
    monkeypatch.setattr(ic, '_push', lambda *a, **kw: None)
    monkeypatch.setattr(ic, '_gate_commands', lambda *a, **kw: (True, 'fixture verification'))
    monkeypatch.setattr(delivery, '_canonical_in_use', lambda *a: False)
    monkeypatch.setattr(task_start, '_throttled_cleanup_old_tasks', lambda: None)
    monkeypatch.setattr(turns, 'build_api_messages',
                        lambda *a, **kw: [{'role': 'user', 'content': 'Assigned objective'}])
    plans = {}
    monkeypatch.setattr('lib.tasks_pkg.orchestrator.api.run_task',
                        lambda task: _run_controlled(task, plans))

    binding = ProjectCoordinationRepository(owner).create(
        str(root), folder_id='real-path-folder', expected_revision=0, command_id='real-path-coordinator')
    conversation = binding['conversationId']
    pair = turns.create_turn_pair(conversation, user_id=owner, command_id='real-path-request',
        input_projection={'content': 'Implement the part and verify it'}, config={})
    request_task = 'real-path-coordinator-' + uuid.uuid4().hex
    turns.bind_task(pair['attempt']['attemptId'], request_task, user_id=owner)
    turns.mark_task_started(pair['attempt']['attemptId'], request_task, user_id=owner)
    principal = PrincipalContext.user(subject_id=f'user:{owner}', owner_user_id=owner,
                                      tenant_id=TENANT, scopes=['chat'])
    coordinator = {'id': request_task, 'convId': conversation, '_userId': owner, '_attended': True,
        '_turnId': pair['attempt']['turnId'], '_attemptId': pair['attempt']['attemptId'],
        '_principalContext': principal.to_payload(), 'status': 'running', 'usage': {},
        'abort_event': threading.Event(),
        'config': {'projectPath': str(root), 'codeExecEnabled': True, 'autoApply': True,
                   'tools': {'programmaticCalling': 'off'},
                   '_coordinatorSubmittedToolSettings': {'programmaticCalling': 'on'}}}
    capture_worker_policy(coordinator, coordinator['config'],
        [{'function': {'name': name}} for name in ('read_files', 'write_file', 'run_command')],
        'fixture-model', worker_model='fixture-model')
    repository = ProjectDispatchRepository(owner, str(root))
    bind_source_account(coordinator, repository=repository)
    workers = ConversationWorkers(owner)
    platform = SimpleNamespace(owner=owner, root=root, coordinator=coordinator, repository=repository,
        scope=coordinator_scope(coordinator), principal=principal, plans=plans, workers=workers,
        tool=lambda name, arguments: json.loads(execute_coordinator_tool(coordinator, name, arguments)))
    try:
        yield platform
    finally:
        from lib.tasks_pkg.event_log import flush_pending
        from lib.tasks_pkg.manager.runtime import chat_task_runtime
        for context in workers.contexts.values():
            flush_pending(context.claim.task_id)
            chat_task_runtime.discard(context.claim.task_id)
        workers.close()


def _delegate(p):
    result = p.tool('delegate_project_work', {'title': 'Real-path Worker',
        'brief': 'Write and verify result.txt in the owned checkout.', 'idempotency_key': 'real-path'})
    assert result.get('assignment_id') and result.get('conversation_id'), result
    return result['assignment_id'], result['conversation_id']


def _execution(p, assignment_id):
    return p.repository.read(assignment_id)['execution']


def _run(p, attempt_id, **plan):
    p.plans[attempt_id] = dict(plan)
    turn = p.workers.run(attempt_id)
    return turn, p.plans[attempt_id]


def _settlement(turn):
    settlement = turn.get('settlement') or {}
    return (turn.get('status'), settlement.get('cause'),
            (settlement.get('error') or {}).get('detail') if isinstance(settlement.get('error'), dict) else None)


def _replay_anchor(turn):
    option = next(option for option in (turn.get('settlement') or {}).get('resumeOptions') or []
                  if option.get('operation') == 'continue')
    return option['anchor']


def _assert_follows(p, assignment_id, conversation, attempt_id, turn):
    """The assignment, its ledger and its checkout follow the successor attempt."""
    from lib.integration_state_repository import find_workspace
    from lib.project_dispatch.service import ProjectDispatchService
    work_id = 'project-worker:' + assignment_id
    assigned = p.repository.read(assignment_id)
    assert assigned['execution']['attemptId'] == attempt_id
    assert assigned['execution']['taskId'] == work_id
    view = ProjectDispatchService(p.repository).view(assignment_id, principal=p.principal)
    assert view['execution']['attemptId'] == attempt_id
    assert view['execution']['taskId'] == 'conversation-attempt:' + attempt_id
    assert view['execution']['status'] == 'completed'
    assert view['execution']['resultRevision'] == turn['projectionRevision']
    workspace = find_workspace(str(p.root), work_id, user_id=p.owner)
    assert workspace is not None and workspace['origin']['executorTaskId'] == 'conversation-attempt:' + attempt_id
    ran = p.plans[attempt_id]['task']
    assert ran['id'] == 'conversation-attempt:' + attempt_id
    assert ran['_principalContext']['subject_id'] == 'project-dispatch:' + assignment_id
    assert ran['config']['projectPath'] == workspace['workspace_path'] != str(p.root)
    assert view['delivery'] is not None and view['delivery']['taskId'] == work_id
    return assigned


def _failed_worker_turn(p, predecessor):
    """A Worker Turn that failed after the assignment had reserved two tools.

    ``restart_then_failed_start`` is the 2026-11-07 chain (a restart, then a
    boot-recovery Continue that never entered); ``provider_error`` is the
    2026-11-06 one (an HTTP 400 in the middle of the Worker's run).
    """
    from lib.project_dispatch.recovery import recover_project_dispatch
    from lib import turn_lifecycle as turns
    assignment_id, conversation = _delegate(p)
    first = _execution(p, assignment_id)
    if predecessor == 'provider_error':
        turn, _ = _run(p, first['attemptId'], tools=2, outcome='provider_error')
        assert _settlement(turn)[0] == 'failed', turn
        assert p.repository.read(assignment_id)['toolReservations']['count'] == 2
        return assignment_id, conversation, first, turn
    turn, _ = _run(p, first['attemptId'], tools=2, outcome='restart')
    assert _settlement(turn)[:2] == ('interrupted', 'server_restart'), turn
    assert p.repository.read(assignment_id)['toolReservations']['count'] == 2
    recovered = recover_project_dispatch(p.owner, str(p.root))
    assert recovered['accepted'] == 1, recovered
    second = _execution(p, assignment_id)
    assert second['attemptId'] != first['attemptId']
    p.workers.fail(second['attemptId'], RuntimeError('prepared task identity differs from conversation worker'))
    turn = turns.get_turn(conversation, second['turnId'], user_id=p.owner)
    assert _settlement(turn)[0] == 'failed', turn
    return assignment_id, conversation, second, turn


def test_a_fresh_worker_starts_as_its_claimed_task_in_the_assignment_checkout(platform):
    p = platform
    assignment_id, conversation = _delegate(p)
    first = _execution(p, assignment_id)
    turn, plan = _run(p, first['attemptId'], tools=1, outcome='complete')
    assert _settlement(turn)[0] == 'completed', _settlement(turn)
    assigned = _assert_follows(p, assignment_id, conversation, first['attemptId'], turn)
    assert assigned['toolReservations'] == {'rootTaskId': 'project-worker:' + assignment_id,
        'accountNonce': assigned['toolReservations']['accountNonce'], 'count': 1}
    assert plan['model_ordinal'] == 1


def test_a_restart_paused_worker_resumes_through_boot_recovery_as_a_claimed_task(platform):
    """The 2026-11-06 23:46 outage: every claimed Worker start refused its own identity."""
    from lib.project_dispatch.recovery import recover_project_dispatch
    p = platform
    assignment_id, conversation = _delegate(p)
    first = _execution(p, assignment_id)
    turn, _ = _run(p, first['attemptId'], tools=2, outcome='restart')
    assert _settlement(turn)[:2] == ('interrupted', 'server_restart'), turn
    recovered = recover_project_dispatch(p.owner, str(p.root))
    assert recovered['accepted'] == 1, recovered
    second = _execution(p, assignment_id)
    turn, plan = _run(p, second['attemptId'], tools=1, outcome='complete')
    assert _settlement(turn)[0] == 'completed', _settlement(turn)
    assigned = _assert_follows(p, assignment_id, conversation, second['attemptId'], turn)
    assert assigned['toolReservations']['count'] == 3
    assert plan['model_ordinal'] == 2


def _follow_up(p, conversation, turn, *, key, instruction):
    """The Coordinator's follow-up, pinned to the settled Turn it read."""
    return p.tool('control_conversation', {
        'action': 'follow_up', 'conversation_id': conversation, 'turn_id': turn['turnId'],
        'attempt_id': turn['currentAttemptId'], 'expected_revision': turn['projectionRevision'],
        'instruction': instruction, 'idempotency_key': key})


def _input_of(conversation, turn_id, owner):
    from lib import turn_lifecycle as turns
    output = turns.get_turn(conversation, turn_id, user_id=owner)
    return turns.get_turn(conversation, output['parentTurnId'], user_id=owner)


def _model_context(p, conversation, turn_id, attempt_id):
    """The history the attempt's model requests are built from (the fixture stubs it for runs)."""
    return json.dumps(production_build_api_messages(
        conversation, turn_id, p.workers.config(attempt_id), user_id=p.owner), ensure_ascii=False)


def test_a_follow_up_continues_an_ended_worker_session_as_its_next_turn(platform):
    """Owner direction 2026-11-08: one Worker session per goal ("adding an elephant could
    always be discussed within a single session"). An instruction to an ended Worker becomes
    the next Turn of its own conversation, under the same assignment, grant, account and
    checkout. The ended Turn is never regenerated; the follow-up result is what the
    Coordinator reviews and delivers."""
    from lib import turn_lifecycle as turns
    from lib.integration_state_repository import find_workspace
    p = platform
    # Reviewed project deliveries land as their own commit (delivered means committed).
    _git(p.root, 'config', '--local', 'tofu.linearCheckpoint', 'true')
    head = _git(p.root, 'rev-parse', 'HEAD')
    assignment_id, conversation = _delegate(p)
    first = _execution(p, assignment_id)
    ended, _ = _run(p, first['attemptId'], tools=2, outcome='complete',
                    write={'first.txt': 'first\n'})
    assert _settlement(ended)[0] == 'completed', _settlement(ended)
    instruction = 'Also add second.txt, then report both files.'
    receipt = _follow_up(p, conversation, ended, key='second-file', instruction=instruction)
    assert 'error' not in receipt, receipt
    assert receipt['assignment_id'] == assignment_id and receipt['conversation_id'] == conversation
    follow_up, successor = receipt['turn']['turnId'], receipt['attempt']['attemptId']
    assert follow_up != first['turnId']
    # Nothing regenerated: the ended Turn keeps its attempt, revision and result.
    kept = turns.get_turn(conversation, first['turnId'], user_id=p.owner)
    assert (kept['currentAttemptId'], kept['projectionRevision'], kept['status']) == (
        first['attemptId'], ended['projectionRevision'], 'completed')
    assert kept['projection']['content'] == ended['projection']['content']
    # The instruction is Coordinator context right after the result it read, never a human message.
    given = _input_of(conversation, follow_up, p.owner)
    assert given['actor'] == 'virtual_user' and given['parentTurnId'] == first['turnId']
    assert instruction in given['projection']['content']
    context = _model_context(p, conversation, follow_up, successor)
    assert ended['projection']['content'] in context and instruction in context
    # The same assignment follows the new Turn; replaying the call starts nothing.
    assert _execution(p, assignment_id)['attemptId'] == successor
    replay = _follow_up(p, conversation, ended, key='second-file', instruction=instruction)
    assert replay['attempt']['attemptId'] == successor and replay['idempotent_replay'], replay
    assert 'error' in _follow_up(p, conversation, ended, key='another-key', instruction=instruction)
    turn, plan = _run(p, successor, tools=1, outcome='complete', write={'second.txt': 'second\n'},
                      observe=lambda task: _receipt(p, assignment_id))
    assert _settlement(turn)[0] == 'completed', _settlement(turn)
    assigned = _assert_follows(p, assignment_id, conversation, successor, turn)
    # One conserved account across both Turns: its ledgers continue, never restart.
    assert assigned['toolReservations']['count'] == 3
    assert plan['model_ordinal'] == 2
    # Its start reopened the held delivery for itself; its settlement captured both edits.
    opened = plan['observed']
    assert (opened['state'], opened['executorTaskId']) == ('working', 'conversation-attempt:' + successor), opened
    checkpoint = find_workspace(str(p.root), 'project-worker:' + assignment_id, user_id=p.owner)['checkpoint_sha']
    assert _receipt(p, assignment_id)['executorTaskId'] == 'conversation-attempt:' + successor
    assert _git(p.root, 'show', checkpoint + ':first.txt') == 'first'
    assert _git(p.root, 'show', checkpoint + ':second.txt') == 'second'
    reviewed = _accept(p, assignment_id, turn, checkpoint, note='Follow-up result checked')
    assert 'error' not in reviewed, reviewed
    accepted = p.repository.read(assignment_id)
    assert accepted['review']['resultRevision'] == turn['projectionRevision']
    assert accepted['publication']['checkpointSha'] == checkpoint
    _drain_receiver()
    assert _delivery(p, assignment_id)['status'] == 'delivered', _receipt(p, assignment_id)
    assert _git(p.root, 'rev-parse', 'HEAD^') == head
    assert _git(p.root, 'show', 'HEAD:first.txt') == 'first'
    assert _git(p.root, 'show', 'HEAD:second.txt') == 'second'


@pytest.mark.parametrize('ended_by', ['accepted', 'delivered'])
def test_a_follow_up_after_a_final_result_continues_the_session_as_a_linked_assignment(
        platform, ended_by):
    """DX-BOT-ADD (c7e6009a) was accepted and delivered, then the owner asked for more. A
    review or delivery is final for its assignment, so the follow-up is a new assignment of
    the current request dispatched into the same conversation, after the result it continues."""
    from lib import turn_lifecycle as turns
    p = platform
    assignment_id, conversation = _delegate(p)
    first = _execution(p, assignment_id)
    ended, _ = _run(p, first['attemptId'], tools=1, outcome='complete')
    assert _settlement(ended)[0] == 'completed', _settlement(ended)
    if ended_by == 'accepted':
        reviewed = p.tool('review_project_work', {
            'assignment_id': assignment_id, 'result_revision': ended['projectionRevision'],
            'decision': 'accept', 'note': 'First stage checked'})
        assert 'error' not in reviewed, reviewed
    instruction = 'Now also add the bot automatically whenever a Router group is created.'
    receipt = _follow_up(p, conversation, ended, key='auto-add', instruction=instruction)
    assert 'error' not in receipt, receipt
    successor_id = receipt['assignment_id']
    assert successor_id != assignment_id and receipt['continues_assignment'] == assignment_id
    assert receipt['conversation_id'] == conversation and receipt['dispatch'] == 'accepted', receipt
    row = p.repository.read(successor_id)
    assert row['request']['recipientId'] == conversation
    assert row['request']['authorizationSource'] == p.repository.read(assignment_id)['request']['authorizationSource']
    # The final result keeps its association; nothing regenerated.
    assert _execution(p, assignment_id) == first
    kept = turns.get_turn(conversation, first['turnId'], user_id=p.owner)
    assert (kept['currentAttemptId'], kept['projectionRevision']) == (first['attemptId'], ended['projectionRevision'])
    given = _input_of(conversation, row['execution']['turnId'], p.owner)
    assert given['ordinal'] > kept['ordinal'] and instruction in given['projection']['content']
    context = _model_context(p, conversation, row['execution']['turnId'], row['execution']['attemptId'])
    assert ended['projection']['content'] in context and instruction in context
    replay = _follow_up(p, conversation, ended, key='auto-add', instruction=instruction)
    assert replay['assignment_id'] == successor_id, replay
    # One follow-up per observed result: another instruction for it is refused.
    other = _follow_up(p, conversation, ended, key='other', instruction='Rename the bot instead.')
    assert other.get('code') == 'follow_up_refused' and successor_id in other['error'], other
    assert len([row for row in p.repository.list() if row['request']['recipientId'] == conversation]) == 2
    turn, plan = _run(p, row['execution']['attemptId'], tools=1, outcome='complete')
    assert _settlement(turn)[0] == 'completed', _settlement(turn)
    _assert_follows(p, successor_id, conversation, row['execution']['attemptId'], turn)
    # Its own grant: a fresh account and checkout, so the ledgers start over.
    assert plan['model_ordinal'] == 1


def test_a_follow_up_is_refused_before_anything_starts_when_the_session_cannot_continue(platform):
    from lib import turn_lifecycle as turns
    p = platform
    assignment_id, conversation = _delegate(p)
    first = _execution(p, assignment_id)
    running = turns.get_turn(conversation, first['turnId'], user_id=p.owner)
    busy = _follow_up(p, conversation, running, key='too-early', instruction='Also do this.')
    assert 'error' in busy and busy['execution_started'] is False, busy
    ended, _ = _run(p, first['attemptId'], tools=1, outcome='complete', write={'first.txt': 'first\n'})
    stale = _follow_up(p, conversation, {**ended, 'projectionRevision': ended['projectionRevision'] - 1},
                       key='stale', instruction='Also do this.')
    assert 'error' in stale and stale['execution_started'] is False, stale
    p.tool('cancel_project_work', {'assignment_id': assignment_id})
    cancelled = _follow_up(p, conversation, ended, key='cancelled', instruction='Also do this.')
    assert 'error' in cancelled and cancelled['execution_started'] is False, cancelled
    assert turns.get_turn(conversation, first['turnId'], user_id=p.owner)['currentAttemptId'] == first['attemptId']
    assert _execution(p, assignment_id) == first
    assert [row['id'] for row in p.repository.list()] == [assignment_id]


FAILED_PREDECESSORS = pytest.mark.parametrize('predecessor', ['restart_then_failed_start', 'provider_error'])


@FAILED_PREDECESSORS
def test_a_coordinator_continue_of_a_failed_worker_turn_starts_and_the_assignment_follows(
        platform, predecessor):
    """N1: the Coordinator's control_conversation continue of a failed Worker never started."""
    from lib.conversations.session_control import control_session
    p = platform
    assignment_id, conversation, failed, turn = _failed_worker_turn(p, predecessor)
    receipt = control_session(p.coordinator, p.scope, {
        'action': 'continue', 'conversation_id': conversation, 'turn_id': failed['turnId'],
        'attempt_id': failed['attemptId'], 'idempotency_key': 'continue-failed-worker',
        'expected_revision': turn['projectionRevision'], 'resume_anchor': _replay_anchor(turn)})
    successor = receipt['attempt']['attemptId']
    assert _execution(p, assignment_id)['attemptId'] == successor
    turn, plan = _run(p, successor, tools=1, outcome='complete')
    assert _settlement(turn)[0] == 'completed', _settlement(turn)
    assigned = _assert_follows(p, assignment_id, conversation, successor, turn)
    assert assigned['toolReservations']['count'] == 3
    assert plan['model_ordinal'] == 2


def _owner_continue(p, conversation, turn):
    """The browser's Continue: the owner's principal and its own view of the settings."""
    from lib.conversation_sync.runtime import conversation_turn_commands
    from lib.conversations.repository import ConversationRepository
    settings = ConversationRepository().get(conversation, user_id=p.owner,
                                            include_messages=False).metadata['settings']
    browser_config = {'model': 'fixture-model', 'projectPath': settings.get('projectPath') or str(p.root)}
    result = conversation_turn_commands.create_attempt(conversation, turn['turnId'], p.owner, {
        'commandId': 'browser-continue-' + uuid.uuid4().hex, 'operation': 'continue',
        'expectedProjectionRevision': turn['projectionRevision'], 'config': browser_config,
        'resumeAnchor': _replay_anchor(turn)}, request_started_at=time.time(), principal=p.principal).value
    return result['attempt']['attemptId']


@pytest.mark.parametrize('continued_by', ['coordinator', 'owner'])
def test_a_continue_after_an_unbound_owner_attempt_runs_under_the_assignment(
        platform, monkeypatch, continued_by):
    """Live 2026-11-07 10:45 (worker-80e6d733, turn e32f1242, assignment 6ddab2df).

    A browser Continue accepted before D3 ran unbound and became the Turn's current
    attempt; it was then interrupted by a restart. The Coordinator's continue of
    that attempt started but ran unbound too, so the assignment kept naming its
    failed attempt and could never be reviewed.
    """
    from lib import turn_lifecycle as turns
    from lib.conversation_sync import command_service
    from lib.conversations.session_control import control_session
    p = platform
    assignment_id, conversation, failed, turn = _failed_worker_turn(p, 'restart_then_failed_start')
    with monkeypatch.context() as before_d3:
        # Without lane a6917e22's binding (8d4407ccdb) the browser path is unbound anyway.
        before_d3.setattr(command_service, '_worker_turn_binding', lambda *a, **kw: None,
                          raising=False)
        unbound = _owner_continue(p, conversation, turn)
    turn, plan = _run(p, unbound, tools=3, outcome='restart')
    assert _settlement(turn)[:2] == ('interrupted', 'server_restart'), turn
    assert plan['task']['_principalContext']['kind'] == 'user'
    assert _execution(p, assignment_id)['attemptId'] == failed['attemptId']
    if continued_by == 'coordinator':
        successor = control_session(p.coordinator, p.scope, {
            'action': 'continue', 'conversation_id': conversation, 'turn_id': turn['turnId'],
            'attempt_id': unbound, 'idempotency_key': 'continue-after-unbound',
            'expected_revision': turn['projectionRevision'],
            'resume_anchor': _replay_anchor(turn)})['attempt']['attemptId']
    else:
        successor = _owner_continue(p, conversation, turn)
    assert _execution(p, assignment_id)['attemptId'] == successor
    turn, plan = _run(p, successor, tools=1, outcome='complete')
    assert _settlement(turn)[0] == 'completed', _settlement(turn)
    assigned = _assert_follows(p, assignment_id, conversation, successor, turn)
    # The unbound attempt's tools ran outside the grant and were never recorded.
    assert assigned['toolReservations']['count'] == 3
    assert plan['model_ordinal'] == 2
    assert turns.get_attempt(unbound, user_id=p.owner)['status'] == 'interrupted'


@FAILED_PREDECESSORS
def test_an_owner_continue_of_a_failed_worker_turn_runs_under_the_assignment(platform, predecessor):
    """D3: the browser Continue ran unbound in a new checkout; the assignment never followed."""
    p = platform
    assignment_id, conversation, failed, turn = _failed_worker_turn(p, predecessor)
    successor = _owner_continue(p, conversation, turn)
    turn, plan = _run(p, successor, tools=1, outcome='complete')
    assert _settlement(turn)[0] == 'completed', _settlement(turn)
    assigned = _assert_follows(p, assignment_id, conversation, successor, turn)
    assert assigned['toolReservations']['count'] == 3
    assert plan['model_ordinal'] == 2


def test_owner_continue_refuses_revoked_grant_before_accepting_an_attempt(platform, monkeypatch):
    from lib import turn_lifecycle as turns
    from lib.conversation_sync.command_service import LifecycleConflict
    from lib.project_dispatch.admission import TrustedDispatch

    p = platform
    assignment_id, conversation, failed, turn = _failed_worker_turn(p, 'provider_error')

    def revoked(self):
        raise PermissionError('revoked Worker grant')

    monkeypatch.setattr(TrustedDispatch, 'checked', revoked)
    with pytest.raises(LifecycleConflict, match='can no longer authorize a resume'):
        _owner_continue(p, conversation, turn)
    assert turns.get_turn(conversation, turn['turnId'], user_id=p.owner)['currentAttemptId'] == failed['attemptId']
    assigned = p.repository.read(assignment_id)
    assert assigned['execution']['attemptId'] == failed['attemptId']
    assert assigned['toolReservations']['count'] == 2


def test_synthetic_caller_cannot_borrow_owner_resume_authority(platform):
    from lib import turn_lifecycle as turns
    from lib.identity import PrincipalContext

    p = platform
    assignment_id, conversation, failed, turn = _failed_worker_turn(p, 'provider_error')
    p.principal = PrincipalContext.system(subject_id='unrelated-synthetic-caller',
        scopes=(), owner_user_id=p.owner, tenant_id=TENANT)
    with pytest.raises(PermissionError, match='identity does not match'):
        _owner_continue(p, conversation, turn)
    assert turns.get_turn(conversation, turn['turnId'], user_id=p.owner)['currentAttemptId'] == failed['attemptId']
    assert p.repository.read(assignment_id)['execution']['attemptId'] == failed['attemptId']


# Delivery follows the attempt that completes. Live 2026-11-08: assignments
# 2d9152a3 (first attempt storage-fenced, Coordinator checkpoint_resume) and
# 6aa7e803 (restart boot-continues) resumed in place and completed, yet their
# delivery kept the first attempt's `preserved` receipt; the accepts recorded
# and nothing published.

def _receipt(p, assignment_id):
    from lib.integration_state_repository import find_workspace
    row = find_workspace(str(p.root), 'project-worker:' + assignment_id, user_id=p.owner) or {}
    return dict((row.get('origin') or {}).get('delivery') or {})


def _delivery(p, assignment_id):
    from lib.project_dispatch.service import ProjectDispatchService
    return ProjectDispatchService(p.repository).view(assignment_id, principal=p.principal)['delivery']


def _drain_receiver():
    """The receiver passes the fixture's disabled wake would have started."""
    from lib.project_mod import delivery
    for _ in range(10):
        if not delivery.resume_pending_workspace_deliveries():
            return
    pytest.fail('the delivery receiver kept finding work')


def _option(turn, operation):
    return next(option['anchor'] for option in (turn.get('settlement') or {}).get('resumeOptions') or []
                if option.get('operation') == operation)


def _coordinator_resume(p, conversation, turn, operation):
    from lib.conversations.session_control import control_session
    return control_session(p.coordinator, p.scope, {
        'action': operation, 'conversation_id': conversation, 'turn_id': turn['turnId'],
        'attempt_id': turn['currentAttemptId'], 'idempotency_key': 'resume-' + turn['currentAttemptId'],
        'expected_revision': turn['projectionRevision'],
        'resume_anchor': _option(turn, operation)})['attempt']['attemptId']


def _interrupted_chain(p, chain):
    """The first attempt(s) of a Worker Turn and the successor that resumes it in place."""
    from lib.project_dispatch.recovery import recover_project_dispatch
    assignment_id, conversation = _delegate(p)
    first = _execution(p, assignment_id)
    outcome = {'fence_then_coordinator_checkpoint_resume': 'fence',
               'provider_error_then_owner_continue': 'provider_error'}.get(chain, 'restart')
    turn, _ = _run(p, first['attemptId'], tools=2, outcome=outcome,
                   write={'result.txt': 'first attempt\n'})
    left = _receipt(p, assignment_id)
    assert left.get('executorTaskId', '') in ('', 'conversation-attempt:' + first['attemptId']), left
    if outcome == 'restart':
        assert _settlement(turn)[:2] == ('interrupted', 'server_restart'), turn
        assert (left['state'], left['taskOutcome']) == ('working', 'interrupted'), left
        assert recover_project_dispatch(p.owner, str(p.root))['accepted'] == 1
        successor = _execution(p, assignment_id)['attemptId']
        if chain == 'restart_then_failed_boot_continue_then_coordinator_continue':
            # 6aa7e803: the boot Continue ran in the checkout and failed, denying its own receipt.
            turn, _ = _run(p, successor, tools=1, outcome='provider_error',
                           write={'result.txt': 'boot continue\n'})
            denied = _receipt(p, assignment_id)
            assert (denied['state'], denied['taskOutcome'], denied['executorTaskId']) == (
                'preserved', 'failed', 'conversation-attempt:' + successor), denied
            successor = _coordinator_resume(p, conversation, turn, 'continue')
        return assignment_id, successor
    # The predecessor's own settlement preserved its checkout without publishing it.
    assert _settlement(turn)[0] == 'failed', turn
    assert (left['state'], left['taskOutcome']) == ('preserved', 'failed'), left
    if chain == 'fence_then_coordinator_checkpoint_resume':
        assert _settlement(turn)[1] == 'internal_fence_abort', turn
        return assignment_id, _coordinator_resume(p, conversation, turn, 'checkpoint_resume')
    return assignment_id, _owner_continue(p, conversation, turn)


def _accept(p, assignment_id, turn, checkpoint, note='accepted'):
    return p.tool('review_project_work', {
        'assignment_id': assignment_id, 'decision': 'accept', 'note': note,
        'result_revision': turn['projectionRevision'], 'checkpoint_sha': checkpoint})


@pytest.mark.parametrize('chain', ['fence_then_coordinator_checkpoint_resume',
                                   'provider_error_then_owner_continue',
                                   'restart_then_boot_continue',
                                   'restart_then_failed_boot_continue_then_coordinator_continue'])
def test_a_resumed_worker_attempt_reopens_settles_and_publishes_its_delivery(platform, chain):
    from lib.integration_state_repository import find_workspace
    p = platform
    # Reviewed project deliveries land as their own commit (delivered means committed).
    _git(p.root, 'config', '--local', 'tofu.linearCheckpoint', 'true')
    head = _git(p.root, 'rev-parse', 'HEAD')
    assignment_id, successor = _interrupted_chain(p, chain)
    assert _execution(p, assignment_id)['attemptId'] == successor
    # The receipt the interrupted attempt left names that attempt, not the work id.
    assert _receipt(p, assignment_id)['executorTaskId'] not in ('', 'conversation-attempt:' + successor)

    turn, plan = _run(p, successor, tools=1, outcome='complete', write={'result.txt': 'resumed attempt\n'},
                      observe=lambda task: _receipt(p, assignment_id))
    assert _settlement(turn)[0] == 'completed', _settlement(turn)
    _assert_follows(p, assignment_id, None, successor, turn)
    # Its start reopened the delivery for itself, with nothing captured.
    opened = plan['observed']
    assert (opened['state'], opened['taskOutcome'], opened.get('checkpointSha', '')) == ('working', '', ''), opened
    assert opened['executorTaskId'] == 'conversation-attempt:' + successor

    # Its completion captured its own checkout and waits for review, like a fresh Worker.
    settled = _receipt(p, assignment_id)
    checkpoint = find_workspace(str(p.root), 'project-worker:' + assignment_id, user_id=p.owner)['checkpoint_sha']
    assert (settled['state'], settled['taskOutcome'], settled['reason']) == (
        'blocked', 'completed', 'project_publication_authority_missing'), settled
    assert settled['executorTaskId'] == 'conversation-attempt:' + successor
    assert settled['checkpointSha'] == checkpoint
    assert _git(p.root, 'show', checkpoint + ':result.txt') == 'resumed attempt'
    assert _delivery(p, assignment_id)['status'] == 'blocked'

    reviewed = _accept(p, assignment_id, turn, checkpoint)
    assert 'error' not in reviewed, reviewed
    assert p.repository.read(assignment_id)['publication']['checkpointSha'] == checkpoint
    assert _receipt(p, assignment_id)['state'] == 'pending'

    _drain_receiver()
    delivered = _delivery(p, assignment_id)
    assert delivered['status'] == 'delivered', (delivered, _receipt(p, assignment_id))
    assert delivered['checkpointSha'] == checkpoint
    assert _git(p.root, 'rev-parse', 'HEAD^') == head
    assert _git(p.root, 'show', 'HEAD:result.txt') == 'resumed attempt'


def test_an_accept_its_delivery_cannot_publish_is_refused_and_never_recorded(platform, monkeypatch):
    """A receipt from before attempt-scoped delivery, and the remedy its refusal names."""
    from lib import integration_control, turn_lifecycle as turns
    from lib.integration_state_repository import find_workspace
    p = platform
    original = integration_control.bind_task_workspace
    with monkeypatch.context() as unscoped:
        # Before this change a start binding named no attempt.
        unscoped.setattr(integration_control, 'bind_task_workspace',
                         lambda *a, executor_task_id='', **kw: original(*a, **kw))
        assignment_id, successor = _interrupted_chain(p, 'fence_then_coordinator_checkpoint_resume')
        turn, _ = _run(p, successor, tools=1, outcome='complete', write={'result.txt': 'resumed attempt\n'})
    assert _settlement(turn)[0] == 'completed', _settlement(turn)
    left = _receipt(p, assignment_id)
    assert left['state'] == 'preserved' and left['taskOutcome'] == 'failed', left
    checkpoint = find_workspace(str(p.root), 'project-worker:' + assignment_id, user_id=p.owner)['checkpoint_sha']

    refused = _accept(p, assignment_id, turn, checkpoint)
    assert refused['error'].startswith('Publication cannot start: the delivery is preserved'), refused
    assert 'Task did not complete' in refused['error'] and refused['execution_started'] is False
    assert 'control_conversation' in refused['recovery']
    assigned = p.repository.read(assignment_id)
    assert assigned['review'] is None and assigned.get('publication') is None
    assert _receipt(p, assignment_id) == left

    # The named remedy: a new attempt of the Worker Turn reopens the delivery in place.
    turn = turns.get_turn(turn['conversationId'], turn['turnId'], user_id=p.owner)
    regenerated = _coordinator_resume(p, turn['conversationId'], turn, 'regenerate')
    turn, _ = _run(p, regenerated, tools=1, outcome='complete', write={'result.txt': 'regenerated\n'})
    assert _settlement(turn)[0] == 'completed', _settlement(turn)
    settled = _receipt(p, assignment_id)
    assert (settled['state'], settled['taskOutcome']) == ('blocked', 'completed'), settled
    assert 'error' not in _accept(p, assignment_id, turn, settled['checkpointSha'])
    _drain_receiver()
    assert _delivery(p, assignment_id)['status'] == 'delivered'
    assert (p.root / 'result.txt').read_text() == 'regenerated\n'
