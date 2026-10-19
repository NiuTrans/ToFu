"""Lost retained callbacks settle only from an exact assignment witness."""

from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from lib.swarm.retained import _reconcile, execute_manage_agents
from lib.swarm.retained_repository import RetainedAgentRepository, scope_for
from tests.swarm.test_retained_agents import RecordClient

pytestmark = pytest.mark.unit

PROJECT = '/workspace/retained-reconciliation'


@pytest.fixture
def context(monkeypatch):
    client = RecordClient()
    monkeypatch.setattr('lib.storage.get_storage_client', lambda write=False: client)
    repository = RetainedAgentRepository(7, tenant_id='tenant-a')
    scope = scope_for(project_path=PROJECT)
    agent = repository.create(scope, role='analyst', name='Analyst', messages=[
        {'role': 'assistant', 'content': 'Previously verified memory'},
    ])
    value = repository.change(scope, agent['id'], lambda current: {
        **current, 'state': 'busy', 'run_id': 'child-a',
        'swarm_key': 'conv-a', 'run_session_id': 'session-a',
        'run_conversation_id': 'conv-a',
    })
    session = {
        'task_id': 'session-a', 'conv_id': 'conv-a',
        'config': {'user_id': 7, 'parent_cfg': {'_tenant_id': 'tenant-a'}},
        'specs': [{
            'id': 'child-a', 'retained_agent_id': agent['id'],
            'retained_scope': scope, 'retained_run_id': 'child-a',
        }],
        'agents': [{
            'agent_id': 'child-a', 'status': 'completed',
            'messages': [{'role': 'assistant', 'content': 'Exact settled memory'}],
            'result': {'status': 'completed', 'final_answer': 'Exact result'},
        }],
    }
    monkeypatch.setattr('lib.swarm.persistence.load_session',
                        lambda key: deepcopy(session) if key == 'conv-a' else None)
    return SimpleNamespace(repository=repository, scope=scope, value=value,
                           session=session, client=client)


def assert_stays_busy(context):
    before = deepcopy(context.client.rows)
    result = _reconcile(context.repository, context.scope, context.value)
    assert result == context.value
    assert result['state'] == 'busy'
    assert result['messages'] == [
        {'role': 'assistant', 'content': 'Previously verified memory'},
    ]
    assert 'last_assignment' not in result
    # Neither retained memory nor roster metadata may be rewritten/released.
    assert context.client.rows == before


@pytest.mark.parametrize('field,replacement', [
    ('task_id', 'replacement-session'),
    ('conv_id', 'replacement-conversation'),
    ('task_id', None),
    ('conv_id', None),
])
def test_same_owner_child_id_in_replacement_session_is_not_settlement(
        context, field, replacement):
    context.session[field] = replacement
    assert_stays_busy(context)


@pytest.mark.parametrize('field', ['run_session_id', 'run_conversation_id', 'run_id'])
def test_incomplete_saved_assignment_stays_unknown(context, field):
    context.value.pop(field)
    assert_stays_busy(context)


@pytest.mark.parametrize('field', [
    'id', 'retained_agent_id', 'retained_scope', 'retained_run_id',
])
@pytest.mark.parametrize('replacement', [None, 'spoofed-binding'])
def test_incomplete_or_spoofed_spec_cannot_import_child_transcript(
        context, field, replacement):
    context.session['specs'][0][field] = replacement
    assert_stays_busy(context)


@pytest.mark.parametrize('field', ['specs', 'agents'])
@pytest.mark.parametrize('kind', ['missing', 'duplicate', 'malformed'])
def test_ambiguous_or_incomplete_checkpoint_stays_unknown(context, field, kind):
    if kind == 'missing':
        context.session.pop(field)
    elif kind == 'duplicate':
        context.session[field].append(deepcopy(context.session[field][0]))
    else:
        context.session[field] = {'not': 'a checkpoint list'}
    assert_stays_busy(context)


@pytest.mark.parametrize('kind', ['owner', 'tenant'])
def test_exact_binding_does_not_bypass_owner_or_tenant(context, kind):
    if kind == 'owner':
        context.session['config']['user_id'] = 8
    else:
        context.session['config']['parent_cfg']['_tenant_id'] = 'tenant-b'
    assert_stays_busy(context)


@pytest.mark.parametrize('status', ['completed', 'failed', 'cancelled'])
def test_exact_terminal_assignment_settles_and_records_exact_witness(context, status):
    context.session['agents'][0]['status'] = status
    context.session['agents'][0]['result']['status'] = status
    result = _reconcile(context.repository, context.scope, context.value)
    assert result['state'] == 'idle'
    assert result['run_id'] == ''
    assert result['messages'] == context.session['agents'][0]['messages']
    assert result['last_status'] == status
    assert result['last_result'] == 'Exact result'
    assert result['last_assignment'] == {
        'agent_id': 'child-a', 'swarm_run_id': 'session-a',
        'conversation_id': 'conv-a', 'swarm_key': 'conv-a',
    }
    assert context.repository.get(context.scope, result['id'])[0] == result


def test_exact_nonterminal_checkpoint_does_not_release_assignment(context):
    context.session['agents'][0]['status'] = 'running'
    assert_stays_busy(context)


@pytest.mark.parametrize('receipt_session_id', ['older-session', None])
def test_manage_assignment_uses_launch_session_authority_not_callers_task(
        context, monkeypatch, receipt_session_id):
    context.repository.change(context.scope, context.value['id'], lambda current: {
        **current, 'state': 'idle', 'run_id': '',
        # A previous assignment identity must not leak into the new reservation.
        'run_session_id': 'stale-session',
    })
    task = {'id': 'current-task', 'convId': 'conv-a', '_userId': 7,
            '_tenant_id': 'tenant-a', 'config': {}}

    def launch(name, args, parent, **kwargs):
        assert name == 'spawn_agents'
        saved = context.repository.get(context.scope, context.value['id'])[0]
        assert saved['run_session_id'] == ''
        assert saved['run_conversation_id'] == 'conv-a'
        return json.dumps({
            'status': 'async_launched',
            'agents': [{'id': args['agents'][0]['id']}],
            'swarm_run_id': receipt_session_id,
        })

    monkeypatch.setattr('lib.swarm.integration.execute_swarm_tool', launch)
    result = json.loads(execute_manage_agents(
        {'action': 'assign', 'agent_id': context.value['id'], 'objective': 'Analyze'},
        task=task, cfg={'model': 'test-model'}, all_tools=[], project_path=PROJECT))
    if receipt_session_id:
        assert result['status'] == 'async_launched'
    else:
        assert result['submission_state'] == 'unknown'
    saved = context.repository.get(context.scope, context.value['id'])[0]
    assert saved['run_session_id'] == (receipt_session_id or '')
    assert saved['run_session_id'] != task['id']
    assert saved['state'] == 'busy'
