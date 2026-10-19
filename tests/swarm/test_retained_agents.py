"""Reusable-agent identity, CAS assignment and memory handoff contracts."""

from __future__ import annotations

import copy
import json

import pytest

from lib.storage.errors import StorageError
from lib.swarm.retained import complete_assignment, execute_manage_agents
from lib.swarm.retained_repository import (
    INDEX_NAMESPACE, NAMESPACE, RetainedAgentRepository, scope_for,
)

pytestmark = pytest.mark.unit


class RecordClient:
    def __init__(self):
        self.rows = {}

    def query(self, operation, payload):
        assert operation in {'record.get', 'record.list'}
        namespace = payload['namespace']
        if operation == 'record.get':
            row = self.rows.get((namespace, payload['key']))
            return copy.deepcopy(row) if row else None
        matches = [
            {'key': key, **copy.deepcopy(row)}
            for (stored_namespace, key), row in self.rows.items()
            if stored_namespace == namespace and key.startswith(payload['prefix'])
            and key > payload.get('after_key', '')
        ]
        rows = sorted(matches, key=lambda row: row['key'])[:payload['limit']]
        if payload.get('include_value') is False:
            rows = [{key: value for key, value in row.items() if key != 'value'} for row in rows]
        return rows

    def command(self, operation, payload, command_id):
        del command_id
        assert operation in {'record.put', 'record.delete'}
        key = (payload['namespace'], payload['key'])
        current = self.rows.get(key)
        actual = current['version'] if current else 0
        if payload.get('expected_version') != actual:
            raise StorageError('database_conflict', 'stale record')
        if operation == 'record.delete':
            self.rows.pop(key, None)
            return {'deleted': True}
        self.rows[key] = {'value': copy.deepcopy(payload['value']),
                          'version': actual + 1}
        return {'version': actual + 1}


@pytest.fixture
def records(monkeypatch):
    client = RecordClient()
    monkeypatch.setattr('lib.storage.get_storage_client', lambda write=False: client)
    return client


def _task(owner=7, conv='conv-a'):
    return {'id': f'task-{conv}', 'convId': conv, '_userId': owner,
            'config': {}}


def _manage(args, *, task=None, project='/workspace/alpha'):
    return json.loads(execute_manage_agents(
        args, task=task or _task(), cfg={'model': 'test-model'},
        all_tools=[], project_path=project))


def test_project_roster_survives_turns_and_is_owner_scoped(records):
    created = _manage({'action': 'create', 'name': 'Ada', 'role': 'coder'})['agent']
    listed = _manage({'action': 'list'}, task=_task(conv='conv-b'))['agents']
    assert [item['id'] for item in listed] == [created['id']]
    assert _manage({'action': 'list'}, task=_task(owner=8))['agents'] == []
    assert _manage({'action': 'inspect', 'agent_id': created['id']},
                   task=_task(owner=8))['code'] == 'retained_agent_error'
    assert _manage({'action': 'list'}, project='/workspace/beta')['agents'] == []
    assert RetainedAgentRepository(7, tenant_id='another-tenant').get(
        scope_for(project_path='/workspace/alpha'), created['id']) is None


def test_assignment_is_serial_and_next_run_reads_committed_memory(records, monkeypatch):
    agent_id = _manage({'action': 'create', 'name': 'Analyst',
                        'role': 'analyst'})['agent']['id']
    launches = []

    def launch(_name, args, task, **kwargs):
        launches.append((args, kwargs))
        return json.dumps({'status': 'async_launched',
                           'agents': [{'id': args['agents'][0]['id']}],
                           'swarm_key': task['convId'], 'swarm_run_id': task['id']})

    monkeypatch.setattr('lib.swarm.integration.execute_swarm_tool', launch)
    first = _manage({'action': 'assign', 'agent_id': agent_id,
                     'objective': 'Investigate A'})
    assert first['retained_agent_id'] == agent_id
    assert _manage({'action': 'assign', 'agent_id': agent_id,
                    'objective': 'Investigate B'})['code'] == 'retained_agent_error'
    run_id = launches[0][0]['agents'][0]['id']
    assert launches[0][1]['retained_binding']['run_id'] == run_id
    assert launches[0][0]['agents'][0]['max_retries'] == 0
    scope = scope_for(project_path='/workspace/alpha')
    assert complete_assignment(
        user_id=7, tenant_id='', scope=scope, agent_id=agent_id,
        run_id=run_id,
        messages=[{'role': 'user', 'content': 'Investigate A'},
                  {'role': 'assistant', 'content': 'Found evidence A'}],
        result={'status': 'completed', 'final_answer': 'Found evidence A'},
    )
    second = _manage({'action': 'assign', 'agent_id': agent_id,
                      'objective': 'Follow up B'}, task=_task(conv='conv-b'))
    assert second['status'] == 'async_launched'
    saved, _ = RetainedAgentRepository(7).get(scope, agent_id)
    assert saved['messages'][-1]['content'] == 'Found evidence A'
    assert saved['state'] == 'busy'


def test_reset_fences_late_completion_and_archive_blocks_assignment(records, monkeypatch):
    agent_id = _manage({'action': 'create', 'name': 'Researcher',
                        'role': 'researcher'})['agent']['id']
    monkeypatch.setattr('lib.swarm.integration.execute_swarm_tool',
                        lambda name, args, task, **kwargs: json.dumps({
                            'status': 'async_launched', 'swarm_run_id': task['id'],
                            'agents': [{'id': args['agents'][0]['id']}]}))
    _manage({'action': 'assign', 'agent_id': agent_id, 'objective': 'Read'})
    scope = scope_for(project_path='/workspace/alpha')
    saved, _ = RetainedAgentRepository(7).get(scope, agent_id)
    old_run = saved['run_id']
    assert _manage({'action': 'reset', 'agent_id': agent_id})['code'] == 'retained_agent_error'
    assert _manage({'action': 'delete', 'agent_id': agent_id})['code'] == 'retained_agent_error'
    reset = _manage({'action': 'reset', 'agent_id': agent_id,
                     'abandon': True})['agent']
    assert reset['generation'] == 2
    assert not complete_assignment(
        user_id=7, tenant_id='', scope=scope, agent_id=agent_id,
        run_id=old_run, messages=[{'role': 'user', 'content': 'stale'}],
        result={'status': 'completed'},
    )
    assert _manage({'action': 'archive', 'agent_id': agent_id})['agent']['state'] == 'archived'
    assert _manage({'action': 'assign', 'agent_id': agent_id,
                    'objective': 'Again'})['code'] == 'retained_agent_error'
    assert _manage({'action': 'restore', 'agent_id': agent_id})['agent']['state'] == 'idle'
    assert _manage({'action': 'delete', 'agent_id': agent_id})['deleted'] is True
    assert _manage({'action': 'inspect', 'agent_id': agent_id})['code'] == 'retained_agent_error'


def test_working_memory_is_appended_to_new_child_objective(records, monkeypatch):
    from lib.swarm.master import MasterOrchestrator
    from lib.swarm.types import SubTaskSpec

    scope = scope_for(project_path='/workspace/alpha')
    repository = RetainedAgentRepository(7)
    created = repository.create(scope, role='analyst', name='Analyst',
                                messages=[{'role': 'user', 'content': 'Yesterday'},
                                          {'role': 'assistant', 'content': 'Found A'}])
    run_id = 'abc123def4567890'
    repository.change(scope, created['id'], lambda value: {
        **value, 'state': 'busy', 'run_id': run_id,
    })
    spec = SubTaskSpec(id=run_id, role='analyst', objective='Continue today',
                       model_override='test-model',
                       retained_agent_id=created['id'],
                       retained_scope=scope, retained_run_id=run_id)
    master = MasterOrchestrator(
        task_id='task-a', conv_id='conv-a', user_id=7, specs=[spec],
        model='test-model', project_path='/workspace/alpha',
        parent_task=_task(), parent_config={})

    from lib.identity import PrincipalContext
    from lib.swarm.master import bind_retained_assignment_context
    parent = _task()
    parent['_principalContext'] = PrincipalContext.user(subject_id='test-user', owner_user_id=7).to_payload()
    bind_retained_assignment_context(spec, parent_task=parent, parent_config={},
        all_tools=[], model='test-model', project_path='/workspace/alpha')
    agent = master._make_agent(spec)
    user_messages = [row['content'] for row in agent.messages
                     if row.get('role') == 'user']
    assert user_messages[:2] == ['Yesterday', 'Continue today']
    assert any(row.get('content') == 'Found A' for row in agent.messages)


def test_failed_launch_releases_reservation(records, monkeypatch):
    agent_id = _manage({'action': 'create', 'role': 'coder'})['agent']['id']
    monkeypatch.setattr('lib.swarm.integration.execute_swarm_tool',
                        lambda *args, **kwargs: json.dumps({
                            'status': 'error', 'code': 'execution_budget_exhausted'}))
    failed = _manage({'action': 'assign', 'agent_id': agent_id,
                      'objective': 'Attempt'})
    assert failed['code'] == 'execution_budget_exhausted'
    assert _manage({'action': 'inspect', 'agent_id': agent_id})['agent']['state'] == 'idle'


def test_terminal_swarm_checkpoint_repairs_lost_memory_callback(records, monkeypatch):
    agent_id = _manage({'action': 'create', 'role': 'analyst'})['agent']['id']
    monkeypatch.setattr('lib.swarm.integration.execute_swarm_tool',
                        lambda name, args, task, **kwargs: json.dumps({
                            'status': 'async_launched', 'swarm_run_id': task['id'],
                            'agents': [{'id': args['agents'][0]['id']}]}))
    _manage({'action': 'assign', 'agent_id': agent_id, 'objective': 'Analyze'})
    scope = scope_for(project_path='/workspace/alpha')
    saved, _ = RetainedAgentRepository(7).get(scope, agent_id)
    monkeypatch.setattr('lib.swarm.persistence.load_session',
                        lambda swarm_key: {
                            'task_id': saved['run_session_id'], 'conv_id': saved['run_conversation_id'],
                            'config': {'user_id': 7, 'parent_cfg': {}},
                            'specs': [{'id': saved['run_id'], 'retained_agent_id': agent_id,
                                       'retained_scope': scope, 'retained_run_id': saved['run_id']}],
                            'agents': [{
                                'agent_id': saved['run_id'], 'status': 'completed',
                                'messages': [
                                    {'role': 'user', 'content': 'Analyze'},
                                    {'role': 'assistant', 'content': 'Result'},
                                ],
                                'result': {'status': 'completed',
                                           'final_answer': 'Result'},
                            }],
                        })
    inspected = _manage({'action': 'inspect', 'agent_id': agent_id})['agent']
    assert inspected['state'] == 'idle'
    assert inspected['last_result'] == 'Result'
    assert inspected['message_count'] == 2


def test_promote_reuses_settled_child_memory_only_within_owner(records, monkeypatch):
    monkeypatch.setattr('lib.swarm.persistence.load_session',
                        lambda swarm_key: {
                            'config': {'user_id': 7, 'parent_cfg': {}},
                            'agents': [{
                                'agent_id': 'temp-1', 'role': 'researcher',
                                'status': 'completed',
                                'messages': [{'role': 'user', 'content': 'Source A'}],
                                'result': {'final_answer': 'Source A found'},
                            }],
                        })
    promoted = _manage({'action': 'promote',
                        'source_agent_id': 'temp-1', 'name': 'Research Desk'})
    assert promoted['promoted_from'] == 'temp-1'
    assert promoted['agent']['message_count'] == 1
    assert promoted['agent']['last_result'] == 'Source A found'
    assert _manage({'action': 'promote', 'source_agent_id': 'temp-1'},
                   task=_task(owner=8))['code'] == 'retained_agent_error'


def test_ambiguous_storage_ack_recovers_exact_committed_agent(records, monkeypatch):
    original_command = records.command
    raised = False

    def ambiguous_ack(operation, payload, command_id):
        nonlocal raised
        receipt = original_command(operation, payload, command_id)
        if operation == 'record.put' and not raised:
            raised = True
            raise StorageError('database_timeout', 'reply lost', retryable=True)
        return receipt

    monkeypatch.setattr(records, 'command', ambiguous_ack)
    created = _manage({'action': 'create', 'role': 'analyst'})['agent']
    assert created['state'] == 'idle'
    assert len(_manage({'action': 'list'})['agents']) == 1


def test_roster_lists_metadata_without_reading_large_memories(records, monkeypatch):
    scope = scope_for(project_path='/workspace/alpha')
    repository = RetainedAgentRepository(7)
    created = repository.create(
        scope, role='analyst', name='Large memory',
        messages=[{'role': 'tool', 'content': 'e' * (512 * 1024)}],
    )
    listed_namespaces = []
    original_query = records.query

    def traced_query(operation, payload):
        if operation == 'record.list':
            listed_namespaces.append(payload['namespace'])
        return original_query(operation, payload)

    monkeypatch.setattr(records, 'query', traced_query)
    agents = repository.list(scope)
    assert [agent['id'] for agent in agents] == [created['id']]
    assert agents[0]['message_count'] == 1
    assert listed_namespaces and set(listed_namespaces) == {INDEX_NAMESPACE, NAMESPACE}
    assert (NAMESPACE, repository._key(scope, created['id'])) in records.rows


def test_ambiguous_roster_ack_is_reconciled_before_rewrite(records, monkeypatch):
    original_command = records.command
    raised = False

    def ambiguous_roster_ack(operation, payload, command_id):
        nonlocal raised
        receipt = original_command(operation, payload, command_id)
        if (operation == 'record.put' and payload['namespace'] == INDEX_NAMESPACE
                and not raised):
            raised = True
            raise StorageError('database_timeout', 'reply lost', retryable=True)
        return receipt

    monkeypatch.setattr(records, 'command', ambiguous_roster_ack)
    created = _manage({'action': 'create', 'role': 'analyst'})['agent']
    assert created['state'] == 'idle'
    assert len(_manage({'action': 'list'})['agents']) == 1


def test_completion_detects_reset_racing_its_cas_retry(records, monkeypatch):
    scope = scope_for(project_path='/workspace/alpha')
    repository = RetainedAgentRepository(7)
    created = repository.create(scope, role='analyst', name='Analyst')
    repository.change(scope, created['id'], lambda value: {
        **value, 'state': 'busy', 'run_id': 'old-run',
    })
    original_change = RetainedAgentRepository.change
    reset_done = False

    def reset_before_completion(self, target_scope, agent_id, transform):
        nonlocal reset_done
        if not reset_done:
            reset_done = True
            original_change(self, target_scope, agent_id, lambda value: {
                **value, 'state': 'idle', 'generation': 2, 'run_id': '',
                'messages': [],
            })
        return original_change(self, target_scope, agent_id, transform)

    monkeypatch.setattr(RetainedAgentRepository, 'change', reset_before_completion)
    assert not complete_assignment(
        user_id=7, tenant_id='', scope=scope, agent_id=created['id'],
        run_id='old-run', messages=[{'role': 'assistant', 'content': 'stale'}],
        result={'status': 'completed'},
    )
    saved, _ = repository.get(scope, created['id'])
    assert saved['messages'] == []
    assert saved['generation'] == 2


def test_team_control_tools_are_root_only_and_reads_skip_write_approval():
    from lib.swarm.registry import scope_tools_for_role
    from lib.tools.registry import all_specs
    from lib.tools.retained_agents import LIST_AGENTS_TOOL, MANAGE_AGENTS_TOOL

    spec = next(item for item in all_specs() if item.key == 'retained_agents')
    assert spec.write_tools == frozenset({'manage_agents'})
    assert spec.provides == frozenset({'list_agents', 'manage_agents'})
    assert scope_tools_for_role('general', [LIST_AGENTS_TOOL, MANAGE_AGENTS_TOOL]) == []


def test_required_memory_completion_waits_without_restarting_assignment(records, monkeypatch):
    from threading import Event
    monkeypatch.setattr('lib.tasks_pkg.persistence_wait._BASE_DELAY_SECONDS', 0)
    repository = RetainedAgentRepository(7)
    scope = scope_for(project_path='/workspace/alpha')
    created = repository.create(scope, role='coder', name='Evidence')
    repository.change(scope, created['id'], lambda value: {
        **value, 'state': 'busy', 'run_id': 'assigned-run'})
    original = records.command
    attempts = []
    def pressured(operation, payload, command_id):
        if payload['namespace'] == NAMESPACE:
            attempts.append((copy.deepcopy(payload), command_id))
            if len(attempts) <= 20:
                raise StorageError('database_busy', 'temporary memory pressure')
        return original(operation, payload, command_id)
    monkeypatch.setattr(records, 'command', pressured)
    carrier = {'id': 'execution', 'config': {}, 'abort_event': Event(), 'aborted': True}
    assert complete_assignment(user_id=7, tenant_id='', scope=scope,
        agent_id=created['id'], run_id='assigned-run',
        messages=[{'role': 'assistant', 'content': 'verified effects'}],
        result={'status': 'completed', 'final_answer': 'verified effects'}, task=carrier)
    saved, version = repository.get(scope, created['id'])
    assert saved['state'] == 'idle' and saved['last_status'] == 'completed'
    assert saved['messages'] == [{'role': 'assistant', 'content': 'verified effects'}]
    assert version == 3
    assert len(attempts) == 21 and all(entry == attempts[0] for entry in attempts)
    assert not carrier['_storageWaits']


def test_complete_roster_beyond_old_identity_and_scan_limits(records):
    repository = RetainedAgentRepository(7, client=lambda **kwargs: records)
    scope = scope_for(conversation_id='large-roster', requested='conversation')
    identities = [repository.create(scope, role='coder', name=f'Colleague {index}')['id']
                  for index in range(1101)]
    listed = repository.list(scope)
    assert {row['id'] for row in listed} == set(identities)
    assert len(listed) == 1101
    assert RetainedAgentRepository(8, client=lambda **kwargs: records).list(scope) == []
