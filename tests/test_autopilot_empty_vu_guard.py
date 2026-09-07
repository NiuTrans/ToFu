"""tests/test_autopilot_empty_vu_guard.py — VU empty-shell double defect
(pt_be69e7cabef54676, incident conv ms9ow2ttm0gnu0 2026-08-01).

THE INCIDENT
------------
The user clicked Stop while the VU carrier sub-task was mid-call (429 storm,
3 minutes, zero tokens). The abort landed on the CARRIER (the client is
attached to the carrier stream while the VU thinks), NOT on the parent.
``run_virtual_user`` only routed TWO abort shapes to a stop — real-message
preemption and a PARENT abort — so the carrier's empty corpse fell through
the verdict pipeline as a valid "keep going" reply (text=''), and
``_maybe_run_autopilot_inner`` had no empty-text gate: it appended an EMPTY
VU row (``✅ Appended VU msg … (0 chars, 0 rounds)``) and spawned a follow-up
task the user then had to stop AGAIN. The empty row persists forever — no
cleanup path exists (19 conversations affected as of 2026-08-01).

THE TWO GUARDS LOCKED IN HERE
-----------------------------
  ① ``run_virtual_user``: a plain user-stop on the carrier sub-task is a
    failed sub-task → return None (stop the run). The real-message
    preemption branch keeps precedence (it routes to queue dispatch).
  ② ``_maybe_run_autopilot_inner``: an empty cleaned VU text never becomes a
    turn — no append, no follow-up spawn; the run stands down with a cancel
    frame (the marker stays armed, same semantics as the abort paths).
  ③ CONTROL: a substantive reply still appends + spawns (the guards must not
    over-block the healthy loop).

No live LLM / orchestrator / DB.
"""

from __future__ import annotations

import threading

import pytest

pytestmark = pytest.mark.unit

from lib.tasks_pkg import autopilot as ap


def _make_task():
    return {
        'id': 'parent-g-1',
        'convId': 'conv-g',
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


# ── Guard ①: carrier user-stop must stop the run ──────────────────────

def test_carrier_user_abort_returns_none(monkeypatch):
    """A plain user Stop on the VU carrier (no queued real message, parent
    NOT aborted) must make run_virtual_user return None — never fall through
    to the empty 'keep going' reply that appended the ghost row.

    NEGATIVE CONTROL: deleting the ``sub_task.get('aborted')`` branch makes
    this return {'text': '', ...} (the incident shape), failing here.
    """
    import lib.tasks_pkg.orchestrator._turn as orch

    monkeypatch.setattr(ap, '_get_or_persist_objective', lambda cid, msgs, *, user_id: 'obj')
    monkeypatch.setattr(ap, '_has_pending_real_message', lambda cid, *, user_id: False)

    def _fake_turn(sub_task):
        # routes.chat_poll_abort.chat_abort landing on the CARRIER: stamps
        # aborted on the sub-task dict; the round loop exits with no content.
        sub_task['aborted'] = True
        sub_task['toolRounds'] = []
        return {'content': ''}

    monkeypatch.setattr(orch, '_run_single_turn', _fake_turn)

    result = ap.run_virtual_user(_make_task(), vu_msg_id='vu-g-1')

    assert result is None, (
        'an aborted VU carrier must stop the run — its empty corpse is not '
        'a keep-going reply (the ms9ow2tt ghost-row incident)')


def test_carrier_preemption_still_routes_first(monkeypatch):
    """ORDERING: a carrier abort carrying real-message preemption keeps its
    own branch (queue dispatch takes over) — guard ① must not shadow it."""
    import lib.tasks_pkg.orchestrator._turn as orch

    monkeypatch.setattr(ap, '_get_or_persist_objective', lambda cid, msgs, *, user_id: 'obj')
    monkeypatch.setattr(ap, '_has_pending_real_message', lambda cid, *, user_id: True)

    def _fake_turn(sub_task):
        sub_task['aborted'] = True
        sub_task['_abort_reason'] = 'real_message_preempts_vu'
        sub_task['toolRounds'] = []
        return {'content': ''}

    monkeypatch.setattr(orch, '_run_single_turn', _fake_turn)

    assert ap.run_virtual_user(_make_task(), vu_msg_id='vu-g-2') is None


# ── Guard ②: an empty cleaned text never becomes a turn ───────────────

def _wire_inner(monkeypatch, vu_text):
    """Drive maybe_run_autopilot to the post-VU path with a stubbed VU reply;
    return (appended, spawned) call recorders."""
    import lib.tasks_pkg.manager as mgr

    monkeypatch.setattr(mgr, 'append_event', lambda task, event: None)
    monkeypatch.setattr(ap, 'is_autopilot_enabled', lambda task: True)
    monkeypatch.setattr(ap, '_get_or_persist_run_id', lambda cid, *, user_id: 'ar-g')
    monkeypatch.setattr(ap, '_has_pending_real_message', lambda cid, *, user_id: False)
    monkeypatch.setattr(ap, '_successor_already_running', lambda t, c: False)
    monkeypatch.setattr(ap, 'run_virtual_user',
                        lambda task, vu_msg_id=None: {
                            'text': vu_text, 'rounds': [], 'segments': []})
    monkeypatch.setattr(ap, '_record_vu_turn_and_check_budget',
                        lambda c, t, targets=None, *, user_id: {'stop': False})

    appended: list = []
    monkeypatch.setattr(
        ap, '_append_conversation_autopilot_turns',
        lambda *a, **kw: appended.append(1) or {'role': 'user'})
    spawned: list = []
    monkeypatch.setattr(ap, '_start_followup_task',
                        lambda t, c: spawned.append(c) or 'next-id')
    return appended, spawned


@pytest.mark.parametrize('vu_text', [
    '',                 # the incident shape: aborted / degenerate VU reply
    '   ',              # whitespace-only
    '[PROGRESS: resolved=1 remaining=2]',  # machine-token-only → clean=''
])
def test_empty_vu_text_no_append_no_spawn(monkeypatch, vu_text):
    """An empty (or machine-token-only) VU reply must NOT append a ghost row
    and must NOT spawn a follow-up — the run stands down.

    NEGATIVE CONTROL: deleting the empty-text gate lets the flow reach
    atomic baton append + follow-up start, failing here.
    """
    appended, spawned = _wire_inner(monkeypatch, vu_text)

    result = ap.maybe_run_autopilot(_make_task())

    assert result is None
    assert appended == [], 'an empty VU reply must never be persisted as a row'
    assert spawned == [], 'an empty VU reply must never spawn a follow-up task'


def test_control_substantive_reply_still_spawns(monkeypatch):
    """CONTROL (load-bearing complement): a substantive reply still appends +
    spawns — the empty-text gate must not over-block the healthy loop."""
    appended, spawned = _wire_inner(monkeypatch, 'Next, add a regression test.')

    result = ap.maybe_run_autopilot(_make_task())

    assert appended == [1], 'healthy loop must still persist the VU turn'
    assert spawned == ['conv-g'], 'healthy loop must still spawn the follow-up'
    assert result is not None and result.get('next_task_id') == 'next-id'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
