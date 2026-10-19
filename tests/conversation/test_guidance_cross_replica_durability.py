"""Distributed guidance flags are routing hints, never scoped answer authority."""
import uuid

import pytest

from lib.storage import StorageError

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = pytest.mark.unit


class Flags:
    def __init__(self): self.values = {}
    def get_value(self, kind, key): return self.values.get((kind, key))
    def set_value(self, kind, key, value, *_): self.values[(kind, key)] = value
    def set_value_nx(self, kind, key, value, *_):
        if (kind, key) in self.values: return False
        self.values[(kind, key)] = value
        return True
    def delete_value(self, kind, key): self.values.pop((kind, key), None)


def question():
    from lib.turn_lifecycle import create_turn_pair, bind_task, record_task_event
    conv = 'replica-' + uuid.uuid4().hex[:12]
    result = create_turn_pair(conv, command_id='initial-' + conv,
        input_projection={'content': 'Choose'}, config={}, user_id=1,
        conversation_defaults={'allowCreate': True, 'title': 'Choose', 'settings': {}})
    turn = result['turn']['turnId']; attempt = result['attempt']['attemptId']
    task_id = 'task-' + conv
    bind_task(attempt, task_id, user_id=1)
    task = {'id': task_id, '_attemptId': attempt, '_turnId': turn, '_userId': 1,
        'convId': conv, 'status': 'running', 'content': '', 'thinking': '', 'config': {},
        'toolRounds': [{'roundNum': 1, 'llmRound': 0, 'toolCallId': 'call', 'toolName': 'ask_human',
            'toolArgs': {}, 'toolContent': None, 'status': 'awaiting_human', 'guidanceId': 'hg_replicas',
            'guidanceQuestion': 'Choose', 'guidanceType': 'choice', 'guidanceOptions': ['Client', 'Server']}]}
    record_task_event(task, {'type': 'tool_result'})
    return task


def registry_pair(monkeypatch):
    from lib.tasks_pkg import human_gate_registry as gates
    from lib.tasks_pkg import human_guidance as guidance
    from lib.runtime import runtime_state_store
    store = Flags(); first = gates.OwnedHumanGateRegistry(4); second = gates.OwnedHumanGateRegistry(4)
    monkeypatch.setattr(gates, '_distributed_mode', lambda: True)
    monkeypatch.setattr(runtime_state_store, 'get_store', lambda: store)
    monkeypatch.setattr(guidance, 'human_gate_registry', first)
    return guidance, first, second, store


def test_remote_legacy_answer_persists_before_waiter_consumes(chat_sidecar, monkeypatch):
    from lib.turn_lifecycle import get_turn
    guidance, first, second, _ = registry_pair(monkeypatch); task = question()
    def remote():
        monkeypatch.setattr(guidance, 'human_gate_registry', second)
        try:
            assert guidance.resolve_human_guidance('hg_replicas', 'Server', owner_user_id=1)
            saved = get_turn(task['convId'], task['_turnId'], user_id=1)
            assert saved['projection']['toolRounds'][0]['guidanceResponse'] == 'Server'
            assert saved['projection']['toolRounds'][0]['guidanceAnswerCommandId']
        finally:
            monkeypatch.setattr(guidance, 'human_gate_registry', first)
    assert guidance.request_human_guidance('hg_replicas', owner_user_id=1, task=task, on_registered=remote) == 'Server'


def test_remote_legacy_undeclared_choice_cannot_reach_model(chat_sidecar, monkeypatch):
    guidance, first, second, _ = registry_pair(monkeypatch); task = question()
    def remote():
        monkeypatch.setattr(guidance, 'human_gate_registry', second)
        try:
            with pytest.raises(StorageError, match='declared|selected|option'):
                guidance.resolve_human_guidance('hg_replicas', 'Injected', owner_user_id=1)
        finally:
            monkeypatch.setattr(guidance, 'human_gate_registry', first)
    assert guidance.request_human_guidance('hg_replicas', owner_user_id=1, task=task, on_registered=remote, abort_check=lambda: True) is None


def test_scoped_waiter_ignores_volatile_notification(chat_sidecar, monkeypatch):
    guidance, first, _, _ = registry_pair(monkeypatch); task = question(); calls = 0
    def notify():
        from lib.tasks_pkg.human_gate_registry import GATE_GUIDANCE
        assert first.resolve(GATE_GUIDANCE, 'hg_replicas', owner_user_id=1, response='Injected')
    def abort():
        nonlocal calls
        calls += 1
        return calls > 1
    assert guidance.request_human_guidance('hg_replicas', owner_user_id=1, task=task, on_registered=notify, abort_check=abort) is None
