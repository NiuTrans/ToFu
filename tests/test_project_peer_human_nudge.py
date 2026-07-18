"""tests/test_project_peer_human_nudge.py — Pillar #6 UX: the HUMAN operator nudge.

The Team panel gains a "Nudge" affordance: the operator (acting via their
displayed conversation) sends an advisory note to a sibling conversation. It
reuses ``send_peer_message`` — the SINGLE messaging seam — with ``human=True``,
which must:

  • frame the target's turn as OPERATOR guidance (not an agent peer's advisory
    opinion), and stamp the enqueued payload ``_peerHuman`` so the receiving
    banner attributes it to the operator;
  • stamp the mirrored feed row ``kind='operator'`` (+ ``human=True`` in the
    payload) for provenance;
  • keep the SAME per-(sender,target) rate limit + self-send refusal — the
    human path is NOT a storm bypass;
  • and the ``_peerHuman`` marker must PROPAGATE onto the persisted turn in
    ``dispatch_next_queued`` (else the operator arrival is byte-identical to an
    agent peer note and the UI can't distinguish it).

DB-free: the queue + feed + audit deps are monkeypatched, mirroring
``tests/test_project_peer.py``.

Two MANDATORY byte-reverting negative controls:
  • NC-HUMAN-PAYLOAD: drop the ``_peerHuman`` payload stamp in
    ``send_peer_message`` → the human-attribution test FAILS.
  • NC-DISPATCH-HUMAN: no-op the ``_peerHuman`` propagation in
    ``dispatch_next_queued`` → the persisted turn loses the operator marker →
    the propagation test FAILS.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_PEER_SRC = os.path.join(ROOT, 'lib', 'conversations', 'project_peer.py')
_MQ_SRC = os.path.join(ROOT, 'lib', 'message_queue.py')


@pytest.fixture(autouse=True)
def _reset_rate_history():
    import lib.conversations.project_peer as pp
    with pp._rate_lock:
        pp._peer_msg_history.clear()
    yield
    with pp._rate_lock:
        pp._peer_msg_history.clear()


@pytest.fixture
def _stub_io(monkeypatch):
    """Capture enqueue + feed emits; audit is a no-op. send_peer_message is
    then DB-free. Returns {'enqueue': [...], 'feed': [...]}."""
    calls = {'enqueue': [], 'feed': []}

    def _fake_enqueue(conv_id, message_data, config, kind='real'):
        calls['enqueue'].append({'conv_id': conv_id, 'kind': kind,
                                 'payload': message_data, 'config': config})
        return {'queueId': 'q_' + conv_id[:6], 'position': 1, 'kind': kind}

    def _fake_emit(project_path, conv_id, kind, summary, **kw):
        calls['feed'].append({'kind': kind, 'summary': summary,
                              'payload': kw.get('payload', {})})
        return None

    monkeypatch.setattr('lib.message_queue.enqueue_message', _fake_enqueue)
    monkeypatch.setattr('lib.conversations.project_feed.emit_project_event',
                        _fake_emit)
    monkeypatch.setattr('lib.conversations.project_peer.audit_log',
                        lambda *a, **k: None)
    # Identity target-id resolution so these DB-free tests use synthetic ids
    # (cOP/cB) without a conversations table.
    monkeypatch.setattr('lib.conversations.project_peer._resolve_target_conv_id',
                        lambda t: ((t or '').strip(), ''))
    return calls


# ════════════════════════════════════════════════════════════════════
#  send_peer_message(human=True) — provenance + framing
# ════════════════════════════════════════════════════════════════════

def test_human_nudge_stamps_peer_human_and_operator_feed(_stub_io):
    from lib.conversations.project_peer import send_peer_message
    from lib.message_queue import KIND_PEER_MSG
    res = send_peer_message('/proj', 'cOP', 'cB', 'please focus on the parser',
                            human=True)
    assert res['ok'] and res['queueId']
    assert len(_stub_io['enqueue']) == 1
    call = _stub_io['enqueue'][0]
    assert call['conv_id'] == 'cB'
    assert call['kind'] == KIND_PEER_MSG, 'human nudge still uses the peer lane'
    pl = call['payload']
    assert pl.get('_peerMessage') is True
    assert pl.get('_peerHuman') is True, 'human nudge MUST stamp _peerHuman'
    assert pl.get('_fromConv') == 'cOP'
    # The target's turn is framed as OPERATOR guidance, not an agent's advisory.
    assert 'operator' in pl['text'].lower()
    assert 'weigh it and act as you see fit' not in pl['text'].lower(), \
        'the operator directive must NOT carry the agent-advisory framing'
    # Feed row stamped operator provenance.
    assert len(_stub_io['feed']) == 1
    fp = _stub_io['feed'][0]['payload']
    assert fp.get('kind') == 'operator' and fp.get('human') is True
    assert fp.get('fromConv') == 'cOP' and fp.get('toConv') == 'cB'


def test_agent_note_stays_advisory_no_peer_human(_stub_io):
    """The default (human=False) agent path is unchanged: no _peerHuman, and the
    feed row keeps its agent kind label — the human flag is strictly additive."""
    from lib.conversations.project_peer import send_peer_message
    send_peer_message('/proj', 'cA', 'cB', 'heads up on lex.py')
    pl = _stub_io['enqueue'][0]['payload']
    assert pl.get('_peerHuman') is None, 'agent peer note must NOT be _peerHuman'
    assert 'advisory' in pl['text'].lower()
    assert _stub_io['feed'][0]['payload'].get('kind') == 'note'
    assert _stub_io['feed'][0]['payload'].get('human') is False


def test_human_nudge_still_refuses_self_and_rate_limits(_stub_io):
    """The human path is NOT a storm bypass — same self-send + rate guards."""
    from lib.conversations.project_peer import (
        _PEER_MSG_MAX_PER_WINDOW, send_peer_message,
    )
    assert send_peer_message('/proj', 'cOP', 'cOP', 'x', human=True)['error'] \
        == 'cannot_message_self'
    assert _PEER_MSG_MAX_PER_WINDOW == 3
    oks = [send_peer_message('/proj', 'cOP', 'cB', f'm{i}', human=True)['ok']
           for i in range(3)]
    assert all(oks)
    blocked = send_peer_message('/proj', 'cOP', 'cB', 'm4 (storm)', human=True)
    assert blocked['ok'] is False and blocked['error'] == 'rate_limited'
    # Only 3 reached the queue (self-send never enqueued either).
    assert len(_stub_io['enqueue']) == 3


# ════════════════════════════════════════════════════════════════════
#  dispatch_next_queued — the _peerHuman marker survives onto the turn
# ════════════════════════════════════════════════════════════════════

class _HybridRow:
    _D = {'messages': '[]', 'updated_at': 0, 'settings': '{}'}

    def __getitem__(self, k):
        if isinstance(k, int):
            return 0
        return self._D.get(k, None)


class _FakeDB:
    def execute(self, sql, params=()):
        class _Cur:
            def fetchone(self_inner):
                return _HybridRow()
        return _Cur()

    def commit(self):
        pass


def _drive_dispatch(monkeypatch, payload):
    import lib.message_queue as mq
    captured = {}
    monkeypatch.setattr(mq, 'dequeue_next', lambda c: {
        'queueId': 'q1', 'config': {}, 'payload': payload})
    monkeypatch.setattr(mq, 'get_thread_db', lambda *a, **k: _FakeDB())
    monkeypatch.setattr(mq, 'db_execute_with_retry', lambda *a, **k: None)
    monkeypatch.setattr('lib.database.json_dumps_pg', lambda x: '[]')
    monkeypatch.setattr('lib.chat.append_user_msg_idempotent',
                        lambda msgs, m: captured.setdefault('msg', m))
    monkeypatch.setattr(
        'lib.tasks_pkg.conv_message_builder.build_api_messages_from_db',
        lambda *a, **k: [])
    mq.dispatch_next_queued('cTARGET')
    return captured.get('msg')


def test_peer_human_marker_survives_dispatch(monkeypatch):
    m = _drive_dispatch(monkeypatch, {
        'text': 'operator: focus on the parser',
        '_peerMessage': True, '_fromConv': 'cOP', '_peerHuman': True})
    assert m is not None
    assert m.get('_peerMessage') is True
    assert m.get('_fromConv') == 'cOP'
    assert m.get('_peerHuman') is True, \
        'the operator marker MUST propagate onto the persisted turn'


def test_agent_peer_dispatch_has_no_human_marker(monkeypatch):
    """A non-human peer note must NOT gain a _peerHuman marker in dispatch."""
    m = _drive_dispatch(monkeypatch, {
        'text': 'heads up', '_peerMessage': True, '_fromConv': 'cA'})
    assert m.get('_peerMessage') is True and m.get('_peerHuman') is None


# ════════════════════════════════════════════════════════════════════
#  Byte-reverting NEGATIVE CONTROLS
# ════════════════════════════════════════════════════════════════════

from tests._nc_harness import patch_restore as _patch_restore  # noqa: E402


def test_NC_send_drops_peer_human_stamp(_stub_io):
    """NC-HUMAN-PAYLOAD: remove the _peerHuman payload stamp in
    send_peer_message → the enqueued payload no longer carries _peerHuman →
    the human-attribution invariant no longer holds (proving the stamp is what
    makes the operator arrival distinguishable)."""
    def run():
        import lib.conversations.project_peer as pp
        pp._resolve_target_conv_id = lambda t: ((t or '').strip(), '')
        pp.send_peer_message('/proj', 'cOP', 'cB', 'focus', human=True)
        pl = _stub_io['enqueue'][-1]['payload']
        assert pl.get('_peerHuman') is None, \
            'NC-HUMAN-PAYLOAD: with the stamp removed _peerHuman must be ABSENT'

    _patch_restore(
        _PEER_SRC,
        "    payload = {'text': body, '_peerMessage': True, '_fromConv': from_conv_id}\n"
        "    if human:\n"
        "        payload['_peerHuman'] = True",
        "    payload = {'text': body, '_peerMessage': True, '_fromConv': from_conv_id}\n"
        "    # NC-HUMAN-PAYLOAD (stamp disabled)",
        run,
    )


def test_NC_dispatch_drops_peer_human_marker(monkeypatch):
    """NC-DISPATCH-HUMAN: no-op the _peerHuman propagation in
    dispatch_next_queued → the persisted operator turn loses its marker →
    the propagation test FAILS."""
    def run():
        m = _drive_dispatch(monkeypatch, {
            'text': 'op', '_peerMessage': True,
            '_fromConv': 'cOP', '_peerHuman': True})
        assert (m or {}).get('_peerHuman') is None, \
            'NC-DISPATCH-HUMAN: with propagation disabled the marker is ABSENT'

    _patch_restore(
        _MQ_SRC,
        "            if payload.get('_peerMessage') and payload.get('_peerHuman'):\n"
        "                user_msg['_peerHuman'] = True",
        "            pass  # NC-DISPATCH-HUMAN (human marker propagation disabled)",
        run,
    )
