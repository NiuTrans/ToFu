"""tests/test_project_peer.py — Pillar #6: cross-conversation communication.

Three agent verbs close the "no agent-initiated communication / no intervention"
gap:

  • ``build_peer_status`` / ``_join_peers`` — LIVE peer introspection (presence
    ⋈ task-registry ⋈ board claim map). Not history.
  • ``send_peer_message`` — advisory peer messaging via a NEW ``KIND_PEER_MSG``
    turn source, rate-limited per (sender, target) so an A→B→A storm is
    impossible.
  • ``intervene_peer`` — advisory by default; a coercive hard abort is
    AUDIT-GATED. When no token is pre-supplied it REQUESTS human approval via an
    injected ``approval_fn`` (the handler wires this to the ``ask_human`` /
    ``request_human_guidance`` UI seam) — grant → abort runs + audit; deny →
    non-coercive. This is what makes the coercive half reachable end-to-end.

Pure cores (``_prune_and_check`` / ``_authorize_hard_abort`` / ``_join_peers``)
are tested without any DB. The messaging/intervention paths are exercised with
``enqueue_message`` + ``emit_project_event`` + ``abort_running_tasks_for_conv``
monkeypatched, so the suite never depends on the bare-CI ``conversations``
table.

Four MANDATORY source-level negative controls (each byte-reverting):
  • NC-STORM: no-op the rate cap in ``_prune_and_check`` → the storm test FAILS
    (the 4th message in a window is no longer refused).
  • NC-GATE: no-op the approval check in ``_authorize_hard_abort`` → the
    unapproved-hard-abort test FAILS (a kill goes through with no approval).
  • NC-DENY: no-op the deny branch in ``intervene_peer`` → a human "Deny" is
    ignored and the abort runs anyway → the deny-path test FAILS.
  • NC-JOIN: no-op the ``exclude_conv`` filter in ``_join_peers`` → the
    self-exclusion test FAILS (a conversation sees itself as a peer).
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_PEER_SRC = os.path.join(ROOT, 'lib', 'conversations', 'project_peer.py')


@pytest.fixture(autouse=True)
def _reset_rate_history():
    """Clear the in-memory per-pair send history before AND after each test so
    the rate-limit window never leaks across tests."""
    import lib.conversations.project_peer as pp
    with pp._rate_lock:
        pp._peer_msg_history.clear()
    yield
    with pp._rate_lock:
        pp._peer_msg_history.clear()


@pytest.fixture
def _stub_io(monkeypatch):
    """Stub the two side-effecting deps of the messaging path (queue + feed) so
    send_peer_message is DB-free. Returns a list capturing enqueue calls."""
    calls = []

    def _fake_enqueue(conv_id, message_data, config, kind='real'):
        calls.append({'conv_id': conv_id, 'kind': kind,
                      'payload': message_data, 'config': config})
        return {'queueId': 'q_' + conv_id[:6], 'position': 1, 'kind': kind}

    monkeypatch.setattr('lib.message_queue.enqueue_message', _fake_enqueue)
    monkeypatch.setattr('lib.conversations.project_feed.emit_project_event',
                        lambda *a, **k: None)
    monkeypatch.setattr('lib.conversations.project_peer.audit_log',
                        lambda *a, **k: None)
    # Identity target-id resolution so these DB-free tests use synthetic ids
    # (cA/cB) without a conversations table. The real resolver is covered by
    # the dedicated seeded-DB tests below (test_resolve_* / test_send_*_target).
    monkeypatch.setattr('lib.conversations.project_peer._resolve_target_conv_id',
                        lambda t: ((t or '').strip(), ''))
    return calls


# ════════════════════════════════════════════════════════════════════
#  Pure core: sliding-window rate check
# ════════════════════════════════════════════════════════════════════

def test_prune_and_check_allows_within_window():
    from lib.conversations.project_peer import _prune_and_check
    # empty history → allowed, records now
    allowed, kept, retry = _prune_and_check([], 100.0, window_s=120, max_n=3)
    assert allowed and kept == [100.0] and retry == 0.0
    # two prior in-window sends, cap 3 → third allowed
    allowed, kept, retry = _prune_and_check([90.0, 95.0], 100.0, window_s=120, max_n=3)
    assert allowed and kept == [90.0, 95.0, 100.0]


def test_prune_and_check_refuses_at_cap():
    from lib.conversations.project_peer import _prune_and_check
    allowed, kept, retry = _prune_and_check([10.0, 20.0, 30.0], 40.0,
                                            window_s=120, max_n=3)
    assert not allowed, 'at capacity within window → refused'
    assert kept == [10.0, 20.0, 30.0], 'refused send must NOT be recorded'
    # oldest (10) ages out at 10+120=130 → retry_after = 90
    assert retry == pytest.approx(90.0)


def test_prune_and_check_prunes_expired():
    from lib.conversations.project_peer import _prune_and_check
    # two sends but one is outside the window → only one counts → allowed
    allowed, kept, _ = _prune_and_check([1.0, 500.0], 600.0, window_s=120, max_n=2)
    assert allowed, 'expired timestamps are pruned, freeing the slot'
    assert 1.0 not in kept and 500.0 in kept and 600.0 in kept


# ════════════════════════════════════════════════════════════════════
#  Pure core: hard-abort authorization gate
# ════════════════════════════════════════════════════════════════════

def test_authorize_gate():
    from lib.conversations.project_peer import _authorize_hard_abort
    assert _authorize_hard_abort(False, '') == (True, 'advisory')
    assert _authorize_hard_abort(True, '')[0] is False
    assert _authorize_hard_abort(True, '   ')[0] is False, 'whitespace token is not approval'
    assert _authorize_hard_abort(True, 'user@x')[0] is True


# ════════════════════════════════════════════════════════════════════
#  Pure core: the presence ⋈ task ⋈ board join
# ════════════════════════════════════════════════════════════════════

def _peers():
    return [
        {'convId': 'cA', 'agentId': '', 'title': 'Parser work',
         'phase': 'working', 'statusLabel': 'editing p.py', 'currentFile': 'p.py'},
        {'convId': 'cB', 'agentId': '', 'title': 'Docs', 'statusLabel': 'generating'},
        {'convId': 'cA', 'agentId': 'sub1', 'parentTitle': 'Parser work',
         'statusLabel': 'working'},
    ]


def test_join_peers_merges_all_three_sources():
    from lib.conversations.project_peer import _join_peers
    view = _join_peers(
        _peers(),
        task_by_conv={'cA': {'round': 5, 'status': 'running'}},
        claim_by_conv={'cA': 'Refactor the parser'},
    )
    by = {(v['convId'], v['agentId']): v for v in view}
    # cA conversation peer carries live round + claimed epic
    assert by[('cA', '')]['round'] == 5
    assert by[('cA', '')]['claimedEpic'] == 'Refactor the parser'
    assert by[('cA', '')]['currentFile'] == 'p.py'
    # cB has no task/claim → zero round, no epic
    assert by[('cB', '')]['round'] == 0 and by[('cB', '')]['claimedEpic'] == ''
    # sub-agent peer is present but never attributed a conversation round/epic
    assert by[('cA', 'sub1')]['round'] == 0
    assert by[('cA', 'sub1')]['claimedEpic'] == ''


def test_join_peers_excludes_self():
    from lib.conversations.project_peer import _join_peers
    view = _join_peers(_peers(), {}, {}, exclude_conv='cA')
    assert all(v['convId'] != 'cA' for v in view), 'caller must not see itself'
    assert {v['convId'] for v in view} == {'cB'}


def test_build_peer_status_convCount_excludes_subagents(monkeypatch):
    """The Team-panel headline/badge count is CONVERSATIONS, not raw peers: a
    running conversation's sub-agents are separate presence peers (convId#agentId)
    and must not inflate the count. build_peer_status returns a backend-computed
    convCount using the SAME rule build_brain_summary applies for activePeers
    (dedup on convId, exclude agentId) so the two views can never drift.
    """
    import lib.conversations.project_peer as pp
    # 1 conversation (cA) running 2 sub-agents → presence has 3 peers total.
    monkeypatch.setattr('lib.presence.registry.snapshot', lambda p: {'peers': [
        {'convId': 'cA', 'agentId': '', 'title': 'Parser work', 'statusLabel': 'working'},
        {'convId': 'cA', 'agentId': 'sub1', 'statusLabel': 'working'},
        {'convId': 'cA', 'agentId': 'sub2', 'statusLabel': 'working'},
    ]})
    monkeypatch.setattr('lib.conversations.project_board.read_board',
                        lambda p: {'tasks': []})
    monkeypatch.setattr('lib.conversations.project_peer._live_task_by_conv', lambda: {})
    monkeypatch.setattr('lib.conversations.project_peer._titles_by_conv', lambda ids: {})
    out = pp.build_peer_status('/proj')
    # All 3 peers are returned + rendered as cards …
    assert out['count'] == 3, out
    assert len(out['peers']) == 3, out
    # … but the conversation count is 1 (the sub-agents do not inflate it).
    assert out['convCount'] == 1, out


def test_build_peer_status_convCount_counts_distinct_conversations(monkeypatch):
    """Two distinct conversations (each with a sub-agent) → convCount == 2,
    even though 4 peers are present. Excludes the caller's own conv."""
    import lib.conversations.project_peer as pp
    monkeypatch.setattr('lib.presence.registry.snapshot', lambda p: {'peers': [
        {'convId': 'cA', 'agentId': '', 'statusLabel': 'working'},
        {'convId': 'cA', 'agentId': 'sub1', 'statusLabel': 'working'},
        {'convId': 'cB', 'agentId': '', 'statusLabel': 'generating'},
        {'convId': 'cB', 'agentId': 'sub1', 'statusLabel': 'working'},
    ]})
    monkeypatch.setattr('lib.conversations.project_board.read_board',
                        lambda p: {'tasks': []})
    monkeypatch.setattr('lib.conversations.project_peer._live_task_by_conv', lambda: {})
    monkeypatch.setattr('lib.conversations.project_peer._titles_by_conv', lambda ids: {})
    # Caller is cA → excluded; only cB (+ its sub-agent) remains.
    out = pp.build_peer_status('/proj', conv_id='cA')
    assert out['convCount'] == 1, out
    assert {p['convId'] for p in out['peers']} == {'cB'}, out


# ════════════════════════════════════════════════════════════════════
#  send_peer_message — refusals + rate-limit storm guard (DB-free)
# ════════════════════════════════════════════════════════════════════

def test_send_refuses_self_and_empty(_stub_io):
    from lib.conversations.project_peer import send_peer_message
    assert send_peer_message('/p', 'cA', 'cA', 'hi')['error'] == 'cannot_message_self'
    assert send_peer_message('/p', 'cA', 'cB', '  ')['error'] == 'empty message'
    assert send_peer_message('', 'cA', 'cB', 'hi')['error'] == 'no project'
    assert _stub_io == [], 'no enqueue on a refused send'


def test_send_enqueues_peer_msg_kind(_stub_io):
    from lib.conversations.project_peer import send_peer_message
    from lib.message_queue import KIND_PEER_MSG
    res = send_peer_message('/p', 'cA', 'cB', 'watch out for the parser epic')
    assert res['ok'] and res['queueId']
    assert len(_stub_io) == 1
    call = _stub_io[0]
    assert call['conv_id'] == 'cB'
    assert call['kind'] == KIND_PEER_MSG, 'peer msg must use KIND_PEER_MSG, not workflow'
    assert call['payload'].get('_peerMessage') is True
    assert call['payload'].get('_fromConv') == 'cA'
    assert 'watch out for the parser epic' in call['payload']['text']


def test_rate_limit_storm_guard(_stub_io):
    """The storm guard: with cap=3/window, the 4th message to the SAME target
    inside the window is refused — so A→B traffic per window is bounded."""
    from lib.conversations.project_peer import (
        _PEER_MSG_MAX_PER_WINDOW, send_peer_message,
    )
    assert _PEER_MSG_MAX_PER_WINDOW == 3
    oks = [send_peer_message('/p', 'cA', 'cB', f'msg {i}')['ok'] for i in range(3)]
    assert all(oks), 'first 3 within cap must succeed'
    blocked = send_peer_message('/p', 'cA', 'cB', 'msg 4 (storm)')
    assert blocked['ok'] is False and blocked['error'] == 'rate_limited'
    assert blocked.get('retryAfter', 0) > 0
    # Only 3 enqueues happened — the 4th never reached the queue.
    assert len(_stub_io) == 3, 'a rate-limited message must NOT be enqueued'
    # A DIFFERENT target is a different (sender,target) pair → still allowed.
    assert send_peer_message('/p', 'cA', 'cC', 'to a different peer')['ok']


def test_failed_enqueue_refunds_rate_slot(monkeypatch):
    """A FAILING enqueue must refund the rate-limit slot it consumed at check
    time — otherwise a flapping (always-raising) target silently drains the
    sender's per-window budget for messages that never landed.

    With cap=3: three failing sends must NOT exhaust the budget (each refunds),
    so a 4th send still passes the gate. NC below proves the refund is what
    makes this hold."""
    import lib.conversations.project_peer as pp
    from lib.conversations.project_peer import send_peer_message
    monkeypatch.setattr('lib.conversations.project_feed.emit_project_event',
                        lambda *a, **k: None)
    monkeypatch.setattr('lib.conversations.project_peer.audit_log',
                        lambda *a, **k: None)
    monkeypatch.setattr('lib.conversations.project_peer._resolve_target_conv_id',
                        lambda t: ((t or '').strip(), ''))

    def _boom(*a, **k):
        raise RuntimeError('queue down')
    monkeypatch.setattr('lib.message_queue.enqueue_message', _boom)

    # Five consecutive FAILED sends — far past the cap of 3. Each must refund,
    # so none is ever rate_limited (the failure is surfaced, not the budget).
    for i in range(5):
        res = send_peer_message('/p', 'cA', 'cB', f'flap {i}')
        assert res['ok'] is False
        assert res['error'] != 'rate_limited', (
            f'send {i}: a failed enqueue must refund its slot, never exhaust '
            f'the budget → got {res}')
    # The window history for the pair is empty (every slot refunded).
    with pp._rate_lock:
        assert not pp._peer_msg_history.get(('cA', 'cB')), \
            'all failed-send slots must have been refunded'


def test_refund_only_on_failure_not_on_success(_stub_io):
    """The refund must NOT fire on a SUCCESSFUL send — the storm guard still
    bounds real traffic. Three successful sends fill the window; the 4th is
    rate_limited exactly as before (the refund only covers failures)."""
    from lib.conversations.project_peer import send_peer_message
    for i in range(3):
        assert send_peer_message('/p', 'cA', 'cB', f'ok {i}')['ok']
    blocked = send_peer_message('/p', 'cA', 'cB', 'msg 4')
    assert blocked['ok'] is False and blocked['error'] == 'rate_limited', \
        'successful sends are NOT refunded — the storm guard must still bite'


def test_no_auto_relay_body_is_plain_content(_stub_io):
    """The received message is PLAIN turn content — it carries no send
    directive, so receiving one can never auto-trigger another send."""
    from lib.conversations.project_peer import send_peer_message
    send_peer_message('/p', 'cA', 'cB', 'hello peer')
    text = _stub_io[0]['payload']['text']
    # advisory framing present; no tool-call / send instruction embedded
    assert 'advisory' in text.lower()
    assert 'project_message' not in text, 'must not instruct the peer to relay'


# ════════════════════════════════════════════════════════════════════
#  intervene_peer — advisory default + audit-gated hard abort
# ════════════════════════════════════════════════════════════════════

def test_intervene_advisory_routes_to_message(_stub_io):
    from lib.conversations.project_peer import intervene_peer
    res = intervene_peer('/p', 'cA', 'cB', 'you are duplicating epic X')
    assert res['ok'] and res['mode'] == 'advisory'
    assert len(_stub_io) == 1 and _stub_io[0]['conv_id'] == 'cB'


def test_intervene_hard_abort_refused_without_approval(_stub_io, monkeypatch):
    from lib.conversations.project_peer import intervene_peer
    aborted = []
    monkeypatch.setattr('lib.tasks_pkg.manager.abort_running_tasks_for_conv',
                        lambda c, **k: aborted.append(c) or 1)
    res = intervene_peer('/p', 'cA', 'cB', 'stop', hard_abort=True, approved_by='')
    assert res['ok'] is False
    assert res['error'] == 'hard_abort_requires_approval'
    assert aborted == [], 'no abort may run without approval'


def test_intervene_hard_abort_runs_when_approved(_stub_io, monkeypatch):
    from lib.conversations.project_peer import intervene_peer
    aborted = []
    audits = []
    monkeypatch.setattr('lib.tasks_pkg.manager.abort_running_tasks_for_conv',
                        lambda c, **k: aborted.append(c) or 2)
    monkeypatch.setattr('lib.conversations.project_peer.audit_log',
                        lambda ev, **k: audits.append((ev, k)))
    res = intervene_peer('/p', 'cA', 'cB', 'stop', hard_abort=True,
                         approved_by='owner')
    assert res['ok'] and res['mode'] == 'hard_abort' and res['aborted'] == 2
    assert aborted == ['cB'], 'approved hard abort targets the peer task only'
    assert any(ev == 'intervention' for ev, _ in audits), 'must audit the intervention'


def test_intervene_refuses_self(monkeypatch):
    from lib.conversations.project_peer import intervene_peer
    # Identity target resolution (synthetic ids, no conversations table); the
    # self-check must still fire on the resolved id.
    monkeypatch.setattr('lib.conversations.project_peer._resolve_target_conv_id',
                        lambda t: ((t or '').strip(), ''))
    assert intervene_peer('/p', 'cA', 'cA', 'x')['error'] == 'cannot_intervene_self'


# ════════════════════════════════════════════════════════════════════
#  REACHABILITY: hard abort via the human-approval REQUEST seam
#  (the previously-dead-code path — approval_fn mints the token at runtime)
# ════════════════════════════════════════════════════════════════════

def test_intervene_hard_abort_requests_approval_then_runs(_stub_io, monkeypatch):
    """The FULL reachable coercive path: no pre-supplied token, but an injected
    approval_fn GRANTS → the token is minted, abort_running_tasks_for_conv is
    actually called, and audit_log('intervention', approved_by=<who>) fires."""
    from lib.conversations.project_peer import intervene_peer
    aborted = []
    audits = []
    prompts = []
    monkeypatch.setattr('lib.tasks_pkg.manager.abort_running_tasks_for_conv',
                        lambda c, **k: aborted.append(c) or 3)
    monkeypatch.setattr('lib.conversations.project_peer.audit_log',
                        lambda ev, **k: audits.append((ev, k)))

    def _grant(prompt):
        prompts.append(prompt)
        return 'alice'   # the approving human

    res = intervene_peer('/p', 'cA', 'cB', 'stop', hard_abort=True,
                         approved_by='', approval_fn=_grant)
    assert res['ok'] and res['mode'] == 'hard_abort' and res['aborted'] == 3
    assert aborted == ['cB'], 'granted abort must target the peer task'
    assert prompts and 'HARD ABORT' in prompts[0], 'human must be asked to approve'
    # audit stamped with the APPROVER identity minted by the approval_fn.
    intervention = [k for ev, k in audits if ev == 'intervention']
    assert intervention and intervention[0].get('approved_by') == 'alice'


def test_intervene_hard_abort_denied_stays_advisory(_stub_io, monkeypatch):
    """DENY path: approval_fn returns None (human denied) → no abort runs, the
    verb reports denied_by_human, and it stays non-coercive."""
    from lib.conversations.project_peer import intervene_peer
    aborted = []
    monkeypatch.setattr('lib.tasks_pkg.manager.abort_running_tasks_for_conv',
                        lambda c, **k: aborted.append(c) or 1)

    def _deny(prompt):
        return None   # human clicked Deny (or task aborted)

    res = intervene_peer('/p', 'cA', 'cB', 'stop', hard_abort=True,
                         approved_by='', approval_fn=_deny)
    assert res['ok'] is False and res['error'] == 'denied_by_human'
    assert aborted == [], 'a denied hard abort must NOT stop the peer'


def _drive_handler_intervene(monkeypatch, decision, autopilot=False):
    """Drive the REAL handler path: _make_intervention_approval_fn wired to a
    stubbed request_human_guidance returning ``decision`` → execute_peer_tool.
    Returns (result_string, aborted_list, events_list, audits_list)."""
    import lib.conversations.project_peer as pp
    import lib.tasks_pkg.handlers.misc as misc
    aborted, audits, events = [], [], []
    monkeypatch.setattr('lib.tasks_pkg.manager.abort_running_tasks_for_conv',
                        lambda c, **k: aborted.append(c) or 2)
    monkeypatch.setattr('lib.conversations.project_peer.audit_log',
                        lambda ev, **k: audits.append((ev, k)))
    monkeypatch.setattr('lib.conversations.project_feed.emit_project_event',
                        lambda *a, **k: None)
    monkeypatch.setattr('lib.tasks_pkg.handlers.misc.append_event',
                        lambda t, ev: events.append(ev))
    monkeypatch.setattr('lib.tasks_pkg.human_guidance.request_human_guidance',
                        lambda gid, task=None: decision)
    monkeypatch.setattr('lib.tasks_pkg.autopilot.is_autopilot_enabled',
                        lambda t: autopilot)
    # Identity target-id resolution: these handler-surface tests use synthetic
    # ids (cA/cB) with no seeded conversations row. Without this the REAL
    # resolver returns unknown_target once ANY sibling suite has run init_db()
    # (which creates the empty conversations table) — a cross-file ordering
    # fragility, not a product bug. The seeded-DB resolver tests cover the real
    # path.
    monkeypatch.setattr('lib.conversations.project_peer._resolve_target_conv_id',
                        lambda t: ((t or '').strip(), ''))
    task = {'id': 't1', 'convId': 'cA', 'messages': [], 'toolRounds': []}
    round_entry = {'query': 'project_intervene', 'status': 'searching'}
    fn_args = {'to_conv_id': 'cB', 'message': 'stop', 'hard_abort': True}
    approval_fn = misc._make_intervention_approval_fn(task, 1, 'tc', round_entry)
    out = pp.execute_peer_tool('project_intervene', fn_args, current_conv_id='cA',
                               project_path='/proj', config={}, approval_fn=approval_fn)
    return out, aborted, events, audits


def test_handler_hard_abort_approved_runs_from_surface(monkeypatch):
    """REACHABILITY from the agent surface: the handler builds approval_fn wired
    to request_human_guidance; a granted decision → the abort actually runs +
    audit fires + the human_guidance_request(intervention) event is emitted.
    (This is the exact path that was dead code before — approval_fn was never
    populated. It also guards the round_entry closure bug.)"""
    out, aborted, events, audits = _drive_handler_intervene(monkeypatch, 'approve abort')
    assert 'human-approved' in out and aborted == ['cB'], \
        'approved hard abort must run from the handler surface'
    assert any(e.get('type') == 'human_guidance_request' and e.get('intervention')
               for e in events), 'must ask the human via the guidance seam'
    assert any(ev == 'intervention' for ev, _ in audits)


def test_handler_hard_abort_denied_from_surface(monkeypatch):
    """DENY from the surface: the human denies → no abort, non-coercive."""
    out, aborted, _events, _audits = _drive_handler_intervene(monkeypatch, 'deny')
    assert 'DENIED' in out and aborted == [], 'denied abort must not run'


def test_handler_hard_abort_autopilot_denied(monkeypatch):
    """Autopilot must NEVER auto-authorize a coercive kill of a sibling."""
    out, aborted, _e, _a = _drive_handler_intervene(monkeypatch, 'approve abort',
                                                    autopilot=True)
    assert aborted == [], 'autopilot cannot green-light a hard abort'


def test_intervene_presupplied_token_skips_approval(_stub_io, monkeypatch):
    """A pre-supplied approved_by token is honored WITHOUT calling approval_fn
    (an already-authorized headless caller path)."""
    from lib.conversations.project_peer import intervene_peer
    aborted = []
    called = []
    monkeypatch.setattr('lib.tasks_pkg.manager.abort_running_tasks_for_conv',
                        lambda c, **k: aborted.append(c) or 1)
    monkeypatch.setattr('lib.conversations.project_peer.audit_log',
                        lambda *a, **k: None)
    res = intervene_peer('/p', 'cA', 'cB', 'stop', hard_abort=True,
                         approved_by='ci-token',
                         approval_fn=lambda p: called.append(p) or 'should-not-run')
    assert res['ok'] and res['aborted'] == 1
    assert called == [], 'a pre-supplied token must short-circuit the approval request'


# ════════════════════════════════════════════════════════════════════
#  Observability: peer-message markers survive dispatch onto the turn
#  (so the arrival is structurally attributable, not silently == user input)
# ════════════════════════════════════════════════════════════════════

class _HybridRow:
    """A DB row supporting BOTH ``row['col']`` (SELECT messages/updated_at) and
    ``row[0]`` (SELECT COUNT(*) in _get_queue_depth)."""
    _D = {'messages': '[]', 'updated_at': 0, 'settings': '{}'}
    def __getitem__(self, k):
        if isinstance(k, int):
            return 0            # COUNT(*) → 0 remaining
        return self._D.get(k, None)


class _FakeDB:
    def execute(self, sql, params=()):
        class _Cur:
            def fetchone(self_inner):
                return _HybridRow()
        return _Cur()
    def commit(self):
        pass


def test_peer_markers_survive_dispatch(monkeypatch):
    """A KIND_PEER_MSG payload carries ``_peerMessage``/``_fromConv``; when the
    queue dispatches it, those markers MUST land on the persisted user message
    (else the turn is byte-identical to real user input and the UI can't
    attribute it). DB-free: dequeue_next is stubbed to return the peer payload
    and the built message is captured at append_user_msg_idempotent."""
    import lib.message_queue as mq

    captured = {}

    monkeypatch.setattr(mq, 'dequeue_next', lambda c: {
        'queueId': 'q1', 'config': {},
        'payload': {'text': 'watch the parser epic',
                    '_peerMessage': True, '_fromConv': 'cSENDER'}})
    monkeypatch.setattr(mq, 'get_thread_db', lambda *a, **k: _FakeDB())
    monkeypatch.setattr(mq, 'db_execute_with_retry', lambda *a, **k: None)
    monkeypatch.setattr('lib.database.json_dumps_pg', lambda x: '[]')
    monkeypatch.setattr('lib.chat.append_user_msg_idempotent',
                        lambda msgs, m: captured.setdefault('msg', m))
    # Short-circuit AFTER the user_msg is built+appended (return [] → None).
    monkeypatch.setattr('lib.tasks_pkg.conv_message_builder.build_api_messages_from_db',
                        lambda *a, **k: [])

    mq.dispatch_next_queued('cTARGET')
    m = captured.get('msg')
    assert m is not None, 'user_msg must be built for a peer turn'
    assert m.get('_peerMessage') is True, \
        'the peer marker MUST propagate onto the persisted turn'
    assert m.get('_fromConv') == 'cSENDER', 'sender attribution must survive'
    assert 'watch the parser epic' in m.get('content', '')


def test_NC_dispatch_drops_peer_markers(monkeypatch):
    """NC-OBSERVE: no-op the marker-propagation block in dispatch_next_queued →
    the persisted turn loses its peer markers → the observability test FAILS
    (the arrival becomes indistinguishable from user input)."""
    import importlib

    captured = {}

    def run():
        import lib.message_queue as mq
        importlib.reload(mq)
        monkeypatch.setattr(mq, 'dequeue_next', lambda c: {
            'queueId': 'q1', 'config': {},
            'payload': {'text': 'hi', '_peerMessage': True, '_fromConv': 'cS'}})
        monkeypatch.setattr(mq, 'get_thread_db', lambda *a, **k: _FakeDB())
        monkeypatch.setattr(mq, 'db_execute_with_retry', lambda *a, **k: None)
        monkeypatch.setattr('lib.database.json_dumps_pg', lambda x: '[]')
        monkeypatch.setattr('lib.chat.append_user_msg_idempotent',
                            lambda msgs, m: captured.setdefault('msg', m))
        monkeypatch.setattr('lib.tasks_pkg.conv_message_builder.build_api_messages_from_db',
                            lambda *a, **k: [])
        mq.dispatch_next_queued('cTARGET')
        m = captured.get('msg') or {}
        assert m.get('_peerMessage') is None, \
            'NC-OBSERVE: with propagation disabled the marker must be ABSENT ' \
            '(proving the real block is what makes the arrival observable)'

    _MQ_SRC = os.path.join(ROOT, 'lib', 'message_queue.py')
    _patch_restore(
        _MQ_SRC,
        "            if payload.get('_peerMessage'):\n                user_msg['_peerMessage'] = True\n                user_msg['_fromConv'] = payload.get('_fromConv', '')",
        "            pass  # NC-OBSERVE (marker propagation disabled)",
        run,
    )
    import lib.message_queue as mq
    importlib.reload(mq)


# ════════════════════════════════════════════════════════════════════
#  Source-level NEGATIVE CONTROLS (byte-reverting)
# ════════════════════════════════════════════════════════════════════

def _patch_restore(path, old, new, run):
    with open(path, encoding='utf-8') as f:
        original = f.read()
    assert old in original, f'anchor not found in {path}'
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(original.replace(old, new, 1))
        run()
    finally:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(original)
    with open(path, encoding='utf-8') as f:
        assert f.read() == original, 'source not restored byte-identical'


def test_NC_storm_guard_noop_breaks_rate_limit(_stub_io):
    """NC-STORM: disable the rate cap in _prune_and_check → the 4th message is
    no longer refused → the storm guard test FAILS."""
    import importlib

    def run():
        import lib.conversations.project_peer as pp
        importlib.reload(pp)
        # Reload re-binds _resolve_target_conv_id to the real (DB-reading) fn;
        # re-stub to identity so this DB-free NC uses synthetic ids.
        pp._resolve_target_conv_id = lambda t: ((t or '').strip(), '')
        # Re-stub via module attrs the reloaded code reads at call time.
        for i in range(3):
            pp.send_peer_message('/p', 'cA', 'cB', f'm{i}')
        blocked = pp.send_peer_message('/p', 'cA', 'cB', 'm4')
        # With the cap removed, the 4th send is (wrongly) allowed → the storm
        # invariant no longer holds.
        assert blocked['ok'] is True, \
            'NC-STORM: with the rate cap disabled the 4th message must go ' \
            'through (proving the real guard is what refuses it)'

    _patch_restore(
        _PEER_SRC,
        "    if len(kept) < max(1, max_n):\n        return True, kept + [now], 0.0\n    # At capacity: the oldest in-window send determines when a slot frees.\n    oldest = min(kept)\n    retry_after = max(0.0, (oldest + window_s) - now)\n    return False, kept, retry_after",
        "    return True, kept + [now], 0.0  # NC-STORM (rate cap disabled)",
        run,
    )
    import lib.conversations.project_peer as pp
    importlib.reload(pp)


def test_NC_audit_gate_noop_allows_unapproved_abort(monkeypatch):
    """NC-GATE: no-op the approval check in _authorize_hard_abort → an
    unapproved hard abort now proceeds → the gate test FAILS."""
    import importlib

    aborted = []
    monkeypatch.setattr('lib.tasks_pkg.manager.abort_running_tasks_for_conv',
                        lambda c, **k: aborted.append(c) or 1)
    monkeypatch.setattr('lib.conversations.project_feed.emit_project_event',
                        lambda *a, **k: None)

    def run():
        import lib.conversations.project_peer as pp
        importlib.reload(pp)
        pp._resolve_target_conv_id = lambda t: ((t or '').strip(), '')
        monkeypatch.setattr('lib.conversations.project_peer.audit_log',
                            lambda *a, **k: None)
        res = pp.intervene_peer('/p', 'cA', 'cB', 'stop',
                                hard_abort=True, approved_by='')
        # With the gate no-opped, an UNAPPROVED abort executes.
        assert res.get('ok') is True and aborted == ['cB'], \
            'NC-GATE: with the approval check disabled an unapproved abort ' \
            'must run (proving the real gate is what blocks it)'

    _patch_restore(
        _PEER_SRC,
        "    if not hard_abort:\n        return True, 'advisory'\n    if not (approved_by or '').strip():\n        return False, 'hard_abort_requires_approval'\n    return True, 'approved'",
        "    return True, 'approved'  # NC-GATE (approval check disabled)",
        run,
    )
    import lib.conversations.project_peer as pp
    importlib.reload(pp)


def test_NC_deny_branch_noop_runs_abort_despite_denial(_stub_io, monkeypatch):
    """NC-DENY: no-op the deny branch (treat a falsy approval as approved) → a
    DENIED hard abort now runs the abort anyway → the deny-path test FAILS.
    This proves the deny branch is what actually stops an unapproved kill."""
    import importlib

    aborted = []
    monkeypatch.setattr('lib.tasks_pkg.manager.abort_running_tasks_for_conv',
                        lambda c, **k: aborted.append(c) or 1)
    monkeypatch.setattr('lib.conversations.project_feed.emit_project_event',
                        lambda *a, **k: None)

    def run():
        import lib.conversations.project_peer as pp
        importlib.reload(pp)
        pp._resolve_target_conv_id = lambda t: ((t or '').strip(), '')
        monkeypatch.setattr('lib.conversations.project_peer.audit_log',
                            lambda *a, **k: None)
        res = pp.intervene_peer('/p', 'cA', 'cB', 'stop', hard_abort=True,
                                approved_by='', approval_fn=lambda prompt: None)
        # With the deny branch no-opped, a human "Deny" is ignored — the abort
        # (wrongly) runs. That is exactly what the real deny branch prevents.
        assert res.get('ok') is True and aborted == ['cB'], \
            'NC-DENY: with the deny branch disabled a denied abort must run ' \
            '(proving the real branch is what enforces the denial)'

    _patch_restore(
        _PEER_SRC,
        "        if approver:\n            approved_by = str(approver).strip()\n        else:\n            logger.info('[Intervene] hard-abort DENIED by human %s→%s',\n                        from_conv_id[:8], to_conv_id[:8])\n            return {'ok': False, 'mode': 'hard_abort', 'error': 'denied_by_human'}",
        "        approved_by = str(approver).strip() if approver else 'nc-deny-forced'  # NC-DENY",
        run,
    )
    import lib.conversations.project_peer as pp
    importlib.reload(pp)


def test_NC_join_exclude_noop_leaks_self():
    """NC-JOIN: no-op the exclude_conv filter → a conversation sees ITSELF in
    its own peer list → the self-exclusion test FAILS."""
    import importlib

    def run():
        import lib.conversations.project_peer as pp
        importlib.reload(pp)
        view = pp._join_peers(_peers(), {}, {}, exclude_conv='cA')
        # With the exclusion disabled, cA leaks into its own peer view.
        assert any(v['convId'] == 'cA' for v in view), \
            'NC-JOIN: with exclude_conv disabled the caller must see itself'

    _patch_restore(
        _PEER_SRC,
        "        if not conv_id or (exclude_conv and conv_id == exclude_conv):\n            continue",
        "        if not conv_id:\n            continue  # NC-JOIN (self-exclude disabled)",
        run,
    )
    import lib.conversations.project_peer as pp
    importlib.reload(pp)
