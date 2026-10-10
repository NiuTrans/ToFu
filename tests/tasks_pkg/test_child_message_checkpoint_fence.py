"""Child projections keep context without transferring another mailbox's rights."""
from __future__ import annotations

import copy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lib.llm.stream_result import ProviderStreamResult
from lib.swarm.run_messages import (
    RunMessageRepository, binding_for, confirm_input, inject_messages,
)
from lib.tasks_pkg.child_invocation import build_child_messages, create_child_invocation
from lib.tasks_pkg.message_input_receipts import remember, stream_with_receipts
from tests.swarm.test_retained_agents import RecordClient

pytestmark = pytest.mark.unit


@pytest.fixture
def run_context(monkeypatch):
    client = RecordClient()
    monkeypatch.setattr('lib.storage.get_storage_client', lambda write=False: client)

    # Registration is a separate durable admission port; mailbox CAS stays real.
    monkeypatch.setattr('lib.tasks_pkg.invocation_registration.register_child_invocation',
                        lambda *a, **kw: None)
    monkeypatch.setattr('lib.swarm.persistence.load_session', lambda *a, **kw: {
        'task_id': 'checkpoint-parent', 'status': 'running', 'agents': []})
    monkeypatch.setattr('lib.conversations.repository.ConversationRepository.get',
                        lambda *a, **kw: SimpleNamespace(metadata={
                            'settings': {'projectPath': '/project'}}))
    parent = {'id': 'checkpoint-parent', 'convId': 'checkpoint-conv', '_userId': 71,
              'config': {'projectPath': '/project', 'memoryEnabled': False}}
    spec = SimpleNamespace(id='worker', retained_agent_id='', retained_run_id='',
                           retained_scope='')
    binding = binding_for(parent, spec, 'checkpoint-conv')
    return SimpleNamespace(parent=parent, binding=binding,
                           repo=RunMessageRepository(binding))


def _create(parent, **inputs):
    return create_child_invocation(
        parent, model='fixture-model', tools=[], objective='Child objective',
        event_sink=lambda event: None, abort_check=lambda: False, **inputs)


def _verified_stream(task, body, **kwargs):
    return ProviderStreamResult.from_legacy(
        {'role': 'assistant', 'content': 'Received context'}, 'stop', None)


@pytest.mark.parametrize('flag', ['_swarmMessageIds', '_projectMessageIds'])
def test_new_bootstrap_does_not_transfer_old_mailbox_carrier(flag):
    checkpoint = [{'role': 'user', 'content': 'Original task'},
                  {'role': 'user', 'content': 'Old scope steer', flag: ['previous']},
                  {'role': 'assistant', 'content': 'Recorded findings'}]
    before = copy.deepcopy(checkpoint)
    result = build_child_messages(objective='New objective', checkpoint_messages=checkpoint)
    assert not any(message.get(flag) for message in result)
    assert not any(message.get('content') == 'Old scope steer' for message in result)
    assert any(message.get('content') == 'Recorded findings' for message in result)
    assert checkpoint == before  # durable evidence is not modified


def test_conversation_child_keeps_parent_context_without_receipt_or_ack_authority(monkeypatch):
    from lib.conversations import project_messages
    from lib.tasks_pkg.manager import discard_task

    parent = {'id': 'conversation-parent', 'convId': 'parent-conv', '_userId': 71,
              '_transientRuntime': True, 'config': {'memoryEnabled': False}}
    messages = [
        {'role': 'user', 'content': 'Parent project evidence',
         '_projectMessageIds': ['parent-message'], '_projectMailbox': 'parent-mailbox',
         '_contextInjectionId': 'inbox_delivery', '_containsHumanSteer': False},
        {'role': 'user', 'content': 'Parent run evidence',
         '_swarmMessageIds': ['parent-run-message'], '_swarmMessageMailbox': 'parent-run'},
        {'role': 'user', 'content': [{'type': 'text', 'text': 'Media context'},
                                    {'type': 'image_url', 'image_url': {'url': 'fixture'}}]},
        {'role': 'assistant', 'tool_calls': [{'id': 'probe', 'type': 'function',
                                            'function': {'name': 'inspect', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'probe', 'content': 'Probe evidence'},
    ]
    before = copy.deepcopy(messages)
    ack = Mock()
    monkeypatch.setattr(project_messages.ProjectMailbox, 'confirm', ack)
    monkeypatch.setattr(project_messages, 'identity',
                        lambda task: pytest.fail('Child attempted parent project authority'))
    child = _create(parent, conversation_messages=messages)
    try:
        expected = copy.deepcopy(messages)
        for message in expected:
            for key in ('_projectMessageIds', '_projectMailbox',
                        '_swarmMessageIds', '_swarmMessageMailbox'):
                message.pop(key, None)
        assert child['messages'] == expected
        remember(child, child['messages'])
        result = stream_with_receipts(_verified_stream, child, {'messages': child['messages']})
        assert result.is_verified_complete
        assert child['_verifiedMessageInputIds'] == {'swarm': [], 'project': []}
        project_messages.confirm(child, 0)
        ack.assert_not_called()
        assert messages == before
    finally:
        discard_task(child['id'])


@pytest.mark.parametrize('confirmed', [False, True], ids=['submitted', 'input-confirmed'])
def test_same_run_checkpoint_keeps_exact_carrier_and_receipt_authority(run_context, confirmed):
    from lib.tasks_pkg.manager import discard_task

    ctx = run_context
    receipt = ctx.repo.submit(content='Evidence needed after restart', kind='reference',
                              key='context', sender_task_id=ctx.parent['id'])
    prior = {'id': 'prior-child', 'config': {}, '_swarmMessageBinding': ctx.binding}
    checkpoint = [{'role': 'user', 'content': 'Child objective'}]
    inject_messages(prior, checkpoint, 0)
    stream_with_receipts(_verified_stream, prior, {'messages': checkpoint})
    if confirmed:
        confirm_input(prior, round_num=0)
    before = copy.deepcopy(checkpoint)
    carrier = copy.deepcopy(checkpoint[-1])
    rebuilt = build_child_messages(objective='Child objective', checkpoint_messages=checkpoint,
                                   swarm_message_binding=ctx.binding)
    child = _create(ctx.parent, checkpoint_messages=rebuilt, swarm_message_binding=ctx.binding)
    try:
        assert child['_swarmMessageBinding'] == ctx.binding
        assert child['_swarmMessageBinding'] is not ctx.binding
        assert carrier in child['messages']
        before_injection = copy.deepcopy(child['messages'])
        inject_messages(child, child['messages'], 1)
        assert child['messages'] == before_injection  # confirmed rows are not reinjected
        result = stream_with_receipts(_verified_stream, child, {'messages': child['messages']})
        assert result.is_verified_complete
        assert child['_verifiedMessageInputIds']['swarm'] == [receipt['id']]
        confirm_input(child, round_num=1)
        row = ctx.repo.load()[0]['messages'][0]
        assert row['state'] == 'input_confirmed'
        assert row['inputTaskId'] == ('prior-child' if confirmed else child['id'])
        assert checkpoint == before
    finally:
        discard_task(child['id'])


@pytest.mark.parametrize('changed', ['owner', 'tenant', 'conversationId', 'swarmRunId', 'agentId'])
def test_cross_assignment_checkpoint_drops_old_active_carrier(run_context, changed):
    ctx = run_context
    ctx.repo.submit(content='Old assignment steer', kind='steer', key='old',
                    sender_task_id=ctx.parent['id'])
    checkpoint = [{'role': 'user', 'content': 'Child objective'}]
    inject_messages({'id': 'prior-child', 'config': {}, '_swarmMessageBinding': ctx.binding},
                    checkpoint, 0)
    checkpoint.append({'role': 'assistant', 'content': 'Recorded findings'})
    before = copy.deepcopy(checkpoint)
    new_binding = {**ctx.binding, changed: 72 if changed == 'owner' else 'replacement'}
    rebuilt = build_child_messages(objective='New assignment', checkpoint_messages=checkpoint,
                                   swarm_message_binding=new_binding)
    assert not any(message.get('_swarmMessageIds') for message in rebuilt)
    assert not any('Old assignment steer' in str(message.get('content')) for message in rebuilt)
    assert any(message.get('content') == 'Recorded findings' for message in rebuilt)
    assert checkpoint == before


def test_same_run_binding_does_not_authorize_parent_project_carrier(run_context):
    checkpoint = [{'role': 'user', 'content': 'Child objective'},
                  {'role': 'user', 'content': 'Parent active steer',
                   '_projectMessageIds': ['parent-id'], '_projectMailbox': 'parent-mailbox'}]
    rebuilt = build_child_messages(objective='Child objective', checkpoint_messages=checkpoint,
                                   swarm_message_binding=run_context.binding)
    assert not any(message.get('_projectMessageIds') for message in rebuilt)
    assert not any(message.get('content') == 'Parent active steer' for message in rebuilt)


def test_retired_same_mailbox_checkpoint_cannot_dispatch(run_context, monkeypatch):
    ctx = run_context
    ctx.repo.submit(content='Retired run evidence', kind='reference', key='old',
                    sender_task_id=ctx.parent['id'])
    prior = {'id': 'prior-child', 'config': {}, '_swarmMessageBinding': ctx.binding}
    checkpoint = [{'role': 'user', 'content': 'Child objective'}]
    inject_messages(prior, checkpoint, 0)
    rebuilt = build_child_messages(objective='Child objective', checkpoint_messages=checkpoint,
                                   swarm_message_binding=ctx.binding)
    child = {'id': 'recovered-child', 'config': {}, '_swarmMessageBinding': ctx.binding}
    remember(child, rebuilt)
    monkeypatch.setattr('lib.swarm.persistence.load_session', lambda *a, **kw: {
        'task_id': ctx.parent['id'], 'status': 'terminated', 'agents': []})
    provider = Mock()
    with pytest.raises(ValueError, match='ended or superseded'):
        stream_with_receipts(provider, child, {'messages': rebuilt})
    provider.assert_not_called()
    assert '_verifiedMessageInputIds' not in child
    assert ctx.repo.load()[0]['messages'][0]['state'] == 'submitted'


def test_subagent_recovery_passes_binding_before_checkpoint_projection(run_context, monkeypatch):
    from lib.swarm.agent import SubAgent
    from lib.swarm.protocol import SubTaskSpec
    from lib.tasks_pkg.manager import discard_task
    ctx = run_context
    item = ctx.repo.submit(content='Confirmed production recovery context', kind='reference',
                           key='production', sender_task_id=ctx.parent['id'])
    prior = {'id': 'prior-child', 'config': {}, '_swarmMessageBinding': ctx.binding}
    checkpoint = [{'role': 'user', 'content': 'Child objective'}]
    inject_messages(prior, checkpoint, 0)
    remember(prior, checkpoint)
    stream_with_receipts(_verified_stream, prior, {'messages': checkpoint})
    confirm_input(prior, round_num=0)
    worker = SubAgent(SubTaskSpec(id='worker', role='general', objective='Recover'),
                      parent_task=ctx.parent, all_tools=[], model='fixture-model')
    worker.swarm_key = 'checkpoint-conv'
    worker.restore_checkpoint(checkpoint, 1)
    assert any(item['id'] in message.get('_swarmMessageIds', []) for message in worker.messages)
    observed = []
    def execute(child, **kwargs):
        observed.append(child)
        assert child['_swarmMessageBinding'] == ctx.binding
        remember(child, child['messages'])
        result = stream_with_receipts(_verified_stream, child, {'messages': child['messages']})
        assert result.is_verified_complete
        assert child['_verifiedMessageInputIds']['swarm'] == [item['id']]
        child['content'] = 'Recovered answer'
    monkeypatch.setattr('lib.tasks_pkg.child_invocation.run_child_invocation', execute)
    monkeypatch.setattr('lib.turn_verdict.derive_task_verdict', lambda child:
                        SimpleNamespace(status=SimpleNamespace(value='completed')))
    for name in ('_presence_announce', '_presence_leave', '_checkpoint'):
        if hasattr(worker, name):
            monkeypatch.setattr(worker, name, lambda *a, **kw: None)
    try:
        outcome = worker.run()
        assert outcome.status == 'completed', outcome.error_message
        assert len(observed) == 1
    finally:
        for child in observed:
            discard_task(child['id'])
