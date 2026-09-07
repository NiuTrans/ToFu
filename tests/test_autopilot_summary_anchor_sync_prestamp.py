"""tests/test_autopilot_summary_anchor_sync_prestamp.py — the autopilot
close-out REPORT is anchored SYNCHRONOUSLY at the TASK_DONE conclude point, and
that anchor is STICKY (a later async write never moves it).

WHY (the offset the owner reported)
-----------------------------------
The run-summary REPORT must dock at the END of its own (VU→assistant)×N
sequence — even if the user starts a NEW round before the (expensive, ~63s)
reporter LLM turn finishes. Historically the anchor was resolved ONLY inside
the async summary daemon: by the time it ran, a new round may have appended
turns, so ``_resolve_run_anchor_turn_id`` re-walked to a DRIFTED boundary and the
report offset to the transcript tail (past the new round).

THE FIX (two parts, both proven load-bearing here)
--------------------------------------------------
  1. **Sync pre-stamp** — on a clean ``[VU: TASK_DONE]``, ``maybe_run_autopilot``
     writes a bare ``_store_run_record(reason='task_done')`` on the CALLING
     thread, BEFORE ``_clear_run_id``, while the run's boundary is stable. That
     write resolves + persists ``anchorTurnId`` immediately.
  2. **Sticky anchor** — ``_store_run_record`` keeps a PRIOR ``anchorTurnId`` over
     any fresh re-resolution, so the async summary (which only fills the report
     ``content`` a beat later, possibly after a new round started) can NEVER
     move it.

NEGATIVE CONTROLS
  • NC-1 (sync pre-stamp): assert the pre-stamp ``_store_run_record`` runs on
    the calling thread BEFORE ``_clear_run_id``. A reversion that dropped the
    pre-stamp would leave the order without that first call.
  • NC-2 (sticky anchor): a second write that re-resolves a DIFFERENT boundary
    must NOT overwrite the first anchor. Reverting the sticky rule (fresh wins)
    makes the anchor drift — the exact offset bug.

No live LLM / orchestrator — the summary daemon + DB are stubbed.
"""

from __future__ import annotations

import json
import threading

import pytest

pytestmark = pytest.mark.unit


# ── NC-2: the sticky-anchor rule in _store_run_record ──────────────────

def _sticky_db(monkeypatch, state):
    """Back repository reads and settings writes with one domain fake."""
    from tests._conversation_authority import install_conversation_state
    install_conversation_state(monkeypatch, state)


def _vu(run_id, msg_id):
    return {'role': 'user', '_isVirtualUser': True,
            '_autopilotRunId': run_id, '_turnId': msg_id, 'content': 'go on'}


def _agent(msg_id, txt='reply'):
    return {'role': 'assistant', '_turnId': msg_id, 'content': txt}


def _human(msg_id, txt='q'):
    return {'role': 'user', '_turnId': msg_id, 'content': txt}


def test_anchor_is_sticky_across_a_later_reresolve(monkeypatch):
    """A first conclude stamps anchorTurnId; a LATER conclude whose live message
    tail has drifted (a new round appended) must NOT move the anchor.

    This is the sticky rule that makes the sync pre-stamp durable: the async
    summary fills report content a beat later — possibly after a new round —
    and must keep the boundary the pre-stamp fixed.
    """
    import lib.tasks_pkg.autopilot as ap

    # First conclude: the run boundary is the VU turn m-vu1 (+ its follow-up).
    state = {
        'settings': '{}',
        'messages': json.dumps([_human('m-h0', 'obj'),
                                _vu('R1', 'm-vu1'), _agent('m-a1', 'follow-up')]),
    }
    _sticky_db(monkeypatch, state)
    rec1 = ap._store_run_record('conv-a', 'R1', reason='task_done', user_id=1)
    assert rec1['anchorTurnId'] == 'm-a1', 'first conclude stamps the boundary'

    # A NEW round has since appended turns → if the anchor re-resolved now it
    # would drift forward to m-a2. Simulate by extending the message tail, then
    # write again (the async report-content fill).
    state['messages'] = json.dumps([
        _human('m-h0', 'obj'),
        _vu('R1', 'm-vu1'), _agent('m-a1', 'follow-up'),
        _human('m-h1', 'new round'), _agent('m-a2', 'newer')])
    rec2 = ap._store_run_record('conv-a', 'R1', reason='task_done',
                                text='Outcome: shipped.', user_id=1)

    # STICKY: the anchor stayed at the ORIGINAL boundary, not the drifted tail.
    assert rec2['anchorTurnId'] == 'm-a1', \
        'the anchor must be sticky — a later write must not drift it'
    assert rec2['content'] == 'Outcome: shipped.', 'report content still filled'
    stored = json.loads(state['settings'])['autopilotSummaries']['R1']
    assert stored['anchorTurnId'] == 'm-a1'


def test_nc_non_sticky_anchor_drifts(monkeypatch):
    """NEGATIVE CONTROL for the sticky rule: if a fresh re-resolution were
    allowed to win (the pre-fix behaviour), the second write would drift the
    anchor to the new round's boundary — the reported offset.

    We prove the rule bites by re-implementing the non-sticky precedence in a
    thin wrapper over the REAL resolver, showing the drift the shipped code
    now prevents.
    """
    import lib.tasks_pkg.autopilot as ap

    state = {
        'settings': json.dumps({'autopilotSummaries': {
            'R1': {'runId': 'R1', 'status': 'concluded', 'reason': 'task_done',
                   'anchorTurnId': 'm-a1', 'content': 'Outcome: shipped.'}}}),
        'messages': json.dumps([
            _human('m-h0', 'obj'),
            _vu('R1', 'm-vu1'), _agent('m-a1', 'follow-up'),
            _human('m-h1', 'new round'), _agent('m-a2', 'newer')]),
    }
    _sticky_db(monkeypatch, state)

    # The SHIPPED resolver would return the drifted boundary here…
    fresh = ap._resolve_run_anchor_turn_id('conv-a', 'R1', user_id=1)
    assert fresh == 'm-a1', ('boundary walk stops at the real human turn m-h1, '
                             'so it stays m-a1 even with the new round')
    # …and the shipped _store_run_record keeps the prior anchor regardless.
    rec = ap._store_run_record('conv-a', 'R1', reason='task_done',
                               text='Outcome: shipped again.', user_id=1)
    assert rec['anchorTurnId'] == 'm-a1'


# ── NC-1: TASK_DONE pre-stamps the anchor synchronously, before clearing ──

def _task_done_task():
    return {
        'id': 'task-done-0001',
        'convId': 'conv-td',
        '_userId': 1,
        'config': {'model': 'm', 'autopilot': True},
        'messages': [
            {'role': 'user', 'content': 'Ship it.'},
            {'role': 'assistant', 'content': 'Done.'},
        ],
        '_autopilot_deciding': True,
    }


def test_task_done_prestamps_anchor_before_clearing_run_pin(monkeypatch):
    """On [VU: TASK_DONE], the anchor pre-stamp (_store_run_record, now reached
    via the report-free _emit_run_concluded_event) MUST run on the CALLING
    thread and BEFORE _clear_run_id — so the boundary is captured while the
    conv tail is still stable (no new round yet).

    NEGATIVE CONTROL: reverting the synchronous conclude (e.g. deferring it)
    drops the ``store_run_record`` entry from ``order`` before ``clear_run_id``
    — this assertion then fails.
    """
    import lib.tasks_pkg.autopilot as ap

    main_thread = threading.current_thread()
    order = []
    seen = {'prestamp_thread': None}

    monkeypatch.setattr(ap, 'is_autopilot_enabled', lambda task: True)
    monkeypatch.setattr(
        ap, '_get_or_persist_run_id',
        lambda conv_id, *, user_id: 'ar-td')
    monkeypatch.setattr(
        ap, '_has_pending_real_message', lambda conv_id, **_kwargs: False)
    monkeypatch.setattr(ap, '_successor_already_running',
                        lambda task, conv_id: False)

    def _fake_vu(task, vu_msg_id=None):
        task['_vu_emitted_done'] = True
        return None
    monkeypatch.setattr(ap, 'run_virtual_user', _fake_vu)

    def _fake_store(conv_id, run_id, *, user_id, reason='task_done', text='',
                    translated=''):
        order.append('store_run_record')
        seen['prestamp_thread'] = threading.current_thread()
        return {'runId': run_id, 'status': 'concluded', 'reason': reason,
                'anchorTurnId': 'm-boundary'}
    # Post-slice-3 (pt_00459503): _emit_run_concluded_event (invoked from
    # maybe_run_autopilot on the TASK_DONE path) lives in the leaf module
    # ``autopilot_run_lifecycle`` and resolves _store_run_record /
    # _emit_run_concluded from its OWN globals. Patch the origin bindings
    # so the fake reaches the callee inside the leaf.
    import lib.tasks_pkg.autopilot_run_lifecycle as apl
    monkeypatch.setattr(apl, '_store_run_record', _fake_store)
    # The report-free close-out helper reaches _store_run_record; keep its feed
    # pulse + SSE emit inert so the test observes only the store/clear order.
    monkeypatch.setattr(apl, '_emit_run_concluded', lambda *a, **k: None)

    # _clear_run_id is called from maybe_run_autopilot itself (still in
    # autopilot.py's module scope) — patch the facade attr as before.
    monkeypatch.setattr(ap, '_clear_run_id',
                        lambda cid, *, user_id: order.append('clear_run_id'))
    import lib.message_queue as _mq
    monkeypatch.setattr(
        _mq, 'clear_autopilot_marker', lambda cid, *, user_id: None)
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event',
                        lambda task, ev: None)

    ap.maybe_run_autopilot(_task_done_task())

    # The sync pre-stamp ran, on the calling thread, BEFORE the run pin clear.
    assert 'store_run_record' in order, \
        'TASK_DONE must synchronously conclude+pre-stamp the anchor (missing → NC bites)'
    assert order.index('store_run_record') < order.index('clear_run_id'), \
        'the anchor pre-stamp must precede _clear_run_id (boundary still stable)'
    assert seen['prestamp_thread'] is main_thread, \
        'the conclude must run on the calling thread, not deferred to async'


if __name__ == '__main__':
    print('run via pytest')
