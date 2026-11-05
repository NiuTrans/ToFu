"""A child model round evaluates its durable outcome once, with compact reads.

Every consumption seam of a round (injection, provider-send entry, each
prepared physical attempt, input acknowledgement) still revalidates
conversation scope, run identity, session state and retained assignment.
Only the round's first complete check evaluates the durable child outcome.
Every check is exactly one compact ``swarm.agent.state.get`` read: no seam
pages agent checkpoints or reads session specs/config. Real mailbox
repository, session loader, receipt seams and root dispatch boundary;
storage, conversation and retained lookups and the provider send are fake.
"""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from lib.llm._sse_core import prepare_request
from lib.llm.llm_errors import RateLimitError
from lib.llm.request import ModelRequestBudgetScope, dispatch_model_request
from lib.llm.stream_result import ProviderStreamResult
from lib.storage.errors import StorageError
from lib.swarm import run_messages
from lib.swarm.run_messages import (
    RunMessageError, RunMessageRepository, confirm_input, inject_messages,
    validate_round_target,
)
from lib.tasks_pkg import message_input_receipts as receipts
from tests.swarm.test_retained_agents import RecordClient

pytestmark = pytest.mark.unit

BINDING = {'owner': 7, 'tenant': '', 'conversationId': 'conv-a', 'projectPath': '/p',
           'swarmKey': 'conv-a', 'swarmRunId': 'launch', 'agentId': 'child-a',
           'retainedAgentId': 'retained-a', 'retainedRunId': 'assignment-1',
           'retainedScope': 'scope-a'}
class Storage(RecordClient):
    """Mailbox records plus one swarm session; records every compact state read."""

    def __init__(self):
        super().__init__()
        self.header = {'swarm_key': 'conv-a', 'task_id': 'launch', 'status': 'running'}
        self.agents = [{'agent_id': f'child-{name}', 'status': 'running'} for name in 'abc']
        self.reads = []
        self.unavailable = False

    def query(self, operation, payload):
        if not operation.startswith('swarm.'):
            return super().query(operation, payload)
        if self.unavailable:
            raise StorageError('database_timeout', 'synthetic outage', retryable=True)
        # Consumption never pages checkpoints or reads the full session header.
        assert operation == 'swarm.agent.state.get'
        assert set(payload) == {'swarm_key', 'agent_id'} and payload['swarm_key'] == 'conv-a'
        self.reads.append('state')
        if self.header is None:
            return None
        agent = next((a for a in self.agents if a['agent_id'] == payload['agent_id']), None)
        return {**deepcopy(self.header), 'agent': deepcopy(agent)}


@pytest.fixture
def world(monkeypatch):
    storage = Storage()
    monkeypatch.setattr('lib.storage.get_storage_client', lambda write=False: storage)
    settings = {'projectPath': '/p'}
    conversation = {'present': True}

    def get_conversation(_self, conv_id, *, user_id, include_messages):
        if conversation['present'] and conv_id == 'conv-a' and user_id == 7:
            return SimpleNamespace(metadata={'settings': dict(settings)})
        return None

    monkeypatch.setattr('lib.conversations.repository.ConversationRepository.get', get_conversation)
    retained = {'state': 'busy', 'run_id': 'assignment-1'}
    monkeypatch.setattr(
        'lib.swarm.retained_repository.RetainedAgentRepository.get',
        lambda _self, scope, agent_id: ((dict(retained), None)
                                        if (scope, agent_id) == ('scope-a', 'retained-a') else None))
    checks = []
    validate_target = run_messages.validate_target

    def recorded(binding, *, active_retained=True, outcome_admitted=False):
        checks.append('admitted' if outcome_admitted else 'complete')
        return validate_target(binding, active_retained=active_retained,
                               outcome_admitted=outcome_admitted)

    monkeypatch.setattr(run_messages, 'validate_target', recorded)
    binding = deepcopy(BINDING)
    repository = RunMessageRepository(binding)
    repository.submit(content='Extra evidence', kind='reference', key='logical-1',
                      sender_task_id='launch')
    storage.reads.clear()
    return SimpleNamespace(
        storage=storage, checks=checks, settings=settings, conversation=conversation,
        retained=retained,
        binding=binding, repository=repository, sends=[], events=[], between=lambda: None,
        messages=[{'role': 'user', 'content': 'Objective'}],
        task={'id': 'runtime-child-1', '_userId': 7, 'config': {}, '_swarmMessageBinding': binding})


def complete():
    return ProviderStreamResult.from_legacy(
        {'role': 'assistant', 'content': 'Read'}, 'stop', {'prompt_tokens': 10, 'completion_tokens': 1})


def physical(source, send):
    """Production scope, preparation and prepared-send seam; only the send is fake."""
    with ModelRequestBudgetScope(source) as scope:
        plan = prepare_request(scope.body, api_key='unused', base_url='https://example.invalid/v1')
        result = dispatch_model_request(plan, send, plan.body)
        scope.usage = result.usage
        return result


def model_call(world, *, between=lambda: None):
    """One provider send whose first physical attempt is refused and retried."""
    task = world.task
    observer = receipts.receipt_admission(task, None)

    def refuse(wire):
        world.sends.append('refused')
        raise RateLimitError('synthetic 429', status_code=429)

    def accept(wire):
        world.sends.append('accepted')
        return complete()

    def stream(task, source):
        with pytest.raises(RateLimitError):
            physical(dict(source, _model_budget_admit=observer), refuse)
        between()
        result = physical(dict(source, _model_budget_admit=observer), accept)
        task['_messageInputPhysicalIds'] = observer.matches
        return result

    body = {'model': 'glm-4-flash', 'max_tokens': 32,
            'messages': [{'role': m['role'], 'content': m['content']} for m in world.messages]}
    return receipts.stream_with_receipts(stream, task, body)


def model_round(world, round_num):
    """Order inside one root dispatch: inject, provider send, verified-input ACK."""
    inject_messages(world.task, world.messages, round_num)
    result = model_call(world, between=world.between)
    confirm_input(world.task, round_num=round_num)
    return result


@pytest.fixture
def loop(world, monkeypatch):
    from lib.tasks_pkg.orchestrator import _root_agent_loop as root
    monkeypatch.setattr(root, 'emit_round_open', lambda *a: None)
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event',
                        lambda task, event: world.events.append(event))
    monkeypatch.setattr(root._RootLoopHooks, '_dispatch_open_round',
                        lambda hooks, round_num, round_tools: model_round(world, round_num))
    return root._RootLoopHooks(SimpleNamespace(
        task=world.task, state=SimpleNamespace(), messages=world.messages))


def message_states(world):
    return [(m['state'], m.get('inputRound')) for m in world.repository.load()[0]['messages']]


def reset(world):
    world.storage.reads.clear()
    world.checks.clear()


def test_one_round_evaluates_the_outcome_once_with_one_compact_read_per_check(world, loop):
    assert loop.dispatch(0, None).is_verified_complete
    assert world.sends == ['refused', 'accepted']
    # Injection is complete; send entry, both prepared attempts and the ACK are admitted.
    assert world.checks == ['complete'] + ['admitted'] * 4
    assert world.storage.reads == ['state'] * 5
    assert message_states(world) == [('input_confirmed', 1)]


def test_next_round_evaluates_the_outcome_again(world, loop):
    loop.dispatch(0, None)
    world.messages.append({'role': 'assistant', 'content': 'Read'})
    world.repository.submit(content='Second evidence', kind='reference', key='logical-2',
                            sender_task_id='launch')
    reset(world)
    loop.dispatch(1, None)
    assert world.checks == ['complete'] + ['admitted'] * 4
    assert world.storage.reads == ['state'] * 5
    assert message_states(world) == [('input_confirmed', 1), ('input_confirmed', 2)]


def test_injection_restarts_a_redispatched_round_with_a_complete_check(world):
    validate_round_target(world.task, world.binding, round_num=0)
    reset(world)
    inject_messages(world.task, world.messages, 0)
    assert world.checks == ['complete']
    assert world.task['_swarmMessageAttemptIds']


def test_changed_binding_requires_a_complete_check(world):
    validate_round_target(world.task, world.binding, round_num=0)
    reset(world)
    validate_round_target(world.task, dict(world.binding, agentId='child-b'), round_num=0)
    world.binding['agentId'] = 'child-c'  # in-place change of the bound dict
    validate_round_target(world.task, world.binding, round_num=0)
    assert world.checks == ['complete', 'complete']


@pytest.mark.parametrize('round_num, equal_round', [
    (None, 0), (True, 1), (False, 0), (-1, 0), (0.0, 0), ('0', 0)])
def test_unknown_round_number_never_records_or_reuses_a_check(world, round_num, equal_round):
    validate_round_target(world.task, world.binding, round_num=round_num)
    validate_round_target(world.task, world.binding, round_num=equal_round)  # nothing to reuse
    validate_round_target(world.task, world.binding, round_num=round_num)  # equal round not honored
    assert world.checks == ['complete'] * 3


def test_send_outside_an_active_root_round_checks_every_attempt_completely(world):
    inject_messages(world.task, world.messages, 0)
    reset(world)
    model_call(world)
    assert world.checks == ['complete'] * 3


@pytest.mark.parametrize('fail, restore', [
    (lambda w: w.storage.header.update(status='cancelled'),
     lambda w: w.storage.header.update(status='running')),
    (lambda w: setattr(w.storage, 'unavailable', True),
     lambda w: setattr(w.storage, 'unavailable', False)),
    (lambda w: w.settings.update(archived=True), lambda w: w.settings.pop('archived')),
], ids=['session_cancelled', 'storage_unavailable', 'conversation_archived'])
def test_failed_complete_check_records_nothing(world, fail, restore):
    fail(world)
    with pytest.raises((RunMessageError, StorageError)):
        validate_round_target(world.task, world.binding, round_num=0)
    restore(world)
    reset(world)
    validate_round_target(world.task, world.binding, round_num=0)
    assert world.checks == ['complete']


REJECTIONS = [
    ('session_cancelled', lambda w: w.storage.header.update(status='cancelled'), 'message_run_ended'),
    ('session_quarantined', lambda w: w.storage.header.update(status='quarantined'), 'message_run_ended'),
    ('run_superseded', lambda w: w.storage.header.update(task_id='continue-task'), 'message_run_ended'),
    ('session_absent', lambda w: setattr(w.storage, 'header', None), 'message_run_ended'),
    ('worker_run_terminated', lambda w: w.storage.header.update(status='terminated'), 'message_run_ended'),
    ('conversation_archived', lambda w: w.settings.update(archived=True), 'message_scope_unavailable'),
    ('conversation_read_only', lambda w: w.settings.update(readOnly=True), 'message_scope_unavailable'),
    ('project_changed', lambda w: w.settings.update(projectPath='/other'), 'message_scope_unavailable'),
    ('conversation_deleted', lambda w: w.conversation.update(present=False), 'message_scope_unavailable'),
    ('assignment_superseded', lambda w: w.retained.update(run_id='assignment-2'), 'message_run_superseded'),
    ('assignment_reset', lambda w: w.retained.update(state='idle'), 'message_run_superseded'),
]


@pytest.mark.parametrize('revoke, code', [r[1:] for r in REJECTIONS], ids=[r[0] for r in REJECTIONS])
def test_admitted_check_still_raises_every_scope_rejection(world, revoke, code):
    validate_round_target(world.task, world.binding, round_num=0)
    reset(world)
    validate_round_target(world.task, world.binding, round_num=0)
    assert world.checks == ['admitted']  # this state is on the admitted path
    revoke(world)
    with pytest.raises(RunMessageError) as rejected:
        validate_round_target(world.task, world.binding, round_num=0)
    assert rejected.value.code == code
    assert world.checks == ['admitted', 'admitted']


def test_cancel_between_physical_attempts_blocks_the_retry_and_the_ack(world, loop):
    world.between = lambda: world.storage.header.update(status='cancelled')
    with pytest.raises(receipts.MessageInputScopeError):
        loop.dispatch(0, None)
    assert world.sends == ['refused']
    assert message_states(world) == [('submitted', None)]
    assert world.checks == ['complete'] + ['admitted'] * 3
    assert [e.get('reason') for e in world.events if e.get('type') == 'round_end'] == ['error']
    # The failed dispatch drops its round check too: a later call starts complete.
    world.storage.header['status'] = 'running'
    world.between = lambda: None
    reset(world)
    model_call(world)
    assert world.checks[0] == 'complete'


def test_ended_child_outcome_is_rejected_by_the_complete_check_and_closes_the_mailbox(world):
    validate_round_target(world.task, world.binding, round_num=0)
    world.storage.agents[0]['status'] = 'completed'
    reset(world)
    with pytest.raises(RunMessageError) as ended:
        validate_round_target(world.task, world.binding, round_num=1)
    assert ended.value.code == 'message_run_ended'
    assert world.checks == ['complete']
    assert world.repository.load()[0]['closed'] is True
    reset(world)
    with pytest.raises(RunMessageError):  # a rejection is never remembered as admitted
        validate_round_target(world.task, world.binding, round_num=1)
    assert world.checks == ['complete']
    with pytest.raises(RunMessageError) as late:
        world.repository.submit(content='Late', kind='reference', key='late', sender_task_id='launch')
    assert late.value.code == 'message_run_ended'


def test_admitted_check_leaves_a_mid_round_outcome_to_the_next_round(world):
    validate_round_target(world.task, world.binding, round_num=0)
    world.storage.agents[0]['status'] = 'completed'
    validate_round_target(world.task, world.binding, round_num=0)
    assert world.repository.load()[0]['closed'] is False
    with pytest.raises(RunMessageError) as ended:
        validate_round_target(world.task, world.binding, round_num=1)
    assert ended.value.code == 'message_run_ended'


def test_post_loop_model_call_starts_with_a_complete_check(world, loop):
    loop.dispatch(0, None)
    assert world.task['_activeRoundNum'] == 0  # failure attribution keeps the last round
    reset(world)
    model_call(world)  # e.g. a post-loop synthesis over the same context
    assert world.checks[0] == 'complete'
    assert world.checks.count('complete') == 1
