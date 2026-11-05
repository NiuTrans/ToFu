"""Project sender authority survives injection and every physical send gate."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from lib.conversations import project_messages as pm
from lib.llm._sse_core import prepare_request
from lib.llm.request import ModelRequestBudgetScope, dispatch_model_request
from lib.llm.stream_result import ProviderStreamResult
from lib.tasks_pkg import message_input_receipts as receipts
from tests.project_mod.test_project_messages import setup as setup

pytestmark = pytest.mark.unit


@pytest.fixture
def watchdog_message(setup, monkeypatch):
    setup.conversations[(1, 'watchdog')] = '/watchdog'
    binding = {'availability': 'available', 'conversationId': 'watchdog', 'owner': 1}

    def current_watchdog(owner, *, create):
        assert create is False
        return binding if owner == binding['owner'] else {'availability': 'missing'}

    monkeypatch.setattr('lib.router.watchdog.binding.router_watchdog', current_watchdog)
    mailbox = pm.ProjectMailbox(1, '/project', 'recipient')
    mailbox.submit('watchdog', {'content': 'Inspect the failed task; no new authority.',
                               'idempotency_key': 'watchdog-evidence'}, 'watchdog-task')
    original = deepcopy(mailbox.read()[0])
    messages = [{'role': 'user', 'content': 'Continue authorized work'}]
    pm.inject(setup.recipient, messages, 1)
    assert messages[-1]['_projectMessageIds'] == [original['id']]
    assert messages[-1]['_containsHumanSteer'] is False
    return SimpleNamespace(task=setup.recipient, mailbox=mailbox, original=original,
                           messages=messages, binding=binding)


def dispatch(witness, sent, *, protocol='openai', before_physical=None):
    """Use real request preparation; only the final provider send is recorded."""
    observer = receipts.receipt_admission(witness.task, None)

    def send(wire):
        sent.append(wire)
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': 'Read'}, 'stop', {})

    def stream(task, body):
        if before_physical is not None:
            before_physical()
        with ModelRequestBudgetScope(dict(body, _model_budget_admit=observer)) as scope:
            plan = prepare_request(scope.body, api_key='unused',
                                   base_url='https://example.invalid/v1', api_protocol=protocol)
            result = dispatch_model_request(plan, send, plan.body)
            scope.usage = result.usage
        task['_messageInputPhysicalIds'] = observer.matches
        return result

    return receipts.stream_with_receipts(stream, witness.task, {
        'model': 'glm-4-flash', 'messages': witness.messages, 'max_tokens': 32})


@pytest.mark.parametrize('protocol', ['openai', 'anthropic', 'responses'])
def test_bound_watchdog_reaches_provider_and_confirms_original(watchdog_message, protocol):
    witness, sent = watchdog_message, []
    assert dispatch(witness, sent, protocol=protocol).is_verified_complete
    assert len(sent) == 1
    assert witness.task['_verifiedMessageInputIds'] == {
        'swarm': [], 'project': [witness.original['id']]}
    assert witness.mailbox.read()[0] == witness.original
    pm.confirm(witness.task, 1)
    confirmed = witness.mailbox.read()[0]
    assert confirmed['state'] == 'input_confirmed'
    assert confirmed['decision'] is None
    assert confirmed['inputTaskId'] == witness.task['id']


@pytest.mark.parametrize('stage', ['logical', 'physical'])
@pytest.mark.parametrize('change', [
    {'availability': 'missing'}, {'conversationId': 'replacement'}, {'owner': 2},
])
def test_revoked_or_foreign_watchdog_never_sends_or_confirms(watchdog_message, stage, change):
    witness, sent = watchdog_message, []

    def revoke():
        witness.binding.update(change)

    if stage == 'logical':
        revoke()
    with pytest.raises(receipts.MessageInputScopeError) as caught:
        dispatch(witness, sent, before_physical=revoke if stage == 'physical' else None)
    assert isinstance(caught.value.__cause__, pm.ProjectMessageError)
    assert sent == []
    assert '_verifiedMessageInputIds' not in witness.task
    pm.confirm(witness.task, 1)
    assert witness.mailbox.read()[0] == witness.original


def test_watchdog_cannot_bypass_recipient_project_binding(watchdog_message, setup):
    setup.conversations[(1, 'recipient')] = '/other'
    sent = []
    with pytest.raises(receipts.MessageInputScopeError):
        dispatch(watchdog_message, sent)
    assert sent == []
    assert watchdog_message.mailbox.read()[0] == watchdog_message.original


def test_watchdog_binding_read_failure_is_not_delivery_permission(watchdog_message, monkeypatch):
    from lib.storage.errors import StorageError

    def unavailable(*args, **kwargs):
        raise StorageError('database_unavailable', 'synthetic unavailable binding')

    monkeypatch.setattr('lib.router.watchdog.binding.router_watchdog', unavailable)
    sent = []
    with pytest.raises(receipts.MessageInputScopeError) as caught:
        dispatch(watchdog_message, sent)
    assert isinstance(caught.value.__cause__, StorageError)
    assert sent == []
    assert watchdog_message.mailbox.read()[0] == watchdog_message.original
