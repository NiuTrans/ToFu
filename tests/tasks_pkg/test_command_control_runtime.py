"""Only the verified private resident incarnation can receive remote controls."""
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('state,live,incarnation', [
    ('starting', True, 'current'), ('ready', False, 'current'), ('ready', True, 'old'),
])
def test_remote_lookup_refuses_stale_execution_owner(monkeypatch, state, live, incarnation):
    from lib.tasks_pkg import command_control_runtime as module
    monkeypatch.setattr(module, 'apply_command_control', lambda *a, **k: None)
    monkeypatch.setattr(module, 'call', lambda *a, **k: pytest.fail('unverified control sent'))
    service = SimpleNamespace(host_matches=lambda record: live, record=lambda: {
        'state': state, 'serviceId': 'current', 'commandControl': {'serviceId': incarnation}})
    with pytest.raises(module.CommandControlUnavailable):
        module.CommandControls(service=service).execute('interrupt', 'task', user_id=41)


def test_remote_lookup_forwards_exact_owner_and_command_identity(monkeypatch):
    from lib.tasks_pkg import command_control_runtime as module
    monkeypatch.setattr(module, 'apply_command_control', lambda *a, **k: None)
    endpoint = {'serviceId': 'current'}
    calls = []
    monkeypatch.setattr(module, 'call', lambda *a, **k: calls.append((a, k)) or {
        'backgrounded': False, 'reason': 'handoff_pending'})
    service = SimpleNamespace(host_matches=lambda record: True, record=lambda: {
        'state': 'ready', 'serviceId': 'current', 'commandControl': endpoint})
    result = module.CommandControls(service=service).execute(
        'background', 'task', user_id=41, round_num=2, tool_call_id='exact')
    assert result == {'backgrounded': False, 'reason': 'handoff_pending'}
    assert calls == [((endpoint, 'background', 'task'), {
        'user_id': 41, 'params': {'round_num': 2, 'tool_call_id': 'exact'}})]


def _ready_service(endpoint):
    return SimpleNamespace(host_matches=lambda record: True, record=lambda: {
        'state': 'ready', 'serviceId': 'current', 'commandControl': endpoint})


def test_execution_storage_metrics_ask_only_the_resident_incarnation(monkeypatch):
    from lib.tasks_pkg import command_control_runtime as module
    monkeypatch.setattr(module, 'apply_command_control',
                        lambda *a, **k: pytest.fail('the API histogram is not the execution host'))
    calls = []
    remote = {'histograms': []}
    monkeypatch.setattr(module, 'call', lambda *a, **k: calls.append((a, k)) or remote)
    endpoint = {'serviceId': 'current'}
    controls = module.CommandControls(service=_ready_service(endpoint))
    assert controls.execution_storage_rpc_metrics(user_id=41) is remote
    assert calls == [((endpoint, 'storage_rpc_metrics', ''), {'user_id': 41, 'params': {}})]
    # Without a split this process runs every task; its own histogram covers them.
    assert module.CommandControls(service=None).execution_storage_rpc_metrics(user_id=41) is None


def test_activity_unions_local_and_remote_views_once(monkeypatch):
    from lib.tasks_pkg import command_control_runtime as module
    local = {'swarms': [{'swarm_id': 'local-run'}, {'swarm_id': 'shared-run'}],
             'backgroundCommands': [{'command_id': 'c1'}]}
    remote = {'swarms': [{'swarm_id': 'shared-run'}, {'swarm_id': 'remote-run'}],
              'backgroundCommands': [{'command_id': 'c1'}, {'command_id': 'c2'}]}
    monkeypatch.setattr(module, 'apply_command_control', lambda method, task_id, **k: (
        local if (method, task_id) == ('background_activity', '') else pytest.fail('local task control')))
    calls = []
    monkeypatch.setattr(module, 'call', lambda *a, **k: calls.append((a, k)) or remote)
    endpoint = {'serviceId': 'current'}
    view = module.CommandControls(service=_ready_service(endpoint)).background_activity(user_id=41)
    assert [row['swarm_id'] for row in view['swarms']] == ['local-run', 'shared-run', 'remote-run']
    assert [row['command_id'] for row in view['backgroundCommands']] == ['c1', 'c2']
    assert calls == [((endpoint, 'background_activity', ''), {'user_id': 41, 'params': {}})]
    assert module.CommandControls(service=None).background_activity(user_id=41) == local


def test_unverified_execution_owner_has_no_activity_view(monkeypatch):
    from lib.tasks_pkg import command_control_runtime as module
    monkeypatch.setattr(module, 'apply_command_control',
                        lambda *a, **k: {'swarms': [], 'backgroundCommands': []})
    monkeypatch.setattr(module, 'call', lambda *a, **k: pytest.fail('unverified control sent'))
    service = SimpleNamespace(host_matches=lambda record: True, record=lambda: {
        'state': 'starting', 'serviceId': 'current', 'commandControl': {'serviceId': 'current'}})
    with pytest.raises(module.CommandControlUnavailable):
        module.CommandControls(service=service).background_activity(user_id=41)


@pytest.mark.parametrize('outcome,expected', [
    ({'success': True, 'outcome': 'aborted'}, {'aborted': True}),
    ({'success': False, 'outcome': 'failed', 'error': 'boom'}, {'aborted': False, 'reason': 'abort_failed'}),
    ({'success': False, 'outcome': 'not_found'}, None),
])
def test_swarm_abort_maps_the_session_outcome(monkeypatch, outcome, expected):
    from lib.tasks_pkg.command_controls import apply_command_control
    seen = []
    monkeypatch.setattr('lib.swarm.integration.abort_swarm',
                        lambda task_id, *, user_id: seen.append((task_id, user_id)) or outcome)
    assert apply_command_control('swarm_abort', 'swarm', user_id=41) == expected
    assert seen == [('swarm', 41)]


@pytest.mark.parametrize('outcome,expected', [
    ({'outcome': 'stopped', 'status': 'running'}, {'stopped': True, 'status': 'running'}),
    ({'outcome': 'not_running', 'status': 'pending'},
     {'stopped': False, 'reason': 'not_running', 'status': 'pending'}),
    ({'outcome': 'not_found', 'status': ''}, None),
])
def test_swarm_agent_stop_maps_the_session_outcome(monkeypatch, outcome, expected):
    from lib.tasks_pkg.command_controls import apply_command_control
    seen = []
    monkeypatch.setattr('lib.swarm.integration.stop_swarm_agent',
                        lambda swarm_id, agent_id, *, user_id: seen.append((swarm_id, agent_id, user_id)) or outcome)
    assert apply_command_control('swarm_agent_stop', 'run', user_id=41, agent_id='a') == expected
    assert seen == [('run', 'a', 41)]


def test_local_activity_lists_only_running_commands(monkeypatch):
    from lib.tasks_pkg.command_controls import apply_command_control
    monkeypatch.setattr('lib.swarm.integration.list_active_swarms', lambda *, user_id: [{'swarm_id': 'run'}])
    monkeypatch.setattr('lib.tasks_pkg.background_commands.list_background_commands', lambda *, user_id: [
        {'command_id': 'live', 'status': 'running'}, {'command_id': 'done', 'status': 'completed'}])
    assert apply_command_control('background_activity', '', user_id=41) == {
        'swarms': [{'swarm_id': 'run'}], 'backgroundCommands': [{'command_id': 'live', 'status': 'running'}]}
    with pytest.raises(ValueError):
        apply_command_control('background_activity', 'task', user_id=41)
