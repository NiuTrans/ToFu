"""Behaviour contract for final provider tool-schema tracking.

The transport boundary emits the exact-schema fingerprint through the
request-scoped diagnostic sink.  The task stream must persist that digest on a
bounded ``tool_wire_projection`` event and remove the callable sidecar after
the model request settles.  This makes round-to-round schema drift observable
without storing full provider schemas or leaking diagnostics onto the wire.
"""

from __future__ import annotations

import threading

import pytest

pytestmark = pytest.mark.unit


def _task():
    return {
        'id': 'wire-projection-tracking-task',
        'convId': 'wire-projection-tracking-conv',
        '_userId': 1,
        'status': 'running',
        'content': '',
        'thinking': '',
        'config': {'userId': 1},
        'events': [],
        'toolRounds': [],
        'content_lock': threading.Lock(),
        'events_lock': threading.Lock(),
    }


def test_stream_persists_bounded_final_schema_fingerprint(monkeypatch):
    import lib.tasks_pkg.manager._stream as stream_module

    events = []
    fingerprint = 'a' * 80

    def fake_dispatch(body, **_kwargs):
        sink = body.get('_request_activity_sink')
        assert callable(sink)
        sink({
            'kind': 'wire_projection',
            'model': 'kimi-k3',
            'backend': 'local',
            'toolNames': ['read_files', 'execute_tools'],
            'toolCount': 2,
            'schemaTokens': 480,
            'schemaFingerprint': fingerprint,
            'schemaBudgetTokens': 0,
            'budgetDroppedNames': [],
            'compactedNames': [],
            'executableToolCount': 12,
        })
        return (
            {'role': 'assistant', 'content': 'ok', 'reasoning_content': ''},
            'stop',
            {'prompt_tokens': 10, 'completion_tokens': 1},
        )

    monkeypatch.setenv('TOFU_CACHE_FLOOR_RETRY', '0')
    monkeypatch.setattr(stream_module, 'dispatch_stream', fake_dispatch)
    monkeypatch.setattr(
        stream_module, 'append_event',
        lambda _task_value, event: events.append(event))
    monkeypatch.setattr(
        stream_module, 'checkpoint_task_partial', lambda _task_value: None)

    body = {
        'model': 'kimi-k3',
        'messages': [{'role': 'user', 'content': 'inspect'}],
    }
    stream_module.stream_llm_response(_task(), body, tag='R4')

    projection = next(
        event for event in events if event.get('type') == 'tool_wire_projection')
    assert projection['roundNum'] == 4
    assert projection['toolNames'] == ['read_files', 'execute_tools']
    assert projection['schemaFingerprint'] == fingerprint[:64]
    assert projection['schemaTokens'] == 480
    assert '_request_activity_sink' not in body

