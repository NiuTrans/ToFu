"""Round checkpoints send only new messages; every read equals a full save.

A child's transcript-changing checkpoints append the messages added since the
writer's last acknowledged write (``swarm.agent.append``), keyed by that
write's checkpoint id and message count. The authority applies an append only
on that exact base, so a rewritten prefix, another writer or an authority
without the operation always lands on a full ``swarm.agent.save``, and restart
recovery reads exactly the transcript a full save would have stored.
"""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from lib.storage.errors import StorageError
from lib.swarm import persistence
from tests.goals.test_goal_invocation_integration import TOOL, runtime as runtime  # noqa: F401
from tests.storage.test_storage_authority_sql_contract import storage as storage  # noqa: F401
from tests.swarm.test_swarm_checkpoint_marker import provider
from tests.swarm.test_swarm_invocation import agent_for, reply, tool_call

pytestmark = pytest.mark.unit
pytest_plugins = ('tests.support.semantic_authorities',)

SAVE, APPEND, MARK = 'swarm.agent.save', 'swarm.agent.append', 'swarm.agent.checkpoint.mark'
SWARM_KEY = 'append-wave'


def _plain(value):
    return json.loads(json.dumps(value))


def _without_event_ids(value):
    if isinstance(value, dict):
        return {key: _without_event_ids(item) for key, item in value.items() if key != 'eventId'}
    if isinstance(value, list):
        return [_without_event_ids(item) for item in value]
    return value


@pytest.fixture
def authority(runtime, storage, monkeypatch):
    """The real SQL authority behind a recorder; ``mode`` emulates older ones."""
    client, writes = storage.client, []
    state = SimpleNamespace(mode='current')

    class Recording:
        def command(self, operation, payload, command_id):
            writes.append((operation, _plain(payload)))
            if operation == APPEND and state.mode == 'no_append_operation':
                raise StorageError('unknown_operation', 'swarm.agent.append')
            response = client.command(operation, payload, command_id)
            if operation == SAVE and state.mode == 'no_append_base':
                response = {'saved': True}
            return response

        def query(self, operation, payload, *args, **kwargs):
            return client.query(operation, payload, *args, **kwargs)

    monkeypatch.setattr(persistence, '_storage', lambda **kw: Recording())
    runtime.parent['_transientRuntime'] = False
    client.command('swarm.session.save', {
        'swarm_key': SWARM_KEY, 'conv_id': '', 'task_id': runtime.parent['id'],
        'status': 'running', 'specs': [], 'config': {'user_id': 771}, 'now_ms': 1,
    }, 'append-spec-session')
    return SimpleNamespace(client=client, writes=writes, state=state)


def durable_agent(runtime, **kwargs):
    agent = agent_for(runtime, **kwargs)
    agent.swarm_key, agent.swarm_run_id = SWARM_KEY, runtime.parent['id']
    agent._message_binding = lambda: None
    return agent


def durable_messages(agent_id):
    return persistence.load_session(SWARM_KEY)['agents'][0]['messages'] if agent_id else None


def transcript_writes(writes):
    return [(operation, payload) for operation, payload in writes if operation != MARK]


def test_round_checkpoints_append_only_new_messages_and_restore_like_a_full_save(
        authority, runtime, monkeypatch):
    agent = durable_agent(runtime)
    before_calls = []
    provider(monkeypatch, runtime, [reply(f'Step {index}', [tool_call(f'call-{index}')])
                                    for index in range(4)] + [reply('Verified answer')],
             lambda index: before_calls.append((
                 durable_messages(agent.spec.id), _plain(agent.messages))))
    result = agent.run()
    assert result.status == 'completed', result.error_message

    row = persistence.load_session(SWARM_KEY)['agents'][0]
    assert row['messages'] == _plain(runtime.children[-1]['messages'])
    writes = transcript_writes(authority.writes)
    assert writes[0][0] == SAVE and writes[0][1]['checkpoint_id']
    appends = [payload for operation, payload in writes[1:]]
    assert {operation for operation, _ in writes[1:]} == {APPEND} and len(appends) >= 9
    # Each append names the previous write and carries only what it added.
    durable_count = len(writes[0][1]['messages'])
    assert writes[0][1]['messages'] == row['messages'][:durable_count]
    previous_id = writes[0][1]['checkpoint_id']
    for payload in appends:
        assert payload['base_checkpoint_id'] == previous_id
        assert payload['base_message_count'] == durable_count
        added = len(payload['messages'])
        assert payload['messages'] == row['messages'][durable_count:durable_count + added]
        durable_count += added
        previous_id = payload['checkpoint_id']
    assert durable_count == len(row['messages'])
    assert appends[-1]['status'] == 'completed'
    assert appends[-1]['result']['final_answer'] == 'Verified answer'
    # Before every paid request the durable transcript is the checkpointed one.
    assert len(before_calls) == 5
    assert all(durable == live for durable, live in before_calls)
    # A full save of the same transcript restores the identical worker context.
    persistence.save_agent(SWARM_KEY, 'reference', role=row['role'], objective=row['objective'],
                           status=row['status'], messages=row['messages'], result=row['result'],
                           rounds_used=row['rounds_used'])
    reference = next(agent for agent in persistence.load_session(SWARM_KEY)['agents']
                     if agent['agent_id'] == 'reference')
    assert reference['messages'] == row['messages'] and reference['result'] == row['result']
    resumed, full = durable_agent(runtime), durable_agent(runtime)
    resumed.restore_checkpoint(row['messages'], row['rounds_used'], row['result'])
    full.restore_checkpoint(reference['messages'], reference['rounds_used'], reference['result'])
    # Context composition mints a fresh eventId per restore; nothing else differs.
    assert _without_event_ids(resumed.messages) == _without_event_ids(full.messages)
    assert (resumed.result.to_dict(), resumed._round_offset) == (
        full.result.to_dict(), full._round_offset)


def checkpoint(agent, messages, phase='ready'):
    agent.messages = messages
    agent.result.checkpoint_phase = phase
    agent.result.rounds_used += 1
    assert agent._checkpoint() is True
    return durable_messages(agent.spec.id)


def test_compacted_or_mutated_prefix_saves_in_full_then_appends_resume(authority, runtime):
    agent = durable_agent(runtime)
    agent.result.status = 'running'
    live = [{'role': 'user', 'content': f'turn {index}'} for index in range(6)]
    assert checkpoint(agent, live) == live
    live.append({'role': 'assistant', 'content': 'grown'})
    assert checkpoint(agent, live) == live
    # A compaction rewrite replaces the prefix with a summary.
    live = [live[0], {'role': 'user', 'content': '[summary of turns 1-5]'}, live[-1]]
    assert checkpoint(agent, live) == live
    live.append({'role': 'assistant', 'content': 'after compaction'})
    assert checkpoint(agent, live) == live
    # An in-place edit of an already durable message is a rewrite too.
    live[1]['content'] = '[summary, revised]'
    assert checkpoint(agent, live) == live
    live.append({'role': 'tool', 'tool_call_id': 'call-9', 'content': 'tail'})
    assert checkpoint(agent, live) == live
    assert [operation for operation, _ in transcript_writes(authority.writes)] == [
        SAVE, APPEND, SAVE, APPEND, SAVE, APPEND]
    assert [len(payload['messages']) for _, payload in authority.writes] == [6, 1, 3, 1, 4, 1]


def test_another_writer_breaks_the_append_base_and_the_next_write_recovers(authority, runtime):
    agent = durable_agent(runtime)
    agent.result.status = 'running'
    live = [{'role': 'user', 'content': 'start'}]
    checkpoint(agent, live)
    live.append({'role': 'assistant', 'content': 'one'})
    checkpoint(agent, live)
    # Rehydration (or a duplicate worker) rewrites the row without this base,
    # even with an identical message count.
    persistence.save_agent(SWARM_KEY, agent.spec.id, role='general', objective='other writer',
                           status='running', messages=[{'role': 'user', 'content': 'x'},
                                                       {'role': 'user', 'content': 'y'}])
    live.append({'role': 'tool', 'tool_call_id': 'call-1', 'content': 'two'})
    assert checkpoint(agent, live) == live
    live.append({'role': 'assistant', 'content': 'three'})
    assert checkpoint(agent, live) == live
    operations = [operation for operation, _ in transcript_writes(authority.writes)]
    assert operations == [SAVE, APPEND, SAVE, APPEND, SAVE, APPEND]
    refused = [payload for operation, payload in authority.writes if operation == APPEND][1]
    assert refused['base_message_count'] == 2


@pytest.mark.parametrize('semantic_authority', ['sqlite', 'native', 'postgres'], indirect=True)
def test_every_authority_reads_appended_checkpoints_exactly_as_full_saves(
        semantic_authority, runtime, monkeypatch):
    factory, owner, _ = semantic_authority
    operations = []

    class Counting:
        def __init__(self, client):
            self.client = client

        def command(self, operation, payload, command_id, *args, **kwargs):
            operations.append(operation)
            return self.client.command(operation, payload, command_id, *args, **kwargs)

        def query(self, *args, **kwargs):
            return self.client.query(*args, **kwargs)

    monkeypatch.setattr(persistence, '_storage', lambda **kw: Counting(factory(**kw)))
    runtime.parent['_transientRuntime'] = False
    persistence.save_session(SWARM_KEY, conv_id='', task_id=runtime.parent['id'], specs=[],
                             config={'user_id': owner}, required=True)
    agent = durable_agent(runtime)
    agent.result.status = 'running'
    live = [{'role': 'system', 'content': 'policy'},
            {'role': 'user', 'content': '目标 "quoted"\n', 'cost': 0.5}]
    assert checkpoint(agent, live) == live
    for index in range(3):
        live.append({'role': 'assistant', 'content': f'step {index}', 'tool_calls': [
            {'id': f'call-{index}', 'type': 'function',
             'function': {'name': 'read_files', 'arguments': '{"path": "a"}'}}]})
        assert checkpoint(agent, live, 'tools_in_flight') == live
        live.append({'role': 'tool', 'tool_call_id': f'call-{index}',
                     'content': f'{index}' * 70_000})
        assert checkpoint(agent, live) == live
    live[2:6] = [{'role': 'user', 'content': '[compacted rounds 0-1]'}]
    assert checkpoint(agent, live) == live
    live.append({'role': 'assistant', 'content': 'done'})
    agent.result.status, agent.result.final_answer = 'completed', 'done'
    assert agent._checkpoint(final=True) is True
    writes = [operation for operation in operations if operation in (SAVE, APPEND)]
    assert writes == [SAVE] + [APPEND] * 6 + [SAVE, APPEND]

    row = persistence.load_session(SWARM_KEY)['agents'][0]
    persistence.save_agent(SWARM_KEY, 'reference', role=row['role'], objective=row['objective'],
                           status=row['status'], messages=live, result=row['result'],
                           rounds_used=row['rounds_used'], required=True,
                           task=agent.context_task)
    reference = next(agent for agent in persistence.load_session(SWARM_KEY)['agents']
                     if agent['agent_id'] == 'reference')
    for item in (row, reference):
        item.pop('agent_id'), item.pop('updated_at')
    assert row == reference and row['messages'] == _plain(live)
    assert (row['status'], row['result']['final_answer']) == ('completed', 'done')


@pytest.mark.parametrize('mode', ['no_append_base', 'no_append_operation'])
def test_authority_without_appends_keeps_exact_full_saves(authority, runtime, mode):
    authority.state.mode = mode
    agent = durable_agent(runtime)
    agent.result.status = 'running'
    live = [{'role': 'user', 'content': 'start'}]
    for index in range(3):
        assert checkpoint(agent, live) == live
        live = live + [{'role': 'assistant', 'content': f'step {index}'}]
    operations = [operation for operation, _ in transcript_writes(authority.writes)]
    if mode == 'no_append_base':
        # An authority that keeps no append base is never sent an append.
        assert operations == [SAVE, SAVE, SAVE]
    else:
        # One that lost the operation refuses it; the same checkpoint saves in full.
        assert operations == [SAVE, APPEND, SAVE, APPEND, SAVE]
    final = deepcopy(live[:-1])
    assert durable_messages(agent.spec.id) == final
