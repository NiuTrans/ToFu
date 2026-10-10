"""Resource contract for indefinitely waitable dispatch retry telemetry."""

import threading

import pytest

from lib.llm_dispatch.retry_i18n import RetryPhaseEventBudget


pytestmark = pytest.mark.unit


def test_one_retry_signature_has_a_strict_logarithmic_event_budget():
    budget = RetryPhaseEventBudget()

    emitted = [
        cycle
        for cycle in range(1, 100_001)
        if budget.should_emit(('dispatch_retry', 'Rate limited (429)', 429))
    ]

    assert emitted == [1 << exponent for exponent in range(16)]
    assert len(emitted) == RetryPhaseEventBudget.MAX_EVENTS_PER_SIGNATURE
    assert len(emitted) <= budget.maximum_events


def test_many_retry_signatures_cannot_grow_the_budget_map_or_event_log():
    budget = RetryPhaseEventBudget()
    emitted = 0

    for signature_index in range(100):
        signature = ('dispatch_retry', f'unknown-{signature_index}', 0)
        for _ in range(100_000):
            emitted += int(budget.should_emit(signature))

    assert emitted == budget.maximum_events


def test_main_chat_keeps_liveness_exact_while_persisting_bounded_retry_phases(
        monkeypatch):
    """The production callback boundary, not only the sampler, is bounded."""
    import lib.tasks_pkg.manager._stream as stream_module

    task = {
        'id': 'retry-budget-task',
        'convId': '',
        '_userId': 1,
        'status': 'running',
        'content': '',
        'thinking': '',
        'config': {'userId': 1},
        'events': [],
        'toolRounds': [],
        'content_lock': threading.Lock(),
        'events_lock': threading.Lock(),
        '_dispatch_heartbeat': 0.0,
    }

    def append_in_memory(current_task, event):
        current_task['events'].append(event)

    def retry_storm(_body, **kwargs):
        on_retry = kwargs['on_retry']
        for cycle in range(1, 100_001):
            on_retry(
                cycle,
                reason='Rate limited (429)',
                status_code=429,
            )
        return ({'role': 'assistant', 'content': 'ok'}, 'stop', {})

    monkeypatch.setattr(stream_module, 'append_event', append_in_memory)
    monkeypatch.setattr(stream_module, 'dispatch_stream', retry_storm)

    stream_module.stream_llm_response(
        task,
        {'model': 'kimi-k3', 'messages': [{'role': 'user', 'content': 'go'}]},
        tag='budget',
    )

    retry_events = [
        event for event in task['events']
        if event.get('type') == 'phase'
        and event.get('phase') == 'retrying'
        and event.get('statusCode') == 429
    ]
    assert len(retry_events) == RetryPhaseEventBudget.MAX_EVENTS_PER_SIGNATURE
    assert retry_events[-1]['attempt'] == 1 << 15
    assert task['_dispatch_heartbeat'] > 0.0
