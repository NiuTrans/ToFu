"""tests/test_endpoint_finalize_status.py — _finalize must report the TRUE
terminal state for endpoint runs.

Regression coverage for the bug where ``_finalize`` unconditionally set
``task['status']='done'`` / ``finishReason='stop'`` and dropped
``task['error']`` even when the loop broke out with ``stop_reason='error'``
(a worker-turn LLM failure) or ``'aborted'`` (user/superseded abort) — so a
real failure surfaced to the user/DB as a clean empty completion.

These tests call ``_finalize`` directly on a synthetic task and assert the
status / finishReason / DONE-event shape, with the DB-persist + auto-translate
side effects stubbed out on the module namespace (no DB, no live LLM).
"""

import threading

import pytest

import lib.tasks_pkg.endpoint as ep

pytestmark = pytest.mark.unit


def _make_task(**extra):
    task = {
        'id': 'ep-finalize-test-0001',
        'content_lock': threading.Lock(),
        'content': '',
        'model': 'test-model',
    }
    task.update(extra)
    return task


@pytest.fixture()
def captured_events(monkeypatch):
    """Capture events appended by _finalize; stub DB + translate side effects."""
    events = []
    monkeypatch.setattr(ep, 'append_event', lambda task, event: events.append(event))
    monkeypatch.setattr(ep, 'persist_task_result', lambda task: None)
    monkeypatch.setattr(ep, '_trigger_endpoint_auto_translate', lambda task, turns: None)
    return events


def _done_event(events):
    from lib.agent_core.events import EventType
    dones = [e for e in events if e.get('type') == EventType.DONE]
    assert len(dones) == 1, f'expected exactly one DONE event, got {len(dones)}'
    return dones[0]


def test_finalize_error_reports_error_state_and_carries_envelope(captured_events):
    envelope = {'message': 'upstream LLM 500', 'kind': 'api_error'}
    task = _make_task(error=envelope)

    ep._finalize(task, accumulated_content='', total_usage={}, iteration=2,
                 stop_reason='error', fallback_model=None, fallback_from=None)

    assert task['status'] == 'error'
    assert task['finishReason'] == 'error'
    done = _done_event(captured_events)
    assert done['finishReason'] == 'error'
    assert done['endpointReason'] == 'error'
    assert done.get('error') == envelope, 'DONE event must carry the error envelope'


def test_finalize_aborted_reports_aborted_state(captured_events):
    task = _make_task()

    ep._finalize(task, accumulated_content='partial', total_usage={}, iteration=1,
                 stop_reason='aborted', fallback_model=None, fallback_from=None)

    assert task['status'] == 'aborted'
    assert task['finishReason'] == 'aborted'
    done = _done_event(captured_events)
    assert done['finishReason'] == 'aborted'
    assert done['endpointReason'] == 'aborted'


def test_finalize_success_unchanged(captured_events):
    """A normal completion (approved/max_iterations/etc.) stays done/stop."""
    task = _make_task()

    ep._finalize(task, accumulated_content='the answer', total_usage={}, iteration=3,
                 stop_reason='approved', fallback_model=None, fallback_from=None)

    assert task['status'] == 'done'
    assert task['finishReason'] == 'stop'
    done = _done_event(captured_events)
    assert done['finishReason'] == 'stop'
    assert done['endpointReason'] == 'approved'
    assert 'error' not in done
