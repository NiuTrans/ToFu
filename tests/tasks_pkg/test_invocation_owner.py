"""The root API enters one synchronous owner without weakening isolation."""
from dataclasses import replace
import threading

import pytest

from lib.tasks_pkg import invocation
from lib.tasks_pkg.orchestrator import _run

pytestmark = pytest.mark.unit


def test_root_calls_declared_owner_with_unchanged_carrier(monkeypatch):
    task = {'id': 'invocation-owner-root', '_userId': 71,
            'config': {'model': 'fixture-model'}, 'messages': []}
    calls = []
    caller = threading.get_ident()
    monkeypatch.setattr(
        _run, '_execute_task_invocation',
        lambda carrier, *, policy: calls.append(
            (carrier, policy, threading.get_ident())))
    assert _run.run_task(task) is None
    assert calls == [(task, invocation.ROOT_INVOCATION_POLICY, caller)]
    assert calls[0][0] is task


@pytest.mark.parametrize('changes', [
    {'parent_task_id': 'parent'}, {'settlement': 'parent'},
    {'presentation': 'goal'}, {'cancellation': 'parent'},
])
def test_unsupported_child_policy_fails_before_any_mutation(monkeypatch, changes):
    task = {'id': 'invocation-owner-child', '_userId': 72}
    original = dict(task)
    monkeypatch.setattr(
        _run, 'check_autopilot_kick',
        lambda _: pytest.fail('child reached kickoff'))
    with pytest.raises(ValueError, match='task-local evidence port'):
        invocation.execute_invocation(
            task, policy=replace(invocation.ROOT_INVOCATION_POLICY, **changes))
    assert task == original


def test_explicit_owner_preserves_root_kickoff_and_cleanup(monkeypatch):
    calls = []
    monkeypatch.setattr(_run, 'check_autopilot_kick',
                        lambda task: calls.append('kick') or True)
    monkeypatch.setattr(_run, 'set_req_id',
                        lambda value: calls.append(('request', value)))
    monkeypatch.setattr(_run, 'clear_log_context',
                        lambda: calls.append('clear'))
    invocation.execute_invocation(
        {'id': 'owner-kickoff'}, policy=invocation.ROOT_INVOCATION_POLICY)
    assert calls == [('request', 'owner-ki'), 'kick', ('request', ''), 'clear']


def test_owner_rejects_missing_carrier_id():
    with pytest.raises(ValueError, match="missing 'id'"):
        invocation.execute_invocation({}, policy=invocation.ROOT_INVOCATION_POLICY)
