"""tests/test_timer_poll_event_suppression.py — Timer poll event suppression.

Covers the 2026-06-28 fix for the ``event_id collision … cold replay will be
missing this event`` log storm (4000+ hits, all on ``tmr_*`` tasks).

Root cause: ``_execute_poll_tool`` built a throwaway ``task_proxy`` keyed on
the *timer id* with a fresh ``events: []`` list but WITHOUT ``_suppressEvents``.
The proxy is never registered in the chat TaskRuntime, so any tool that calls
``_finalize_tool_round`` → ``manager.append_event`` took the legacy fallback,
minting ``seq = len(task['events'])`` (= 0, 1, …) on every poll and persisting
rows keyed ``(timer_id, 0)`` / ``(timer_id, 1)`` into ``task_events``. The next
poll re-minted the same seqs → composite-PK collision → ``ON CONFLICT DO
NOTHING`` dropped the row and tripped the data-loss canary on every poll.

The fix mirrors the swarm sub-agent proxy: set ``_suppressEvents=True`` so a
poll's tool events are never appended/persisted (there is no SSE consumer for
a poll — the UI renders the per-poll timeline from ``tool_trace`` instead).

These tests drive the REAL ``_execute_poll_tool`` with a stubbed
``_execute_tool_one`` that emits a genuine event through the production
``append_event`` path, and assert (a) the proxy carries the suppression flag
and (b) two consecutive polls persist ZERO rows under the timer id (so the
collision condition can never arise). Reverting the one-line fix makes the
second assertion fail (poll 1 persists seq 0/1, poll 2 collides).

Uses the session SQLite DB from conftest (TOFU_DB_PATH) — no PG needed.
"""

import uuid

import pytest

import lib.scheduler.timer as timer_mod
from lib.tasks_pkg.event_log import read_events

pytestmark = pytest.mark.unit


def _fake_tool_call():
    return {
        'id': 'tc_' + uuid.uuid4().hex[:8],
        'function': {'name': 'run_command', 'arguments': '{"command": "echo hi"}'},
    }


def test_poll_tool_proxy_sets_suppress_events(monkeypatch):
    """The task_proxy handed to the executor must carry _suppressEvents=True."""
    captured = {}

    def _spy_execute_tool_one(task, tc, fn_name, tc_id, fn_args, rn,
                              round_entry, cfg, project_path, project_enabled):
        captured['suppress'] = task.get('_suppressEvents')
        captured['task_id'] = task.get('id')
        return (None, 'tool ok', None)

    import lib.tasks_pkg.executor as _ex
    monkeypatch.setattr(_ex, '_execute_tool_one', _spy_execute_tool_one, raising=True)

    timer_id = 'tmr_' + uuid.uuid4().hex[:8]
    result, elapsed, is_err = timer_mod._execute_poll_tool(
        _fake_tool_call(), timer_id, project_path='')

    assert is_err is False
    assert result == 'tool ok'
    assert captured['task_id'] == timer_id
    assert captured['suppress'] is True, (
        'timer poll task_proxy must set _suppressEvents=True so tool events '
        'are not persisted under the timer id (collision data-loss canary)')


def test_poll_tool_emits_no_persisted_events_across_polls(monkeypatch):
    """Two consecutive polls persist ZERO task_events rows → no PK collision.

    The stubbed executor emits a real tool_result through the production
    ``append_event`` path. With suppression the event is dropped before
    persistence; without it, poll 1 would write (timer_id, 0/1) and poll 2
    would collide. We assert the durable store stays empty either way the
    fix could regress.
    """
    from lib.tasks_pkg.manager import append_event

    def _emitting_execute_tool_one(task, tc, fn_name, tc_id, fn_args, rn,
                                   round_entry, cfg, project_path, project_enabled):
        # Mimic a real handler finalizing a tool round on the proxy.
        append_event(task, {'type': 'tool_progress', 'roundNum': 0, 'chunk': 'x'})
        append_event(task, {'type': 'tool_result', 'roundNum': 0, 'results': 'done'})
        return (None, 'tool ok', None)

    import lib.tasks_pkg.executor as _ex
    monkeypatch.setattr(_ex, '_execute_tool_one', _emitting_execute_tool_one, raising=True)

    timer_id = 'tmr_' + uuid.uuid4().hex[:8]

    for _poll in range(2):
        timer_mod._execute_poll_tool(_fake_tool_call(), timer_id, project_path='')

    persisted = read_events(timer_id)
    assert persisted == [], (
        'timer poll must not persist any task_events rows under the timer id; '
        f'found {len(persisted)} — suppression regressed and cold-replay '
        'collisions will resume')
