"""A personal owner keeps one swarm scope across restart and durable recovery.

Task registration stores an absent tenant as ``''`` while ``PrincipalContext``
represents the same personal scope as ``None``. Startup recovery rebuilds the
session parent from the durable principal and records results with it, and
rows persisted before task birth carried a tenant hold none at all. Every
caller here is registered through the real task registry; spawn, restart
recovery and the result reads run over a real storage authority. The owner's
own later turns must reach those sessions, rows and records, while a named
tenant still has to match exactly.
"""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lib.identity import PrincipalContext
from lib.swarm import persistence
from lib.swarm.integration import _logs, _rehydrate, _state, execute_swarm_tool
import lib.swarm.integration as integration
from tests.swarm.test_swarm_async import _wait_until
from tests.swarm.test_swarm_run_results import (  # noqa: F401
    OWNER, _CheckpointingAgent, _expire, authority, conversation)

pytestmark = pytest.mark.unit


@pytest.fixture
def turns(conversation, monkeypatch, tmp_path):  # noqa: F811
    """Register owner turns of one conversation; workers settle on start."""
    from lib.tasks_pkg.manager import create_task, discard_task
    monkeypatch.setattr(_logs, 'SWARM_OUTPUT_DIR', str(tmp_path))
    monkeypatch.setattr('lib.swarm.master._build_sub_agent', lambda spec, **kwargs:
                        _CheckpointingAgent(spec, 'ANSWER-' + spec.id, kwargs['swarm_key']))
    # The spawning turn completed before the restart; nothing stopped it.
    monkeypatch.setattr('lib.swarm.restart_recovery.read_recovery_turns',
                        lambda *a, **kw: {'turns': [{'status': 'completed'}]})
    registered = []

    def register(tenant=None):
        principal = PrincipalContext.user(
            subject_id=f'user:{OWNER}', owner_user_id=OWNER, tenant_id=tenant)
        task = create_task(conversation, [{'role': 'user', 'content': 'delegate'}], {},
                           principal=principal, supersede=False)
        registered.append(task['id'])
        task['status'] = 'running'
        return task

    yield register
    for task_id in registered:
        discard_task(task_id)


def _spawn(task, agent_id):
    handle = json.loads(execute_swarm_tool('spawn_agents', {'agents': [{
        'id': agent_id, 'role': 'researcher', 'objective': f'Investigate {agent_id}'}]},
        task=task))
    assert handle['status'] == 'async_launched', handle
    task['status'] = 'done'  # The spawning turn finishes; its run is detached.
    return handle['swarm_run_id']


def _restart(conv):
    """Process restart: in-memory sessions are gone, durable rows remain."""
    with _state._sessions_lock:
        _state._active_sessions.pop(conv, None)
        _state._session_timestamps.pop(conv, None)


def _result(task, **args):
    return json.loads(execute_swarm_tool('get_agent_result', args, task=task))


def _poll(task, handle, monkeypatch, tmp_path):
    from lib.conversations import project_coordination
    from lib.tasks_pkg.executor import _execute_tool_one
    from lib.tasks_pkg.handlers import poll as poll_mod
    monkeypatch.setattr(poll_mod, '_finalize_tool_round', Mock())
    monkeypatch.setattr(project_coordination, 'ProjectCoordinationRepository',
                        lambda owner: SimpleNamespace(read=lambda path: {
                            'projectPath': path, 'conversationId': '', 'revision': 0,
                            'availability': 'unbound'}))
    entry = {'toolCallId': 'tc-poll', 'toolName': 'poll', 'status': 'searching'}
    return _execute_tool_one(task, {}, 'poll', 'tc-poll', {'id': handle}, 1, entry, {},
                             str(tmp_path), True)[1]


def _named_tenant_denied(turns, code, **args):
    """The same owner in a named tenant is another scope."""
    foreign = turns('tenant-a')
    payload = _result(foreign, **args)
    foreign['status'] = 'done'
    assert payload['status'] == 'error' and payload['code'] == code, payload
    assert 'ANSWER' not in json.dumps(payload)


def test_startup_recovered_run_serves_its_personal_owner(
        conversation, turns, monkeypatch, tmp_path):  # noqa: F811
    conv = conversation
    launch = turns()
    assert launch['_tenant_id'] == '' and launch['_principalContext']['tenant_id'] is None
    # The worker is still running when the process stops.
    monkeypatch.setattr('lib.swarm.master.MasterOrchestrator.run_in_background',
                        lambda self: None)
    run_id = _spawn(launch, 'first')
    _restart(conv)
    assert _rehydrate._rehydrate_one(persistence.load_session(conv))
    recovered = integration.get_active_session(conv)
    assert _wait_until(lambda: recovered.is_terminated, timeout=10.0)

    later = turns()
    live = _result(later, agent_id='first')
    assert live['status'] == 'ok', live
    assert (live['agent_status'], live['final_answer']) == ('completed', 'ANSWER-first')
    waited = json.loads(execute_swarm_tool(
        'await_agents', {'ids': ['first'], 'mode': 'all'}, task=later))
    assert [row['result'] for row in waited['completed']] == ['ANSWER-first'], waited
    polled = _poll(later, 'first', monkeypatch, tmp_path)
    assert 'ANSWER-first' in polled and 'Unknown handle' not in polled
    _named_tenant_denied(turns, 'swarm_scope_denied', agent_id='first')

    # The conversation goes quiet; its rows are reclaimed after recording.
    later['status'] = 'done'
    _expire(conv, monkeypatch)
    assert persistence.load_session(conv) is None
    returning = turns()
    recorded = _result(returning, agent_id='first', swarm_run_id=run_id)
    assert recorded['status'] == 'ok', recorded
    assert (recorded['source'], recorded['final_answer']) == ('run_record', 'ANSWER-first')
    _named_tenant_denied(turns, 'swarm_not_found', agent_id='first', swarm_run_id=run_id)


def test_rows_saved_before_task_tenant_identity_stay_readable(conversation, turns):  # noqa: F811
    conv = conversation
    legacy = turns()
    del legacy['_tenant_id']  # Registered before task birth carried a tenant.
    run_id = _spawn(legacy, 'a1')
    assert _wait_until(lambda: integration.get_active_session(conv).is_terminated,
                       timeout=10.0)
    assert '_tenant_id' not in persistence.load_session(conv)['config']['parent_cfg']
    _restart(conv)

    later = turns()
    persisted = _result(later, agent_id='a1')
    assert persisted['status'] == 'ok', persisted
    assert (persisted['source'], persisted['swarm_run_id']) == ('persisted', run_id)
    assert persisted['final_answer'] == 'ANSWER-a1'
    _named_tenant_denied(turns, 'swarm_not_found', agent_id='a1')
