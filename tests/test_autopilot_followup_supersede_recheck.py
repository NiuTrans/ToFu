"""tests/test_autopilot_followup_supersede_recheck.py — the FINAL supersede
recheck immediately before ``_start_followup_task``.

WHY
---
``maybe_run_autopilot`` checks ``task['aborted']`` and
``_successor_already_running`` near the TOP (the eligibility gauntlet) and once
more right after the VU LLM call returns. But between that post-VU check and
the actual follow-up spawn it does real, wall-clock-consuming work:

    _append_conversation_autopilot_turns  # atomic durable turn-pair write
    terminal bookkeeping
    _record_vu_turn_and_check_budget # settings bookkeeping

A user action that SUPERSEDES this run — a concurrent regenerate / edit /
send — lands in exactly that window. It calls
``abort_running_tasks_for_conv`` (stamping this parent ``task['aborted']=True``)
and ``create_task`` (registering its OWN task as the conv's latest task).
Without a recheck at the last moment, ``maybe_run_autopilot`` would still call
``_start_followup_task`` → ``create_task``, whose supersede invariant then
ABORTS the user's just-started task — "autopilot snipes the user's regen".

THE FIX (this suite locks it in): re-read BOTH supersede signals immediately
before ``_start_followup_task`` and stand down (return None, no spawn) if
either fires. The already-persisted VU turn stays in history (harmless).

Guards:
  1. aborted flips DURING the window (after the post-VU check) → NO follow-up
     spawn, returns None.
  2. a successor registers as latest DURING the window → NO follow-up spawn,
     returns None.
  3. CONTROL (no supersede) → follow-up IS spawned (proves the recheck does
     not over-block the healthy loop — the load-bearing complement).

No live LLM / orchestrator.
"""

from __future__ import annotations

import threading

import pytest

pytestmark = pytest.mark.unit

from lib.tasks_pkg import autopilot as ap


def _wire(monkeypatch, *, flip=None, successor_flag=None):
    """Drive maybe_run_autopilot to the VU-produced-reply path and stub every
    side-effecting hop so it reaches the final recheck deterministically.

    ``flip`` — optional 0-arg callback invoked from inside
    ``_append_conversation_autopilot_turns`` (after the post-VU abort check,
    inside
    the vulnerable window) to simulate a concurrent supersede landing there.
    ``successor_flag`` — a mutable {'v': bool} read by the stubbed
    ``_successor_already_running`` so the top gauntlet can pass (False) while
    the final recheck sees True.
    """
    import lib.tasks_pkg.manager as mgr

    events: list = []
    monkeypatch.setattr(mgr, 'append_event',
                        lambda task, event: events.append(event))

    monkeypatch.setattr(ap, 'is_autopilot_enabled', lambda task: True)
    monkeypatch.setattr(ap, '_get_or_persist_run_id', lambda cid, *, user_id: 'ar-rc')
    monkeypatch.setattr(ap, '_has_pending_real_message', lambda cid, *, user_id: False)

    if successor_flag is not None:
        monkeypatch.setattr(ap, '_successor_already_running',
                            lambda task, cid: bool(successor_flag['v']))
    else:
        monkeypatch.setattr(ap, '_successor_already_running',
                            lambda task, cid: False)

    # VU produced a real reply.
    monkeypatch.setattr(ap, 'run_virtual_user',
                        lambda task, vu_msg_id=None: {
                            'text': 'keep going', 'rounds': [], 'segments': []})

    # The VU commit hop: fire the injected supersede here (inside the window),
    # then return a persisted-looking vu_msg so the flow continues to the gate.
    def _fake_append(task, conv_id, vu_msg_id, text, rounds=None, run_id='',
                     segments=None):
        if flip is not None:
            flip()
        return {'role': 'user', '_isVirtualUser': True, '_msgId': vu_msg_id,
                'content': text, '_autopilotRunId': run_id}
    monkeypatch.setattr(
        ap, '_append_conversation_autopilot_turns', _fake_append)

    monkeypatch.setattr(ap, '_record_vu_turn_and_check_budget',
                        lambda conv_id, vu_text, targets=None, *, user_id: {'stop': False})

    spawned: list = []
    monkeypatch.setattr(ap, '_start_followup_task',
                        lambda task, conv_id: spawned.append(conv_id) or 'next-task-id')

    return events, spawned


def _make_task():
    return {
        'id': 'parent-rc-1',
        'convId': 'conv-rc',
        '_userId': 1,
        'config': {'model': 'm', 'autopilot': True},
        'messages': [
            {'role': 'user', 'content': 'go'},
            {'role': 'assistant', 'content': 'done'},
        ],
        'events': [],
        'events_lock': threading.Lock(),
        'aborted': False,
        'modifiedFileList': [],
    }


def test_aborted_during_window_stands_down(monkeypatch):
    """The parent task is aborted (by a concurrent regen's
    abort_running_tasks_for_conv) DURING the commit window → the final recheck
    must stand down: no follow-up spawn, returns None."""
    task = _make_task()
    # Flip abort ON from inside the atomic turn-pair append — strictly
    # AFTER the post-VU abort check, inside the vulnerable window.
    events, spawned = _wire(monkeypatch,
                            flip=lambda: task.__setitem__('aborted', True))

    result = ap.maybe_run_autopilot(task)

    assert result is None, 'must not return a follow-up baton after supersede'
    assert spawned == [], \
        '_start_followup_task must NOT be called once the parent was aborted'


def test_successor_registered_during_window_stands_down(monkeypatch):
    """A newer task registers as the conv's latest DURING the window (real-user
    send/regen create_task) → the final recheck sees _successor_already_running
    True and stands down, even though the top gauntlet saw False."""
    task = _make_task()
    flag = {'v': False}
    events, spawned = _wire(monkeypatch,
                            flip=lambda: flag.__setitem__('v', True),
                            successor_flag=flag)

    result = ap.maybe_run_autopilot(task)

    assert result is None
    assert spawned == [], \
        '_start_followup_task must NOT be called once a successor owns the conv'


def test_control_no_supersede_spawns_followup(monkeypatch):
    """CONTROL (load-bearing complement): with NO supersede in the window the
    follow-up IS spawned and the baton returned — proving the recheck does not
    over-block the healthy loop."""
    task = _make_task()
    events, spawned = _wire(monkeypatch)  # no flip, successor stays False

    result = ap.maybe_run_autopilot(task)

    assert spawned == ['conv-rc'], 'healthy loop must still spawn the follow-up'
    assert result is not None and result.get('next_task_id') == 'next-task-id'
    assert task.get('_autopilot_spawned_followup') == 'next-task-id'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
