"""Persistence port for Flow-backed chat endpoint turns."""

from pathlib import Path

import pytest

from lib.orchestration_chat_turn_persistence import (
    OrchestrationChatTurnPersistence,
)


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def _port(task, calls, *, fail_store=False):
    def store(owner, turns):
        calls.append(('store', owner, len(turns)))
        if fail_store:
            raise RuntimeError('database offline')

    def sync(owner, turns):
        calls.append(('sync', owner, len(turns)))
        return len(turns) - 1

    return OrchestrationChatTurnPersistence(
        task,
        store_turns=store,
        sync_turns=sync,
        translate_turn=lambda owner, message, index: calls.append(
            ('turn', owner, message, index)),
        translate_final=lambda owner, turns: calls.append(
            ('final', owner, len(turns))),
    )


def test_live_buffer_binding_drives_incremental_and_final_sync():
    task = {'id': 'task-one'}
    calls = []
    persistence = _port(task, calls)
    messages = [{'role': 'assistant', 'content': 'one'}]
    persistence.bind(messages)

    assert persistence(messages[0]) is True
    messages.append({'role': 'user', 'content': 'review'})
    assert persistence(messages[1]) is True
    assert persistence.finalize() is True

    assert [call[0] for call in calls] == [
        'store', 'sync', 'turn',
        'store', 'sync', 'turn',
        'store', 'sync', 'final',
    ]
    assert calls[2][3] == 0
    assert calls[5][3] == 1
    assert all(call[1] is task for call in calls)


def test_unbound_empty_and_rebinding_contracts_are_explicit():
    persistence = _port({'id': 'task-two'}, [])

    assert persistence({'role': 'assistant'}) is False
    assert persistence.finalize() is False
    with pytest.raises(TypeError):
        persistence.bind(())
    first = []
    persistence.bind(first)
    persistence.bind(first)
    with pytest.raises(RuntimeError):
        persistence.bind([])


def test_final_translation_safety_net_runs_when_database_sync_fails():
    task = {'id': 'task-three'}
    calls = []
    persistence = _port(task, calls, fail_store=True)
    messages = [{'role': 'assistant', 'content': 'partial'}]
    persistence.bind(messages)

    assert persistence(messages[0]) is False
    assert persistence.finalize() is False
    assert [call[0] for call in calls] == ['store', 'store', 'final']


def test_endpoint_runner_only_assembles_turn_persistence_ports():
    runner = (ROOT / 'lib' / 'orchestration_endpoint_runner.py').read_text()
    runtime = (
        ROOT / 'lib' / 'orchestration_chat_flow_runtime.py').read_text()
    completion = (
        ROOT / 'lib' / 'orchestration_chat_completion.py').read_text()

    assert 'execute_orchestration_chat_flow_task(' in runner
    assert 'OrchestrationChatFlowRuntimePorts' in runtime
    assert 'OrchestrationChatTurnPersistence(' in runtime
    assert 'emit=turn_persistence' in runtime
    assert 'store_turns=ports.store_turns' in runtime
    assert 'turn_persistence.bind(adapter.messages)' in runtime
    assert 'turn_persistence=turn_persistence' in runtime
    assert 'self._turn_persistence.finalize()' in completion
    assert '_adapter_ref' not in runner + runtime
    assert 'def _persist_endpoint_msg' not in runner + runtime
