"""The Flow-backed chat endpoint consumes the canonical outcome projection."""

import threading

import pytest


pytestmark = pytest.mark.unit


def _task():
    return {
        'id': 'flow-outcome-task',
        'convId': 'flow-outcome-conv',
        'messages': [{'role': 'user', 'content': 'do the thing'}],
        'config': {'endpointMaxIterations': 1},
        'events': [],
        'events_lock': threading.Lock(),
        'content_lock': threading.Lock(),
        'toolRounds': [],
        'phase': 'tool',
    }


def _install_side_effect_stubs(monkeypatch):
    import lib.orchestration_endpoint_runner as runner
    import lib.tasks_pkg.endpoint as endpoint
    import lib.tasks_pkg.manager as manager

    events = []
    monkeypatch.setattr(runner, '_build_tools_for_task',
                        lambda _task: ([], '', ''))
    monkeypatch.setattr(manager, 'append_event',
                        lambda _task, event: events.append(event))
    monkeypatch.setattr(manager, 'persist_task_result', lambda _task: None)
    monkeypatch.setattr(endpoint, '_store_endpoint_turns_on_task',
                        lambda *_args: None)
    monkeypatch.setattr(endpoint, '_sync_endpoint_turns_to_conversation',
                        lambda *_args: 0)
    monkeypatch.setattr(endpoint, '_trigger_per_turn_auto_translate',
                        lambda *_args: None)
    monkeypatch.setattr(endpoint, '_trigger_endpoint_auto_translate',
                        lambda *_args: None)
    return events


def test_incomplete_flow_chat_turn_is_done_but_never_clean_stop(monkeypatch):
    import lib.orchestration_engine as engine
    from lib.orchestration_endpoint_runner import run_endpoint_via_flow

    def never_stop(_self, node, _context, iteration):
        if node.get('role') == 'critic':
            return {
                'output': 'more work [VERDICT: CONTINUE_WORKER]',
                'status': 'completed', 'error': '', 'tool_log': [],
            }
        return {
            'output': f'partial {iteration}',
            'status': 'completed', 'error': '',
            'tool_log': [{'tool': 'write_file'}],
        }

    events = _install_side_effect_stubs(monkeypatch)
    monkeypatch.setattr(engine.FlowExecutor, '_default_runner', never_stop)
    task = _task()
    run_endpoint_via_flow(task)

    assert task['status'] == 'done'
    assert task['finishReason'] == 'incomplete'
    assert task['_endpoint_stop_reason'] == 'max_iterations'
    assert task['_orchestration_outcome']['category'] == 'incomplete'
    done = [event for event in events if event.get('type') == 'done'][-1]
    assert done['finishReason'] == 'incomplete'
    assert done['incomplete'] is True
    assert done['orchestrationOutcome']['stop_reason'] == 'max_iterations'


def test_failed_flow_chat_turn_uses_error_status_and_reason(monkeypatch):
    import lib.orchestration_engine as engine
    from lib.orchestration_endpoint_runner import run_endpoint_via_flow

    def failed_worker(_self, node, _context, _iteration):
        if node.get('role') == 'worker':
            return {
                'output': 'partial work', 'status': 'failed',
                'error': 'worker exploded', 'tool_log': [],
            }
        if node.get('role') == 'critic':
            return {
                'output': '[VERDICT: STOP]', 'status': 'completed',
                'error': '', 'tool_log': [],
            }
        return {
            'output': 'plan', 'status': 'completed',
            'error': '', 'tool_log': [],
        }

    events = _install_side_effect_stubs(monkeypatch)
    monkeypatch.setattr(engine.FlowExecutor, '_default_runner', failed_worker)
    task = _task()
    run_endpoint_via_flow(task)

    assert task['status'] == 'error'
    assert task['finishReason'] == 'error'
    assert task['_endpoint_stop_reason'] == 'node_failed'
    assert task['error']['kind'] == 'generic'
    assert task['error']['detail'] == 'worker exploded'
    assert task['error']['outcome']['category'] == 'failure'
    done = [event for event in events if event.get('type') == 'done'][-1]
    assert done['finishReason'] == 'error'
    assert done['error'] == task['error']
