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
