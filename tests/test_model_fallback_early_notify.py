#!/usr/bin/env python3
"""Model-fallback EARLY notification — backend wiring guard.

WHY: a single turn can run for a very long time. When the primary model
errors and ``_llm_call_with_fallback`` silently switches to the configured
fallback model, the only signals today are
  * a TRANSIENT ``phase=retrying`` line — replaced/cleared the moment the
    fallback model starts streaming content (``setStreamPhase(null)``), and
  * the SETTLED finish-tag — which only appears after the whole turn ends.
For the entire (potentially very long) fallback generation the user has NO
indication the model changed. The fix: emit a dedicated, structured
``model_fallback`` SSE event AT THE DECISION MOMENT (before the fallback
stream starts), stamp the task fields EARLY (so a cold-reload state
snapshot mid-fallback-round still carries them), and CLEAR them again if
the fallback itself fails (a done event must never claim a fallback that
produced nothing).

Failing-first / NEUTER discipline:
  * ``test_model_fallback_event_emitted_before_fallback_stream`` is RED
    without the new ``append_event`` block in ``_call.py``.
  * ``test_task_fields_stamped_at_decision_time`` is RED while the stamping
    stays after the fallback stream (an observer DURING the stream sees
    nothing) — proven by asserting the fields are already set at the moment
    the fallback stream is entered.
  * ``test_task_fields_cleared_when_fallback_fails`` is RED without the
    clear-on-failure branch.
  * ``test_state_snapshot_includes_fallback_fields`` is RED without the
    ``build_fresh_state_snapshot`` block.

Run directly (env-guarded):
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python tests/test_model_fallback_early_notify.py
"""
from __future__ import annotations

import os
import sys
import threading as _thr

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

_PRIMARY = 'aws.claude-opus-4.8'
_FALLBACK = 'aws.claude-opus-4.1'


def _task(conv_id='mfb1'):
    return {'id': 'task-mfb-1', 'convId': conv_id, 'content': '',
            'thinking': '', 'config': {}, 'events': [],
            'status': 'running', 'error': None, 'toolRounds': [],
            'content_lock': _thr.Lock(), 'events_lock': _thr.Lock()}


def _body():
    return {'model': _PRIMARY,
            'messages': [{'role': 'system', 'content': 'S'},
                         {'role': 'user', 'content': 'go'}]}


def _drive(monkeypatch, *, tool_call_happened, fallback_outcome):
    """Drive _llm_call_with_fallback with a primary that always raises and a
    scripted fallback outcome ('ok' or 'raise').

    Returns (task, result_or_none, raised_or_none, events, flags) where
    ``events`` is the append_event recorder and ``flags['fields_at_stream']``
    captures whether the task carried the fallback fields AT THE MOMENT the
    fallback stream was entered (the early-stamp assertion).
    """
    import lib.tasks_pkg.llm_fallback as _fb

    monkeypatch.setattr(_fb, '_get_fallback_model', lambda task=None: _FALLBACK)

    events = []
    monkeypatch.setattr(_fb, 'append_event',
                        lambda task, ev: events.append(dict(ev)))
    monkeypatch.setattr(_fb, '_emit_round_usage', lambda *a, **k: None)
    monkeypatch.setattr(_fb, '_flag_empty_stop_for_retry',
                        lambda *a, **k: False)

    flags = {'fields_at_stream': None}
    calls = {'n': 0}

    def _fake_stream(task, body, tag=None, on_tool_call_ready=None):
        i = calls['n']
        calls['n'] += 1
        if i == 0:
            raise RuntimeError('primary exploded (500 upstream)')
        # Fallback attempt: snapshot the task's fallback fields NOW — this is
        # the only honest probe of "did the user-visible state exist DURING
        # the (long) fallback generation".
        flags['fields_at_stream'] = (
            task.get('_fallback_model'), task.get('_fallback_from'),
            task.get('_fallback_reason'), task.get('_fallback_kind'))
        if fallback_outcome == 'raise':
            raise RuntimeError('fallback exploded too (502)')
        return ({'role': 'assistant', 'content': 'recovered'}, 'stop',
                {'prompt_tokens': 10, 'completion_tokens': 5})

    monkeypatch.setattr(_fb, 'stream_llm_response', _fake_stream)

    task = _task()
    result, raised = None, None
    try:
        result = _fb._llm_call_with_fallback(
            task, _body(), _PRIMARY, 0, 4096,
            tool_call_happened=tool_call_happened, tool_list=None,
            max_tool_rounds=10, messages=_body()['messages'], preset='opus',
            thinking_enabled=False,
            accumulated_usage={}, api_rounds=[],
            on_tool_call_ready=None,
        )
    except Exception as e:  # noqa: BLE001 — asserted by the caller
        raised = e
    return task, result, raised, events, flags


def test_model_fallback_event_emitted_before_fallback_stream(monkeypatch):
    """The structured ``model_fallback`` event must fire AT THE DECISION
    MOMENT — before the fallback stream starts — so the frontend can paint
    the in-bubble banner for the ENTIRE fallback generation, not after it."""
    _task_, result, raised, events, _flags = _drive(
        monkeypatch, tool_call_happened=False, fallback_outcome='ok')
    assert raised is None and result and result['model'] == _FALLBACK

    fb_events = [e for e in events if e.get('type') == 'model_fallback']
    assert len(fb_events) == 1, (
        f'exactly one structured model_fallback event expected; got {fb_events}')
    ev = fb_events[0]
    assert ev.get('fallbackModel') == _FALLBACK
    assert ev.get('fallbackFrom') == _PRIMARY
    assert ev.get('fallbackKind'), f'event must carry the typed kind; got {ev}'
    assert ev.get('fallbackReason'), f'event must carry a reason; got {ev}'

    # ORDER is the point ("as early as possible"): the event must precede
    # the fallback stream's first usage/round event. The decision-time phase
    # event (retrying) is our anchor — the structured event must sit with it,
    # BEFORE any FALLBACK-tagged round usage would exist. Since we recorded
    # every append_event in order, assert no event at all mentions the
    # fallback model BEFORE our structured event was appended.
    idx = events.index(ev)
    assert idx <= 2, (
        f'model_fallback must be emitted at decision time (event index '
        f'{idx} of {len(events)}), not after the fallback round completes')


def test_task_fields_stamped_at_decision_time(monkeypatch):
    """The task must carry the fallback fields DURING the fallback stream —
    not only after it lands — so the state snapshot (cold reload) and any
    mid-stream observer can surface the switch while it is happening."""
    task, result, raised, _events, flags = _drive(
        monkeypatch, tool_call_happened=False, fallback_outcome='ok')
    assert raised is None and result
    assert flags['fields_at_stream'] == (
        _FALLBACK, _PRIMARY, flags['fields_at_stream'][2],
        flags['fields_at_stream'][3]), (
        f'fallback fields must be stamped BEFORE the fallback stream; '
        f'observed at stream entry: {flags["fields_at_stream"]}')
    assert flags['fields_at_stream'][2], 'reason must be non-empty at stream entry'
    assert flags['fields_at_stream'][3], 'kind must be non-empty at stream entry'
    # And the settled invariant is unchanged: fields persist after success.
    assert task['_fallback_model'] == _FALLBACK
    assert task['_fallback_from'] == _PRIMARY


def test_task_fields_cleared_when_fallback_fails(monkeypatch):
    """A fallback that ALSO fails must not leave the stamp behind — the done
    event / persist meta reads ``task['_fallback_model']`` to claim a
    fallback happened; claiming one that produced nothing is a lie."""
    # Branch A: prior tool calls → returns an error dict (no raise).
    task, result, raised, _e, _f = _drive(
        monkeypatch, tool_call_happened=True, fallback_outcome='raise')
    assert raised is None and result and result['finish_reason'] == 'error'
    for k in ('_fallback_model', '_fallback_from', '_fallback_reason',
              '_fallback_kind'):
        assert k not in task, (
            f'{k} must be cleared when the fallback fails; task still has '
            f'{task.get(k)!r}')

    # Branch B: no prior tool calls → exception propagates.
    task2, _r2, raised2, _e2, _f2 = _drive(
        monkeypatch, tool_call_happened=False, fallback_outcome='raise')
    assert raised2 is not None, 'both-failed with no tool calls must raise'
    for k in ('_fallback_model', '_fallback_from', '_fallback_reason',
              '_fallback_kind'):
        assert k not in task2, (
            f'{k} must be cleared when the fallback fails (raise branch); '
            f'task still has {task2.get(k)!r}')


def test_state_snapshot_includes_fallback_fields():
    """Cold reload mid-fallback-round: build_fresh_state_snapshot must fold
    the early-stamped task fields into the state event so the banner
    repaints after a page refresh DURING the fallback generation."""
    from lib.chat_dispatch import build_fresh_state_snapshot

    task = _task('mfb-snap')
    task['_fallback_model'] = _FALLBACK
    task['_fallback_from'] = _PRIMARY
    task['_fallback_reason'] = 'upstream_5xx: 500 exploded'
    task['_fallback_kind'] = 'upstream_5xx'

    state, _meta, _cursor = build_fresh_state_snapshot(task)
    assert state.get('fallbackModel') == _FALLBACK, (
        f'state snapshot must carry fallbackModel; keys={sorted(state)}')
    assert state.get('fallbackFrom') == _PRIMARY
    assert state.get('fallbackReason') == 'upstream_5xx: 500 exploded'
    assert state.get('fallbackKind') == 'upstream_5xx'

    # Absent when no fallback fired (no phantom banner after a normal turn).
    task2 = _task('mfb-snap2')
    state2, _m2, _c2 = build_fresh_state_snapshot(task2)
    assert 'fallbackModel' not in state2, (
        f'no fallback → no fallbackModel in snapshot; got {state2.get("fallbackModel")!r}')


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v', '-p', 'no:cacheprovider']))
