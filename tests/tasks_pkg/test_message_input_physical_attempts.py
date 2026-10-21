"""Mailbox input authority is rechecked at the existing prepared-send seam.

No provider network or real credentials: the dispatcher and prepared-attempt
owner are real; only storage reads and the expensive provider send are fake.
"""
from types import SimpleNamespace
import threading
import time

import pytest

from lib.llm.request import (
    ModelRequestBudgetScope, dispatch_model_request, mark_model_request_dispatched,
)
from lib.llm._sse_core import prepare_request
from lib.llm.stream_result import ProviderStreamResult
from lib.llm.llm_errors import AbortedError, RateLimitError
from lib.tasks_pkg import message_input_receipts as receipts

pytestmark = pytest.mark.unit


TEXT = 'Exact bounded mailbox carrier'
EMPTY = {'swarm': [], 'project': []}
FLOOR = {'prompt_tokens': 10, 'cache_read_tokens': 28654,
         'cache_creation_input_tokens': 42000}
HIT = {'prompt_tokens': 10, 'cache_read_tokens': 150000,
       'cache_creation_input_tokens': 1200}


def complete(usage=None):
    return ProviderStreamResult.from_legacy(
        {'role': 'assistant', 'content': 'Read'}, 'stop', usage or HIT.copy())


def body(text=TEXT):
    return {'model': 'glm-4-flash', 'messages': [{'role': 'user', 'content': text}],
            'max_tokens': 32}


@pytest.fixture(params=['project', 'swarm'])
def mailbox(request, monkeypatch):
    """Scope repository seams retain the real family-specific validators."""
    family = request.param
    state = {'revoked': False, 'acks': []}
    task = {'id': 'physical-mailbox', '_userId': 1, 'convId': 'synthetic',
            'config': {}, 'events': [], 'toolRounds': [], 'content': '',
            'thinking': '', 'content_lock': threading.Lock(),
            'events_lock': threading.Lock()}
    if family == 'project':
        from lib.conversations import project_messages as project
        monkeypatch.setattr(project, 'identity', lambda task: (1, 'recipient', 'project'))

        def scope(*args):
            if state['revoked']:
                raise project.ProjectMessageError('scope_unavailable', 'Project rebound')

        monkeypatch.setattr(project, 'scope', scope)

        class Repository:
            key = 'project-key'

            def __init__(self, *args):
                pass

            def load(self):
                return {'messages': [{'id': 'm', 'sender': 'sender'}]}, None

            def confirm(self, *args, **kwargs):
                state['acks'].append(args)

        monkeypatch.setattr(project, 'ProjectMailbox', Repository)
        key = Repository.key
        task['_projectMessageAttempt'] = {
            'owner': 1, 'recipient': 'recipient', 'project': 'project', 'ids': ['m']}
    else:
        from lib.swarm import run_messages, persistence, retained_repository
        binding = {'owner': 1, 'tenant': '', 'conversationId': '',
                   'projectPath': '', 'swarmKey': 'swarm', 'swarmRunId': 'run',
                   'agentId': 'agent', 'retainedAgentId': 'retained',
                   'retainedRunId': 'assignment', 'retainedScope': 'scope'}
        monkeypatch.setattr(persistence, 'load_session', lambda *a, **k:
                            {'task_id': 'run', 'status': 'running', 'agents': []})
        monkeypatch.setattr(retained_repository.RetainedAgentRepository, 'get',
                            lambda *a, **k: ({'state': 'busy', 'run_id':
                                             'reassigned' if state['revoked'] else 'assignment'}, None))
        key = run_messages.RunMessageRepository(binding).key
        monkeypatch.setattr(run_messages.RunMessageRepository, 'confirm',
                            lambda *a, **k: state['acks'].append(k))
        task['_swarmMessageBinding'] = binding
        task['_swarmMessageAttemptIds'] = ['m']
    id_key = '_projectMessageIds' if family == 'project' else '_swarmMessageIds'
    mailbox_key = '_projectMailbox' if family == 'project' else '_swarmMessageMailbox'
    receipts.remember(task, [{'content': TEXT, id_key: ['m'], mailbox_key: key}])
    return task, state, family


def acknowledge(task):
    from lib.conversations.project_messages import confirm
    from lib.swarm.run_messages import confirm_input
    confirm(task, 1)
    confirm_input(task, round_num=1)


def physical(source, send, protocol='openai'):
    """Production scope/preparation/dispatch, fake only the final send."""
    with ModelRequestBudgetScope(source) as scope:
        plan = prepare_request(scope.body, api_key='unused',
                               base_url='https://example.invalid/v1',
                               api_protocol=protocol)
        result = dispatch_model_request(plan, send, plan.body)
        scope.usage = result.usage
        return result


def test_sealed_input_not_canonical_body_is_receipt_evidence(mailbox):
    task, state, family = mailbox
    observer = receipts.receipt_admission(task, None)
    canonical = body()

    def stream(task, source):
        translated = body('Carrier removed by final provider projection')
        translated['_model_budget_admit'] = observer
        result = physical(translated, lambda wire: complete())
        task['_messageInputPhysicalIds'] = observer.matches
        return result

    receipts.stream_with_receipts(stream, task, canonical)
    assert task['_verifiedMessageInputIds'] == EMPTY
    acknowledge(task)
    assert state['acks'] == []


@pytest.mark.parametrize('protocol', ['openai', 'anthropic', 'responses'])
def test_unchanged_sealed_input_confirms_only_surviving_carrier(mailbox, protocol):
    task, state, family = mailbox
    observer = receipts.receipt_admission(task, None)

    def stream(task, source):
        result = physical(dict(source, _model_budget_admit=observer),
                          lambda wire: complete(), protocol=protocol)
        task['_messageInputPhysicalIds'] = observer.matches
        return result

    receipts.stream_with_receipts(stream, task, body())
    assert task['_verifiedMessageInputIds'][family] == ['m']
    acknowledge(task)
    assert len(state['acks']) == 1


@pytest.mark.parametrize('wait_before_first', [False, True])
def test_dispatcher_429_or_wait_revocation_blocks_next_send(mailbox, monkeypatch,
                                                          wait_before_first):
    from lib.llm_dispatch import api
    from lib.llm_dispatch.slot import Slot
    import lib.llm as llm
    task, state, family = mailbox
    observer = receipts.receipt_admission(task, None)
    slots = [Slot(key_name=f'key-{i}', api_key='unused', model='glm-4-flash',
                  capabilities={'text'}) for i in range(2)]
    sent = []

    class Dispatcher:
        picks = 0

        def pick_and_reserve(self, **kwargs):
            self.picks += 1
            if wait_before_first:
                state['revoked'] = True  # request queued after logical check
            slot = slots[self.picks - 1]
            slot.record_request()
            return slot

        def has_capable_slots(self, *args, **kwargs):
            return True


        def summarize_slots(self, *args, **kwargs):
            return 'synthetic slots'
    dispatcher = Dispatcher()
    monkeypatch.setattr(api, 'get_dispatcher', lambda: dispatcher)
    monkeypatch.setattr(api.time, 'sleep', lambda *a: None)
    monkeypatch.setenv('TOFU_CONV_STICKY_HOLD', '0')
    monkeypatch.setenv('TOFU_CACHE_SETTLE', '0')

    def send(wire):
        sent.append(wire)
        assert '_model_budget_admit' not in wire
        state['revoked'] = True
        raise RateLimitError('synthetic 429')

    monkeypatch.setattr(llm, 'stream_chat', lambda source, **kwargs: physical(source, send))

    def stream(task, source):
        return api.dispatch_stream(dict(source, _model_budget_admit=observer),
                                   max_retries=1, max_429_attempts=3)

    with pytest.raises(receipts.MessageInputScopeError):
        receipts.stream_with_receipts(stream, task, body())
    assert len(sent) == (0 if wait_before_first else 1)
    assert dispatcher.picks == (1 if wait_before_first else 2)
    assert all(slot.inflight == 0 for slot in slots)
    assert slots[dispatcher.picks - 1].consecutive_errors == 0
    assert '_verifiedMessageInputIds' not in task
    acknowledge(task)
    assert state['acks'] == []


def floor_setup(monkeypatch):
    from lib.tasks_pkg import floor_retry
    monkeypatch.setattr(floor_retry, 'floor_retry_enabled', lambda: True)
    monkeypatch.setattr(floor_retry, 'floor_retry_max', lambda: 1)
    monkeypatch.setattr(floor_retry, 'wire_prefix_stable', lambda *a, **k: True)


@pytest.mark.parametrize('cancel', [False, True])
def test_manager_floor_resend_revocation_or_stop_has_no_ack(mailbox, monkeypatch, cancel):
    from lib.tasks_pkg.manager import _stream as manager
    task, state, family = mailbox
    floor_setup(monkeypatch)
    sent = []

    completions = []
    monkeypatch.setattr(manager, 'append_event', lambda task, event:
                        completions.append(event))
    calls = []

    def send(wire):
        sent.append(wire)
        state['revoked'] = True
        return complete(FLOOR.copy())

    def dispatch(source, **kwargs):
        calls.append(1)
        if cancel and len(calls) == 2:
            raise AbortedError('Owner cancelled floor resend')
        return physical(source, send)

    monkeypatch.setattr(manager, 'dispatch_stream', dispatch)
    request = body()
    with pytest.raises(AbortedError if cancel else receipts.MessageInputScopeError) as failure:
        receipts.stream_with_receipts(manager.stream_llm_response, task, request)
    assert failure.value.usage['cache_read_tokens'] == FLOOR['cache_read_tokens']
    assert '_model_budget_admit' not in request
    assert '_activeModelRequestSpan' not in task
    assert len(calls) == 2
    assert len(sent) == 1
    assert '_verifiedMessageInputIds' not in task
    acknowledge(task)
    assert state['acks'] == []
    # The manager retains its request-complete failure boundary, not a success.
    completions = [e for e in completions if e.get('type') == 'model_request_complete']
    assert len(completions) == 1
    assert completions[0]['status'] in {'failed', 'aborted'}


def test_budget_delegate_settles_unsent_scope_denial(mailbox):
    from lib.agent_core.execution_budget import model_budget_admission
    task, state, family = mailbox
    task['config']['executionLimits'] = {
        'max_tokens': 10000, 'max_tool_calls': 0, 'deadline': time.time() + 60}
    observer = receipts.receipt_admission(task, model_budget_admission(task))
    state['revoked'] = True
    with pytest.raises(receipts.MessageInputScopeError):
        physical(dict(body(), _model_budget_admit=observer),
                 lambda wire: pytest.fail('scope denied before transport'))
    assert task['_executionBudget'].consumption_snapshot() == (0, 0, True)


def test_receipt_lease_forwards_refusal_to_the_budget_account(mailbox):
    """The composed lease must not swallow proven-zero settlement evidence.

    Chat rounds admit through the receipt observer, which wraps the budget
    lease. A 429 arrives as ``RateLimitError`` from the transport, so the
    wrapper is the only place that can carry that proof to the account.
    """
    from lib.agent_core.execution_budget import model_budget_admission
    from lib.llm.llm_errors import RateLimitError, mark_provider_refusal
    task, state, family = mailbox
    task['config']['executionLimits'] = {
        'max_tokens': 10000, 'max_tool_calls': 0, 'deadline': time.time() + 60}
    observer = receipts.receipt_admission(task, model_budget_admission(task))

    def refuse(wire):
        # The transport marks the waitable rejection; everything downstream is
        # the real scope, the real receipt wrapper and the real account.
        raise mark_provider_refusal(
            RateLimitError('rate limited', status_code=429))

    with pytest.raises(RateLimitError):
        physical(dict(body(), _model_budget_admit=observer), refuse)
    assert task.get('_executionActualsComplete') is not False
    assert task['_executionBudget'].consumption_snapshot() == (0, 0, True)


def test_floor_discard_restores_selected_physical_matches(monkeypatch):
    from lib.tasks_pkg.manager._floor_retry_stream import apply_floor_retry
    floor_setup(monkeypatch)
    selected = {'swarm': ['selected'], 'project': []}
    observer = SimpleNamespace(matches=selected)
    task = {'id': 'floor-discard', '_userId': 1, 'convId': 'synthetic'}

    def dispatch(*args, **kwargs):
        observer.matches = EMPTY
        raise RuntimeError('Synthetic transport failure')

    result = apply_floor_retry(task, body(), *complete(FLOOR.copy()),
                              model='glm-4-flash', pool_wide=False, pfx='[test]', tag='',
                              dispatch_stream_fn=dispatch, abort_check=lambda: False,
                              on_retry=None, avoid_pairs=None, on_waiting=None,
                              round_base_content='', round_base_thinking='',
                              receipt_admit=observer)
    assert result.is_verified_complete
    assert observer.matches == selected


@pytest.mark.parametrize('verified', [True, False])
def test_manager_receipt_publication_tracks_physical_return(mailbox, monkeypatch, verified):
    from lib.tasks_pkg.manager import _stream as manager
    task, state, family = mailbox
    monkeypatch.setenv('TOFU_CACHE_FLOOR_RETRY', '0')
    monkeypatch.setattr(manager, 'append_event', lambda *a: None)

    def dispatch(source, **kwargs):
        if verified:
            return physical(source, lambda wire: complete())
        # Legacy nonterminal return still exercises the physical input check.
        result = ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': 'partial'}, None, {})
        return physical(source, lambda wire: result)

    monkeypatch.setattr(manager, 'dispatch_stream', dispatch)
    request = body()
    receipts.stream_with_receipts(manager.stream_llm_response, task, request)
    assert '_model_budget_admit' not in request
    assert '_messageInputPhysicalIds' not in task
    if verified:
        assert task['_verifiedMessageInputIds'][family] == ['m']
    else:
        assert '_verifiedMessageInputIds' not in task
    acknowledge(task)
    assert len(state['acks']) == (1 if verified else 0)


def test_real_sync_stream_rechecks_after_route_selection_before_http(mailbox, monkeypatch):
    import importlib
    stream = importlib.import_module('lib.llm.stream')
    from lib.desktop import egress
    task, state, family = mailbox
    observer = receipts.receipt_admission(task, None)

    def choose_route(*args, **kwargs):
        state['revoked'] = True
        return 'direct'

    monkeypatch.setattr(egress, 'route_request', choose_route)
    monkeypatch.setattr(stream, 'get_sync_session', lambda: SimpleNamespace(
        post=lambda *a, **k: pytest.fail('Revocation must precede HTTP post')))

    def call(task, source):
        return stream.stream_chat(dict(source, _model_budget_admit=observer),
                                  api_key='unused', base_url='https://example.invalid/v1')

    with pytest.raises(receipts.MessageInputScopeError):
        receipts.stream_with_receipts(call, task, body())
    assert '_verifiedMessageInputIds' not in task
    acknowledge(task)
    assert state['acks'] == []


def test_same_prepared_plan_revalidates_before_route_resend(mailbox):
    task, state, family = mailbox
    observer = receipts.receipt_admission(task, None)
    request = dict(body(), _model_budget_admit=observer)
    sent = []
    with ModelRequestBudgetScope(request) as scope:
        plan = prepare_request(scope.body, api_key='unused',
                               base_url='https://example.invalid/v1')
        dispatch_model_request(plan, lambda: sent.append('first'))
        state['revoked'] = True
        with pytest.raises(receipts.MessageInputScopeError):
            dispatch_model_request(plan, lambda: sent.append('route-resend'))
    assert sent == ['first']
    assert observer.matches is None
    acknowledge(task)
    assert state['acks'] == []
