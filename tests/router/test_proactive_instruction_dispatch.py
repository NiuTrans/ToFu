"""A standing instruction the owner granted dispatches project work without a new "继续".

docs/ROUTER.md R2/R4/R10: the frozen owner text is the authority, and the
router's note / triggering event is nonhuman evidence that must reach the same
Coordinator request. Delivering that evidence as a SECOND input (the old
behaviour) manufactured a synthetic Coordinator request whenever the steer
window had closed: the request named the instruction, re-attempted its work and
was refused (``Synthetic authorization origin denied``), so the Project Router
stalled asking the owner for "继续" — observed live 2026-11-09.

Over a real storage authority: real Global Router binding, real standing
instruction record, real ``dispatch_scheduled_turn``/turn command, real queued
turn activation and real ``source_for_task`` / Coordinator tool adapters. Only
executor dispatch, tool policy assembly and the isolated-workspace preflight
are local.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.support.semantic_authorities import semantic_authority as semantic_authority

pytest_plugins = ('tests.support.postgres_cluster',)
# Native runs when TOFUDB_BIN names a source-matched engine, and skips otherwise.
pytestmark = [pytest.mark.unit,
              pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)]

TEXT = '有人报告 bug 时，在 parallel 复现；只有确认修好了才告诉我。'
NOTE = 'Trigger: channel event dx:msg:9001 — Alice reports the parser crashes on empty input.'


@pytest.fixture
def routed(semantic_authority, monkeypatch, tmp_path):
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.identity import PrincipalContext
    from lib.project_dispatch.delegation import capture_worker_policy
    from lib.router.binding import global_router
    from lib.router.home import router_home_location
    from lib.router.instructions import RouterInstructions
    from lib.turn_lifecycle import create_turn_pair
    client, owner, _engine = semantic_authority
    monkeypatch.setattr('lib.storage.get_storage_client', client)
    monkeypatch.setattr('lib.scheduler.conversation_dispatch.get_storage_client', client)
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(tmp_path / 'router'))
    def record_attempt(attempt_id, *, user_id):
        from lib.turn_lifecycle import bind_task
        bind_task(attempt_id, 'executor-task', user_id=user_id)
    monkeypatch.setattr('lib.conversation_sync.worker_dispatch.dispatch_accepted_attempt', record_attempt)
    monkeypatch.setattr('lib.conversations.coordinator_workers.require_isolated_worker_root',
                        lambda project: None)
    router = global_router(owner, create=True)
    project = tmp_path / 'project'
    project.mkdir()
    target = ProjectCoordinationRepository(owner).provision(
        str(project), title='Project coordinator', settings={}, command_id='route-to-project')
    pair = create_turn_pair(router['conversationId'], command_id='owner-request',
                            input_projection={'content': TEXT}, config={}, user_id=owner)
    instruction = RouterInstructions(owner).create(
        router_conversation_id=router['conversationId'], source_turn=pair['submittedTurn'],
        label='Bug reports', project_path=str(project), thread='dx:gid:42', idempotency_key='bugs')
    state = SimpleNamespace(owner=owner, router=router, target=target, project=str(project),
                            instruction=instruction, monkeypatch=monkeypatch)
    state.scope = {'user_id': owner, 'project_path': router_home_location(owner),
                   'conversation_id': router['conversationId'], 'binding_revision': router['revision']}
    state.task = {'id': 'router-task', 'convId': router['conversationId'], '_userId': owner,
                  'config': {'projectPath': router['projectPath']}}
    state.arguments = {'project_path': str(project), 'instruction_id': instruction['instructionId'],
                       'event_id': 'dx:msg:9001', 'note': NOTE}
    state.coordinator_scope = {'user_id': owner, 'project_path': str(project),
                               'conversation_id': target['conversationId'],
                               'binding_revision': target['revision']}

    def coordinator_task(turn, attempt, *, task_id='project-task'):
        """The executing Coordinator request of one accepted Turn/Attempt."""
        from lib.turn_lifecycle import bind_task, mark_task_started
        if not attempt.get('taskId'):
            bind_task(attempt['attemptId'], task_id, user_id=owner)
        task_id = attempt.get('taskId') or task_id
        mark_task_started(attempt['attemptId'], task_id, user_id=owner)
        from lib.turn_lifecycle import get_attempt
        attempt = get_attempt(attempt['attemptId'], user_id=owner)
        task = {'id': task_id, 'convId': target['conversationId'], '_userId': owner,
                '_attended': True, '_turnId': turn['turnId'], '_attemptId': attempt['attemptId'],
                '_principalContext': PrincipalContext.user(
                    subject_id=f'user:{owner}', owner_user_id=owner, scopes=[]).to_payload(),
                'config': {'projectPath': str(project), 'model': 'fixture-model'}}
        monkeypatch.setattr('lib.tasks_pkg.coordinator_policy.coordinator_scope',
                            lambda _task: state.coordinator_scope)
        capture_worker_policy(task, task['config'], [
            {'function': {'name': name}} for name in
            ('read_files', 'write_file', 'run_command', 'delegate_project_work', 'get_project_workers',
             'finish_project_request', 'report_to_user')], 'fixture-model',
            worker_model='fixture-model')
        return task
    state.coordinator_task = coordinator_task
    yield state
    from tests.support.chat_tasks import chat_task_registry
    chat_task_registry.pop('live-task', None)


def _worker_factory(state, created):
    """Create the Worker conversation through the ordinary owner-scoped command."""
    from lib.conversations.coordinator_tools import _create
    def create(scope, arguments):
        created.append(arguments)
        return _create(scope, arguments)
    return create


def _forward_with_closed_steer_window(state):
    """A live attempt whose steer execution window is closed (never started).

    The real turn command then durably queues the forward instead of steering
    it — the production state that used to queue the evidence as a second input.
    """


def _live_reply(state):
    from lib.turn_lifecycle import create_turn_pair
    from tests.support.chat_tasks import register_local_executor
    live = create_turn_pair(state.target['conversationId'], command_id='earlier-request',
                            input_projection={'content': 'Status?'}, config={}, user_id=state.owner)
    register_local_executor('live-task', conv_id=state.target['conversationId'],
                            attempt_id=live['attempt']['attemptId'],
                            turn_id=live['turn']['turnId'], user_id=state.owner)
    return live


def _only_forwarded_input(state):
    from lib.storage import get_storage_client
    turns = get_storage_client().query('turn.list', {
        'conversation_id': state.target['conversationId'], 'user_id': state.owner})
    inputs = [item for item in turns if item.get('actor') == 'human'
              and item.get('turnId') != _earlier_input(state)]
    return inputs


def _earlier_input(state):
    from lib.storage import get_storage_client
    turns = get_storage_client().query('turn.list', {
        'conversation_id': state.target['conversationId'], 'user_id': state.owner})
    return next((item['turnId'] for item in turns
                 if (item.get('projection') or {}).get('content') == 'Status?'), None)


def test_one_authorized_request_carries_the_owner_words_and_their_evidence(routed):
    """The forward and its evidence are ONE Coordinator request, never two.

    Fail-before: the evidence is dispatched as a second peer input, which later
    becomes its own synthetic Coordinator request.
    """
    from lib.router.forwarding import route_to_project
    from lib.router.instructions import authored_text
    result = route_to_project(routed.task, routed.scope, routed.arguments)
    assert result['disposition'] == 'started'
    assert result['context_disposition'] is None, 'no second input may be dispatched'
    [forwarded] = _only_forwarded_input(routed)
    projection = forwarded['projection']
    assert projection['origin']['initiator'] == 'human'
    assert authored_text(forwarded) == TEXT, 'the owner words stay byte-identical'
    assert NOTE in projection['content'], 'the model reads the evidence with the request'
    assert projection['content'].startswith(TEXT)
    assert 'nonhuman' in projection['content']
    assert projection['origin']['forwardedFrom']['instructionId'] == routed.instruction['instructionId']


def test_a_queued_forward_never_leaves_a_synthetic_evidence_request(routed):
    """Fail-before reproduction of the live stall, now asserted fixed.

    With the steer window closed the evidence used to start its own synthetic
    Coordinator request; delegation, read-only worker queries and finishing
    were all refused. One request must now carry both, and it must work.
    """
    from lib.conversations.coordinator_workers import delegate, get_workers
    from lib.project_dispatch.delegation import source_for_task
    from lib.project_dispatch.repository import ProjectDispatchRepository
    from lib.router.forwarding import route_to_project
    from lib.turn_lifecycle import get_attempt, get_turn
    live = _live_reply(routed)
    _forward_with_closed_steer_window(routed)
    result = route_to_project(routed.task, routed.scope, routed.arguments)
    assert result['disposition'] == 'queued', result
    assert result['context_disposition'] is None, 'the evidence must not be a second input'
    # The earlier reply settles (no Stop record); the queued forward activates.
    from lib.turn_lifecycle import record_task_event
    from tests.support.chat_tasks import chat_task_registry
    live_task = chat_task_registry['live-task']
    live_task.update(content='done', thinking='', events=[], toolRounds=[], status='done')
    assert record_task_event(live_task, {'type': 'done', 'finishReason': 'stop'})
    from lib.storage import get_storage_client
    turns = get_storage_client().query('turn.list', {
        'conversation_id': routed.target['conversationId'], 'user_id': routed.owner})
    assert not [item for item in turns
                if (item.get('projection') or {}).get('origin', {}).get('initiator') == 'peer'], \
        'no synthetic Coordinator request was manufactured'
    # The authorized request itself delegates and reads its Workers.
    forwarded = _only_forwarded_input(routed)[0]
    reply = next(item for item in turns if item.get('parentTurnId') == forwarded['turnId'])
    task = routed.coordinator_task(reply, get_attempt(reply['currentAttemptId'], user_id=routed.owner))
    repository = ProjectDispatchRepository(routed.owner, routed.project)
    _, source, _ = source_for_task(repository, task)
    assert source['turnId'] == forwarded['turnId']
    assert get_workers(task, routed.coordinator_scope, {})['workers'] == []
    created = []
    outcome = delegate(task, routed.coordinator_scope,
                       {'title': 'Reproduce the bug', 'brief': 'Reproduce and fix', 'idempotency_key': 'work'},
                       create=_worker_factory(routed, created))
    assert outcome['dispatch'] == 'accepted', outcome
    assert created, 'a new Worker was dispatched under the standing instruction'
    row = repository.read(outcome['assignment_id'])
    assert row['request']['authorizationSource']['turnId'] == forwarded['turnId']


def test_an_idle_project_router_dispatches_from_the_same_request(routed):
    """An idle target starts one authorized request; it delegates and pages Workers."""
    from lib.conversations.coordinator_workers import delegate, get_workers
    from lib.project_dispatch.repository import ProjectDispatchRepository
    from lib.router.forwarding import route_to_project
    from lib.turn_lifecycle import get_attempt
    from lib.storage import get_storage_client
    result = route_to_project(routed.task, routed.scope, routed.arguments)
    assert result['disposition'] == 'started'
    forwarded = _only_forwarded_input(routed)[0]
    turns = get_storage_client().query('turn.list', {
        'conversation_id': routed.target['conversationId'], 'user_id': routed.owner})
    reply = next(item for item in turns if item.get('parentTurnId') == forwarded['turnId'])
    task = routed.coordinator_task(reply, get_attempt(reply['currentAttemptId'], user_id=routed.owner))
    repository = ProjectDispatchRepository(routed.owner, routed.project)
    assert get_workers(task, routed.coordinator_scope, {})['workers'] == []
    outcome = delegate(task, routed.coordinator_scope,
                       {'title': 'Reproduce', 'brief': 'Reproduce and fix', 'idempotency_key': 'work'},
                       create=_worker_factory(routed, []))
    assert outcome['dispatch'] == 'accepted', outcome


def test_scope_pause_cancel_and_unrelated_synthetic_input_stay_refused(routed):
    """The authority model is unchanged: scope, revocation, synthetic inputs."""
    from lib.project_dispatch.delegation import source_for_task
    from lib.project_dispatch.repository import ProjectDispatchRepository, SyntheticSource
    from lib.router.forwarding import route_to_project
    from lib.router.instructions import RouterInstructions
    from lib.turn_lifecycle import get_attempt
    from lib.storage import get_storage_client
    owner = RouterInstructions(routed.owner)
    other = routed.project + '-other'
    import os
    os.mkdir(other)
    with pytest.raises(PermissionError):
        route_to_project(routed.task, routed.scope, {**routed.arguments, 'project_path': other})
    owner.pause(routed.instruction['instructionId'])
    with pytest.raises(PermissionError):
        route_to_project(routed.task, routed.scope, routed.arguments)
    owner.resume(routed.instruction['instructionId'])
    route_to_project(routed.task, routed.scope, routed.arguments)
    owner.cancel(routed.instruction['instructionId'])
    with pytest.raises(PermissionError):
        route_to_project(routed.task, routed.scope,
                         {**routed.arguments, 'event_id': 'dx:msg:9002'})
    # An unrelated synthetic Coordinator request (a peer notice, no forward)
    # still cannot authorize Workers or even page them read-only.
    from lib.conversations.coordinator_workers import delegate, get_workers
    from lib.turn_lifecycle import create_turn_pair
    from lib.turn_initiation import INITIATOR_PEER, stamp_initiator
    peer = create_turn_pair(routed.target['conversationId'], command_id='peer-notice',
                            lane_id='peer',
                            input_projection=stamp_initiator(
                                {'role': 'user', 'content': 'A project report',
                                 'origin': {'initiator': 'peer', 'sourceConversationId': 'other'}},
                                INITIATOR_PEER),
                            config={}, user_id=routed.owner)
    task = routed.coordinator_task(peer['turn'], peer['attempt'], task_id='peer-task')
    repository = ProjectDispatchRepository(routed.owner, routed.project)
    with pytest.raises(SyntheticSource, match='Synthetic authorization origin denied'):
        source_for_task(repository, task)
    with pytest.raises(SyntheticSource):
        get_workers(task, routed.coordinator_scope, {})
    with pytest.raises(SyntheticSource):
        delegate(task, routed.coordinator_scope,
                 {'title': 'Not authorized', 'brief': 'No owner authority', 'idempotency_key': 'no'},
                 create=_worker_factory(routed, []))


def test_request_budget_covers_the_forwarded_request(routed):
    """Usage of the forwarded request and its Workers joins the request account."""
    from lib.project_dispatch.repository import ProjectDispatchRepository
    from lib.project_dispatch.request_budget import bind_source_account, request_view, source_policy
    from lib.router.forwarding import route_to_project
    from lib.turn_lifecycle import get_attempt
    from lib.storage import get_storage_client
    route_to_project(routed.task, routed.scope, routed.arguments)
    forwarded = _only_forwarded_input(routed)[0]
    turns = get_storage_client().query('turn.list', {
        'conversation_id': routed.target['conversationId'], 'user_id': routed.owner})
    reply = next(item for item in turns if item.get('parentTurnId') == forwarded['turnId'])
    task = routed.coordinator_task(reply, get_attempt(reply['currentAttemptId'], user_id=routed.owner))
    repository = ProjectDispatchRepository(routed.owner, routed.project)
    accepted = bind_source_account(task, repository=repository)
    assert accepted is not None, 'the forwarded human source opens a request account'
    assert accepted['authorizationSource']['turnId'] == forwarded['turnId']
    lease = task['_executionBudget'].reserve_model(task, input_tokens=5, max_output_tokens=5)
    lease.mark_dispatched()
    lease.settle({'prompt_tokens': 5, 'completion_tokens': 5})
    budget = source_policy(repository, task)
    view = request_view(repository, repository._load(repository._selection(budgets=[budget['id']]))[0],
                        budget['id'])
    assert view['usage']['tokens'] == 10
