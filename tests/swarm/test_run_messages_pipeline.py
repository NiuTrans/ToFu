"""Actual worker/root pipeline with hermetic provider and tool ports.

No network model is called. The ordinary request builder, tool pairing,
provider-consumption receipt and child isolation owners are exercised.
"""
from copy import deepcopy
import pytest
from tests.goals import test_goal_invocation_integration as fixture
from tests.swarm.test_retained_agents import RecordClient
from tests.swarm.test_swarm_invocation import agent_for, scripted_provider, reply, tool_call
from lib.swarm import run_messages

runtime = fixture.runtime
pytestmark = pytest.mark.unit


def install_mailbox(runtime, monkeypatch):
    client = RecordClient()

    monkeypatch.setattr('lib.swarm.persistence.load_session', lambda *a, **kw: {'task_id': runtime.parent['id'], 'status': 'running', 'agents': []})
    binding = {'owner': 771, 'tenant': '', 'conversationId': '', 'projectPath': '',
               'swarmKey': runtime.parent['id'], 'swarmRunId': runtime.parent['id'], 'agentId': 'child-fixture',
               'retainedAgentId': '', 'retainedScope': '', 'retainedRunId': ''}
    actual = run_messages.RunMessageRepository
    repository = actual(binding, client=lambda **kw: client)
    monkeypatch.setattr(run_messages, 'RunMessageRepository', lambda b: actual(b, client=lambda **kw: client))
    import lib.tasks_pkg.child_invocation as factory
    original = factory.create_child_invocation
    def create(*args, **kw):
        child = original(*args, **kw)
        # This is the same declared carrier field production SubAgent supplies;
        # the test itself remains storage-free outside its isolated mailbox.
        child['_swarmMessageBinding'] = deepcopy(binding)
        return child
    monkeypatch.setattr(factory, 'create_child_invocation', create)

    import lib.tasks_pkg.manager._stream as stream
    original_dispatch = stream.dispatch_stream
    def prepared_dispatch(body, **kwargs):
        # The hermetic transport must exercise the production attempt port.
        # Private observer callbacks are not wire/model input or snapshots.
        admission = body.get('_model_budget_admit')
        lease = admission(input_tokens=0, max_output_tokens=1) if admission else None
        if lease is not None:
            lease.prepare_input(body)
            lease.mark_dispatched()
        wire_input = {key: value for key, value in body.items() if key != '_model_budget_admit'}
        try:
            return original_dispatch(wire_input, **kwargs)
        finally:
            if lease is not None:
                lease.settle(None)
    monkeypatch.setattr(stream, 'dispatch_stream', prepared_dispatch)
    return repository


def test_existing_child_receives_nonhuman_context_and_preserves_tools(runtime, monkeypatch):
    repo = install_mailbox(runtime, monkeypatch)
    message = repo.submit(content='Check the alpha evidence', kind='reference', key='first', sender_task_id=runtime.parent['id'])
    result = agent_for(runtime).run()
    assert result.status == 'completed', result.error_message
    assert len(runtime.calls) == 2 and len(runtime.children) == 1
    carrier = next(m for m in runtime.calls[0]['messages'] if m.get('_swarmMessageIds'))
    assert 'Check the alpha evidence' in carrier['content']
    assert carrier['_containsHumanSteer'] is False
    assert runtime.calls[0]['tools'] == [fixture.TOOL]
    receipt = repo.load()[0]['messages'][0]
    assert receipt['id'] == message['id'] and receipt['state'] == 'input_confirmed'
    assert receipt['inputTaskId'] == runtime.children[0]['id']
    assert receipt['inputRound'] == 1
    assert sum(bool(m.get('_swarmMessageIds')) for m in runtime.calls[1]['messages']) == 1
    assert not runtime.parent.get('_swarmMessageBinding')


def test_message_committed_during_tool_enters_next_closed_boundary(runtime, monkeypatch):
    repo = install_mailbox(runtime, monkeypatch)
    original = runtime.parent['_tool_env'].resolve(fixture.TOOL['function']['name'])
    from types import SimpleNamespace
    def handler(*args, **kwargs):
        repo.submit(content='Additional result interpretation', kind='steer', key='during-tool', sender_task_id=runtime.parent['id'])
        assert repo.load()[0]['messages'][0]['state'] == 'submitted'
        return original(*args, **kwargs)
    runtime.parent['_tool_env'] = SimpleNamespace(resolve=lambda name: handler)
    result = agent_for(runtime).run()
    assert result.status == 'completed', result.error_message
    assert not any(m.get('_swarmMessageIds') for m in runtime.calls[0]['messages'])
    second = runtime.calls[1]['messages']
    index = next(i for i, m in enumerate(second) if m.get('_swarmMessageIds'))
    assert any(m['role'] == 'tool' for m in second[:index])
    assert 'Additional result interpretation' in second[index]['content']
    assert repo.load()[0]['messages'][0]['inputRound'] == 2
    assert len(runtime.children) == 1


def test_provider_failure_does_not_ack_input(runtime, monkeypatch):
    scripted_provider(monkeypatch, runtime, [RuntimeError('HTTP 400: synthetic model rejected')])
    repo = install_mailbox(runtime, monkeypatch)
    repo.submit(content='Keep evidence', kind='reference', key='fail', sender_task_id=runtime.parent['id'])
    result = agent_for(runtime).run()
    assert result.status == 'failed'
    assert repo.load()[0]['messages'][0]['state'] == 'submitted'


def test_cancellation_does_not_ack_or_create_a_second_child(runtime, monkeypatch):
    from lib.llm_errors import AbortedError
    scripted_provider(monkeypatch, runtime, [AbortedError('operator Stop')])
    repo = install_mailbox(runtime, monkeypatch)
    repo.submit(content='Keep evidence', kind='steer', key='abort', sender_task_id=runtime.parent['id'])
    result = agent_for(runtime).run()
    assert result.status == 'cancelled'
    assert repo.load()[0]['messages'][0]['state'] == 'submitted'
    assert len(runtime.children) == 1
