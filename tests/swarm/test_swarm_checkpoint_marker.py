"""A child's pre-request write-ahead is a constant-size marker over its last write.

The real child pipeline writes through a real SQL authority. Before every paid
call the authority must durably say "a paid request may be in flight"; when the
transcript is unchanged since the last acknowledged write that record is a compact
``swarm.agent.checkpoint.mark`` instead of a transcript rewrite, and every read
(restart recovery included) sees exactly what a full save would have written.
"""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from lib.swarm import persistence
from tests.goals.test_goal_invocation_integration import TOOL, runtime as runtime  # noqa: F401
from tests.storage.test_storage_authority_sql_contract import storage as storage  # noqa: F401
from tests.swarm.test_swarm_invocation import agent_for, reply, tool_call

pytestmark = pytest.mark.unit

SAVE, APPEND, MARK = 'swarm.agent.save', 'swarm.agent.append', 'swarm.agent.checkpoint.mark'
SWARM_KEY = 'marker-wave'
# contracts/tofudb_ir_v1.json swarm checkpointMarkerResultFields.
MARKER_FIELDS = {'actuals_complete', 'checkpoint_phase', 'completion_tokens', 'cost_usd',
                 'elapsed_seconds', 'prompt_tokens', 'rounds_used', 'total_tokens'}


@pytest.fixture
def durable(runtime, storage, monkeypatch):
    client, writes = storage.client, []

    class Recording:
        def command(self, operation, payload, command_id):
            writes.append((operation, json.loads(json.dumps(payload))))
            return client.command(operation, payload, command_id)

        def query(self, operation, payload, *args, **kwargs):
            return client.query(operation, payload, *args, **kwargs)

    monkeypatch.setattr(persistence, '_storage', lambda **kw: Recording())
    runtime.parent['_transientRuntime'] = False
    client.command('swarm.session.save', {
        'swarm_key': SWARM_KEY, 'conv_id': '', 'task_id': runtime.parent['id'],
        'status': 'running', 'specs': [], 'config': {'user_id': 771}, 'now_ms': 1,
    }, 'marker-spec-session')
    return SimpleNamespace(client=client, writes=writes)


def durable_agent(runtime, **kwargs):
    agent = agent_for(runtime, **kwargs)
    agent.swarm_key, agent.swarm_run_id = SWARM_KEY, runtime.parent['id']
    # Exact-run mailbox input is orthogonal to checkpoint durability.
    agent._message_binding = lambda: None
    return agent


def provider(monkeypatch, runtime, responses, before_call=None):
    import lib.tasks_pkg.manager._stream as stream

    def dispatch(body, **kwargs):
        index = len(runtime.calls)
        assert index < len(responses), 'Unexpected extra model request'
        if before_call is not None:
            before_call(index)
        runtime.calls.append(deepcopy(body))
        if responses[index].message.get('content'):
            kwargs['on_content'](responses[index].message['content'])
        return responses[index]
    monkeypatch.setattr(stream, 'dispatch_stream', dispatch)


def durable_row(agent_id):
    return persistence.load_session(SWARM_KEY)['agents'], persistence.load_session(
        SWARM_KEY, agent_id=agent_id)['agents'][0]


def phase_of(write):
    operation, payload = write
    return operation, payload['result'].get('checkpoint_phase')


def test_unchanged_transcript_write_ahead_is_a_constant_size_marker(durable, runtime, monkeypatch):
    provider(monkeypatch, runtime, [reply('Reading', [tool_call('call-1')]),
                                    reply('Reading more', [tool_call('call-2')]),
                                    reply('Verified answer')])
    agent = durable_agent(runtime)
    result = agent.run()
    assert result.status == 'completed', result.error_message
    writes = durable.writes
    pre_request = [write for write in writes if phase_of(write)[1] == 'model_in_flight']
    # One write-ahead per paid call; the first write of a run is always a full save.
    assert [operation for operation, _ in pre_request] == [SAVE, MARK, MARK]
    assert writes[0][0] == SAVE
    # Later transcript writes only grow it (tests/swarm/test_swarm_checkpoint_append.py).
    assert all(operation == APPEND for operation, payload in writes
               if payload['result'].get('checkpoint_phase') in ('tools_in_flight', 'ready'))
    for operation, payload in pre_request[1:]:
        assert set(payload) == {'swarm_key', 'agent_id', 'rounds_used', 'result', 'now_ms'}
        assert set(payload['result']) == MARKER_FIELDS
        assert payload['result']['actuals_complete'] is False
        assert len(json.dumps(payload)) < 1024
    final_operation, final = writes[-1]
    assert final_operation == APPEND and final['status'] == 'completed'
    assert len(json.dumps(final)) > len(json.dumps(pre_request[-1][1]))
    agents, _ = durable_row(agent.spec.id)
    assert agents[0]['result']['final_answer'] == 'Verified answer'
    assert agents[0]['result']['actuals_complete'] is True


def test_crash_after_marker_restores_incomplete_usage_and_finite_budget_refuses(
        durable, runtime, monkeypatch):
    crash = {}
    agent = durable_agent(runtime)

    def die_during_second_call(index):
        if index == 1:
            # Process loss here keeps exactly the acknowledged writes.
            crash['last_write'] = durable.writes[-1]
            crash['agents'], crash['state'] = durable_row(agent.spec.id)
    provider(monkeypatch, runtime, [reply('Reading', [tool_call('call-1')]),
                                    reply('Verified answer')], die_during_second_call)
    assert agent.run().status == 'completed'
    operation, payload = crash['last_write']
    assert operation == MARK and 'messages' not in payload
    row = crash['agents'][0]
    assert row['result']['checkpoint_phase'] == 'model_in_flight'
    assert row['result']['actuals_complete'] is False
    assert row['result']['total_tokens'] == 23
    assert row['rounds_used'] == crash['state']['rounds_used'] == 1
    assert [m['tool_call_id'] for m in row['messages'] if m.get('role') == 'tool'] == ['call-1']

    # Restart resumes from this row (master.rehydrate -> restore_checkpoint)
    # under a session whose shared allowance is finite.
    runtime.parent['config']['executionLimits'] = {
        'max_tokens': 1000, 'max_tool_calls': 20, 'deadline': None}
    resumed = durable_agent(runtime)
    resumed.restore_checkpoint(row['messages'], row['rounds_used'], row['result'])
    assert resumed.result.actuals_complete is False
    paid_calls = len(runtime.calls)
    outcome = resumed.run()
    assert len(runtime.calls) == paid_calls
    assert outcome.status == 'failed'
    assert 'execution_token_usage_unknown' in outcome.error_message
    assert outcome.total_tokens == 23 and outcome.actuals_complete is False


def test_marker_never_outlives_later_writes_and_grown_transcripts_append(
        durable, runtime, monkeypatch):
    import lib.tasks_pkg.orchestrator._root_agent_loop as loop

    def hygiene(task, messages, *, round_num, **_kwargs):
        if round_num == 1:
            messages.append({'role': 'user', 'content': 'Injected before the second request'})
    monkeypatch.setattr(loop, 'run_round_message_hygiene', hygiene)
    agent = durable_agent(runtime)
    during_tools = []
    original = runtime.parent['_tool_env']

    def resolve(name):
        handler = original.resolve(name)
        def observed(*args):
            during_tools.append(durable_row(agent.spec.id)[0][0])
            return handler(*args)
        return observed
    runtime.parent['_tool_env'] = SimpleNamespace(resolve=resolve)
    before_calls = []
    provider(monkeypatch, runtime, [reply('Step 1', [tool_call('call-1')]),
                                    reply('Step 2', [tool_call('call-2')]),
                                    reply('Step 3', [tool_call('call-3')]),
                                    reply('Verified answer')],
             lambda index: before_calls.append(phase_of(durable.writes[-1])))
    result = agent.run()
    assert result.status == 'completed', result.error_message
    # The injected message grew the transcript: that write-ahead appends it.
    assert before_calls == [(SAVE, 'model_in_flight'), (APPEND, 'model_in_flight'),
                            (MARK, 'model_in_flight'), (MARK, 'model_in_flight')]
    injected = [write for write in durable.writes if write[0] == APPEND
                and phase_of(write)[1] == 'model_in_flight'][0][1]
    assert [message['content'] for message in injected['messages']] == [
        'Injected before the second request']
    # Each tool-intent write supersedes the preceding marker.
    assert [(row['result']['checkpoint_phase'], row['result']['actuals_complete'],
             row['rounds_used']) for row in during_tools] == [
        ('tools_in_flight', True, 0), ('tools_in_flight', True, 1), ('tools_in_flight', True, 2)]
    assert during_tools[2]['messages'][-1]['tool_calls'][0]['id'] == 'call-3'
    final = durable_row(agent.spec.id)[0][0]
    assert final['status'] == 'completed' and final['result']['actuals_complete'] is True
    assert final['result']['checkpoint_phase'] != 'model_in_flight'
