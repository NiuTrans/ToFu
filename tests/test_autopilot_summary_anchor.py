"""tests/test_autopilot_summary_anchor.py — the autopilot close-out run record
carries a BACKEND-RESOLVED ``anchorTurnId``: the stable ``_turnId`` of the run's
boundary turn (the last turn the run produced).

WHY (the root cause this fixes)
-------------------------------
Historically the run record (``settings.autopilotSummaries[runId]``) carried NO
anchor, so the FRONTEND had to re-derive each report's placement by scanning
``_autopilotRunId`` stamps in a positional renderer. When a run's stamped turn
wasn't in the loaded
window (compaction / lazy window), the frontend fell back to "dock at the
transcript tail" — and EVERY such run tail-docked, so reports from DIFFERENT
runs piled up together. That is a frontend INFERENCE of a fact the backend
owns.

THE FIX (backend authority)
---------------------------
At conclude time the backend resolves the run's boundary turn server-side —
where the run's turns are known — and stamps its stable ``_turnId`` as
``record['anchorTurnId']``. The boundary is the last turn belonging to the run:
the run's VU turn, EXTENDED forward over the trailing unstamped agent
follow-up(s) it prompted, stopping at the next run's VU turn / a real human
turn / end-of-list (the same rule the frontend heuristic used, now computed
once, server-side, on a stable id — never an array index).

Covered here:
  • ``_resolve_run_anchor_turn_id`` — VU-only run, follow-up extension, boundary
    stops at the next run / a human turn, unresolvable → '', a boundary turn
    lacking ``_turnId`` → '' (can't anchor without a stable id).
  • ``_store_run_record`` / ``conclude_run`` stamp ``anchorTurnId`` on BOTH
    close-out paths (the single chokepoint), and never when unresolvable.
"""

import json

import pytest

pytestmark = pytest.mark.unit


def _msgs_db(monkeypatch, messages_json: str):
    """Wire the repository seam to the given projected transcript."""
    from tests._conversation_authority import install_conversation_state
    install_conversation_state(
        monkeypatch, {'messages': messages_json, 'settings': '{}'})


def _vu(run_id, msg_id):
    return {'role': 'user', '_isVirtualUser': True,
            '_autopilotRunId': run_id, '_turnId': msg_id, 'content': 'go on'}


def _agent(msg_id, txt='reply'):
    return {'role': 'assistant', '_turnId': msg_id, 'content': txt}


def _human(msg_id, txt='q'):
    return {'role': 'user', '_turnId': msg_id, 'content': txt}


# ── _resolve_run_anchor_turn_id ──────────────────────────────────────────

def test_resolve_run_anchor_vu_only(monkeypatch):
    import lib.tasks_pkg.autopilot as ap
    msgs = [_human('m-h0', 'obj'), _vu('R1', 'm-vu1')]
    _msgs_db(monkeypatch, json.dumps(msgs))
    assert ap._resolve_run_anchor_turn_id('conv-a', 'R1', user_id=1) == 'm-vu1'


def test_resolve_run_anchor_extends_over_followup(monkeypatch):
    """The boundary extends past the VU turn over the unstamped agent
    follow-up it prompted — the anchor is the follow-up's _turnId."""
    import lib.tasks_pkg.autopilot as ap
    msgs = [_human('m-h0', 'obj'), _agent('m-a0', 'a1'),
            _vu('R1', 'm-vu1'), _agent('m-a1', 'follow-up')]
    _msgs_db(monkeypatch, json.dumps(msgs))
    assert ap._resolve_run_anchor_turn_id('conv-a', 'R1', user_id=1) == 'm-a1'


def test_resolve_run_anchor_stops_at_next_run(monkeypatch):
    """Two back-to-back runs: each anchor is its OWN boundary — they can never
    collapse onto one turn."""
    import lib.tasks_pkg.autopilot as ap
    msgs = [_human('m-h0', 'obj'),
            _vu('R1', 'm-vu1'), _agent('m-a1'),
            _vu('R2', 'm-vu2'), _agent('m-a2')]
    _msgs_db(monkeypatch, json.dumps(msgs))
    assert ap._resolve_run_anchor_turn_id('conv-a', 'R1', user_id=1) == 'm-a1'
    assert ap._resolve_run_anchor_turn_id('conv-a', 'R2', user_id=1) == 'm-a2'


def test_resolve_run_anchor_stops_at_human(monkeypatch):
    """A real (non-VU) human turn begins a new exchange — the run's boundary
    stops before it, not at end-of-list."""
    import lib.tasks_pkg.autopilot as ap
    msgs = [_vu('R1', 'm-vu1'), _agent('m-a1'),
            _human('m-h1', 'new q'), _agent('m-a2')]
    _msgs_db(monkeypatch, json.dumps(msgs))
    assert ap._resolve_run_anchor_turn_id('conv-a', 'R1', user_id=1) == 'm-a1'


def test_resolve_run_anchor_unresolvable_returns_empty(monkeypatch):
    import lib.tasks_pkg.autopilot as ap
    msgs = [_human('m-h0', 'obj'), _vu('R1', 'm-vu1')]
    _msgs_db(monkeypatch, json.dumps(msgs))
    assert ap._resolve_run_anchor_turn_id('conv-a', 'R-absent', user_id=1) == ''


def test_resolve_run_anchor_boundary_without_turn_id_returns_empty(monkeypatch):
    """A boundary turn lacking a stable _turnId cannot be anchored — return ''
    (obeys the stream-target-resolution-by-turn_id convention: never fabricate an
    index anchor)."""
    import lib.tasks_pkg.autopilot as ap
    msgs = [{'role': 'user', '_isVirtualUser': True, '_autopilotRunId': 'R1',
             'content': 'go on'}]  # no _turnId
    _msgs_db(monkeypatch, json.dumps(msgs))
    assert ap._resolve_run_anchor_turn_id('conv-a', 'R1', user_id=1) == ''


# ── _store_run_record / conclude_run stamp the anchor (both paths) ─────

def _anchor_store_db(monkeypatch, state):
    """Back repository reads and settings writes with one domain fake."""
    from tests._conversation_authority import install_conversation_state
    install_conversation_state(monkeypatch, state)


def test_store_run_record_stamps_anchor_turn_id(monkeypatch):
    """_store_run_record resolves the run's boundary turn server-side and stamps
    its _turnId as anchorTurnId on the persisted record."""
    import lib.tasks_pkg.autopilot as ap
    msgs = [_human('m-h0', 'obj'),
            _vu('R1', 'm-vu1'), _agent('m-a1', 'follow-up')]
    state = {'settings': '{}', 'messages': json.dumps(msgs)}
    _anchor_store_db(monkeypatch, state)

    rec = ap._store_run_record('conv-a', 'R1', reason='task_done',
                               text='Outcome: shipped.', user_id=1)
    assert rec is not None
    assert rec['anchorTurnId'] == 'm-a1', 'anchor = the follow-up boundary turn'

    stored = json.loads(state['settings'])['autopilotSummaries']['R1']
    assert stored['anchorTurnId'] == 'm-a1'


def test_conclude_run_stamps_anchor_turn_id(monkeypatch):
    """The manual-stop path (conclude_run → _store_run_record) also stamps the
    anchor — both close-out paths carry the backend fact."""
    import lib.tasks_pkg.autopilot as ap
    msgs = [_human('m-h0', 'obj'), _vu('R1', 'm-vu1')]
    state = {'settings': json.dumps({'autopilotRunId': 'R1'}),
             'messages': json.dumps(msgs)}
    _anchor_store_db(monkeypatch, state)

    rec = ap.conclude_run('conv-a', reason='stopped', user_id=1)
    assert rec is not None
    assert rec['runId'] == 'R1'
    assert rec['anchorTurnId'] == 'm-vu1', 'VU-only run anchors on the VU turn'


def test_store_run_record_no_anchor_when_unresolvable(monkeypatch):
    """When the run's turns aren't on disk (nothing to anchor to), the record
    carries NO anchorTurnId — the frontend then uses the ts-tail last resort."""
    import lib.tasks_pkg.autopilot as ap
    state = {'settings': '{}', 'messages': json.dumps([_human('m-h0', 'obj')])}
    _anchor_store_db(monkeypatch, state)

    rec = ap._store_run_record('conv-a', 'R-absent', reason='task_done',
                               text='report', user_id=1)
    assert rec is not None
    assert 'anchorTurnId' not in rec


if __name__ == '__main__':
    print('run via pytest')
