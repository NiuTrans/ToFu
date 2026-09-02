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

import pytest

pytestmark = pytest.mark.unit

TEST_OWNER_USER_ID = 1


def _resolve_synthetic_target(target, *, user_id):
    """Resolve synthetic ids while proving the owner crosses the test seam."""
    assert user_id == TEST_OWNER_USER_ID
    return (target or '').strip(), ''


def _owned_static(value):
    """Return a storage stub that rejects a missing or cross-owner read."""
    def _read(*_args, user_id):
        assert user_id == TEST_OWNER_USER_ID
        return value

    return _read


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

    def _fake_enqueue(
        conv_id, message_data, config, kind='real', *, user_id,
    ):
        assert user_id == TEST_OWNER_USER_ID
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
                        _resolve_synthetic_target)
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
    monkeypatch.setattr('lib.presence.registry.snapshot', _owned_static({'peers': [
        {'convId': 'cA', 'agentId': '', 'title': 'Parser work', 'statusLabel': 'working'},
        {'convId': 'cA', 'agentId': 'sub1', 'statusLabel': 'working'},
        {'convId': 'cA', 'agentId': 'sub2', 'statusLabel': 'working'},
    ]}))
    monkeypatch.setattr('lib.conversations.project_board.read_board',
                        _owned_static({'tasks': []}))
    monkeypatch.setattr('lib.conversations.project_peer._live_task_by_conv',
                        _owned_static({}))
    monkeypatch.setattr('lib.conversations.project_peer._titles_by_conv',
                        _owned_static({}))
    out = pp.build_peer_status('/proj', user_id=TEST_OWNER_USER_ID)
    # All 3 peers are returned + rendered as cards …
    assert out['count'] == 3, out
    assert len(out['peers']) == 3, out
    # … but the conversation count is 1 (the sub-agents do not inflate it).
    assert out['convCount'] == 1, out


def test_build_peer_status_convCount_counts_distinct_conversations(monkeypatch):
    """Two distinct conversations (each with a sub-agent) → convCount == 2,
    even though 4 peers are present. Excludes the caller's own conv."""
    import lib.conversations.project_peer as pp
    monkeypatch.setattr('lib.presence.registry.snapshot', _owned_static({'peers': [
        {'convId': 'cA', 'agentId': '', 'statusLabel': 'working'},
        {'convId': 'cA', 'agentId': 'sub1', 'statusLabel': 'working'},
        {'convId': 'cB', 'agentId': '', 'statusLabel': 'generating'},
        {'convId': 'cB', 'agentId': 'sub1', 'statusLabel': 'working'},
    ]}))
    monkeypatch.setattr('lib.conversations.project_board.read_board',
                        _owned_static({'tasks': []}))
    monkeypatch.setattr('lib.conversations.project_peer._live_task_by_conv',
                        _owned_static({}))
    monkeypatch.setattr('lib.conversations.project_peer._titles_by_conv',
                        _owned_static({}))
    # Caller is cA → excluded; only cB (+ its sub-agent) remains.
    out = pp.build_peer_status('/proj', conv_id='cA', user_id=TEST_OWNER_USER_ID)
    assert out['convCount'] == 1, out
    assert {p['convId'] for p in out['peers']} == {'cB'}, out


# ════════════════════════════════════════════════════════════════════
#  send_peer_message — refusals + rate-limit storm guard (DB-free)
# ════════════════════════════════════════════════════════════════════

def test_send_refuses_self_and_empty(_stub_io):
    from lib.conversations.project_peer import send_peer_message
    assert send_peer_message('/p', 'cA', 'cA', 'hi', user_id=TEST_OWNER_USER_ID)['error'] == 'cannot_message_self'
    assert send_peer_message('/p', 'cA', 'cB', '  ', user_id=TEST_OWNER_USER_ID)['error'] == 'empty message'
    assert send_peer_message('', 'cA', 'cB', 'hi', user_id=TEST_OWNER_USER_ID)['error'] == 'no project'
    assert _stub_io == [], 'no enqueue on a refused send'


def test_send_enqueues_peer_msg_kind(_stub_io):
    from lib.conversations.project_peer import send_peer_message
    from lib.message_queue import KIND_PEER_MSG
    res = send_peer_message('/p', 'cA', 'cB', 'watch out for the parser epic', user_id=TEST_OWNER_USER_ID)
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
    oks = [send_peer_message('/p', 'cA', 'cB', f'msg {i}', user_id=TEST_OWNER_USER_ID)['ok'] for i in range(3)]
    assert all(oks), 'first 3 within cap must succeed'
    blocked = send_peer_message('/p', 'cA', 'cB', 'msg 4 (storm)', user_id=TEST_OWNER_USER_ID)
    assert blocked['ok'] is False and blocked['error'] == 'rate_limited'
    assert blocked.get('retryAfter', 0) > 0
    # Only 3 enqueues happened — the 4th never reached the queue.
    assert len(_stub_io) == 3, 'a rate-limited message must NOT be enqueued'
    # A DIFFERENT target is a different (sender,target) pair → still allowed.
    assert send_peer_message('/p', 'cA', 'cC', 'to a different peer', user_id=TEST_OWNER_USER_ID)['ok']


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
                        _resolve_synthetic_target)

    def _boom(*a, **k):
        raise RuntimeError('queue down')
    monkeypatch.setattr('lib.message_queue.enqueue_message', _boom)

    # Five consecutive FAILED sends — far past the cap of 3. Each must refund,
    # so none is ever rate_limited (the failure is surfaced, not the budget).
    for i in range(5):
        res = send_peer_message('/p', 'cA', 'cB', f'flap {i}', user_id=TEST_OWNER_USER_ID)
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
        assert send_peer_message('/p', 'cA', 'cB', f'ok {i}', user_id=TEST_OWNER_USER_ID)['ok']
    blocked = send_peer_message('/p', 'cA', 'cB', 'msg 4', user_id=TEST_OWNER_USER_ID)
    assert blocked['ok'] is False and blocked['error'] == 'rate_limited', \
        'successful sends are NOT refunded — the storm guard must still bite'


def test_no_auto_relay_body_is_plain_content(_stub_io):
    """The received message is PLAIN turn content — it carries no send
    directive, so receiving one can never auto-trigger another send."""
    from lib.conversations.project_peer import send_peer_message
    send_peer_message('/p', 'cA', 'cB', 'hello peer', user_id=TEST_OWNER_USER_ID)
    text = _stub_io[0]['payload']['text']
    # advisory framing present; no tool-call / send instruction embedded
    assert 'advisory' in text.lower()
    assert 'project_message' not in text, 'must not instruct the peer to relay'


# ════════════════════════════════════════════════════════════════════
#  intervene_peer — advisory default + audit-gated hard abort
# ════════════════════════════════════════════════════════════════════

def test_intervene_advisory_routes_to_message(_stub_io):
    from lib.conversations.project_peer import intervene_peer
    res = intervene_peer('/p', 'cA', 'cB', 'you are duplicating epic X', user_id=TEST_OWNER_USER_ID)
    assert res['ok'] and res['mode'] == 'advisory'
    assert len(_stub_io) == 1 and _stub_io[0]['conv_id'] == 'cB'


def test_intervene_hard_abort_refused_without_approval(_stub_io, monkeypatch):
    from lib.conversations.project_peer import intervene_peer
    aborted = []
    monkeypatch.setattr('lib.tasks_pkg.manager.abort_running_tasks_for_conv',
                        lambda c, **k: aborted.append(c) or 1)
    res = intervene_peer('/p', 'cA', 'cB', 'stop', hard_abort=True, approved_by='', user_id=TEST_OWNER_USER_ID)
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
                         approved_by='owner', user_id=TEST_OWNER_USER_ID)
    assert res['ok'] and res['mode'] == 'hard_abort' and res['aborted'] == 2
    assert aborted == ['cB'], 'approved hard abort targets the peer task only'
    assert any(ev == 'intervention' for ev, _ in audits), 'must audit the intervention'


def test_intervene_refuses_self(monkeypatch):
    from lib.conversations.project_peer import intervene_peer
    # Identity target resolution (synthetic ids, no conversations table); the
    # self-check must still fire on the resolved id.
    monkeypatch.setattr('lib.conversations.project_peer._resolve_target_conv_id',
                        _resolve_synthetic_target)
    assert intervene_peer('/p', 'cA', 'cA', 'x', user_id=TEST_OWNER_USER_ID)['error'] == 'cannot_intervene_self'


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
                         approved_by='', approval_fn=_grant, user_id=TEST_OWNER_USER_ID)
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
                         approved_by='', approval_fn=_deny, user_id=TEST_OWNER_USER_ID)
    assert res['ok'] is False and res['error'] == 'denied_by_human'
    assert aborted == [], 'a denied hard abort must NOT stop the peer'


def _drive_handler_intervene(monkeypatch, decision, autopilot=False):
    """Drive the REAL handler path: _make_intervention_approval_fn wired to a
    stubbed request_human_guidance returning ``decision`` → execute_peer_tool.
    Returns (result_string, aborted_list, events_list, audits_list)."""
    import lib.conversations.project_peer as pp
    import lib.tasks_pkg.handlers.misc._brain as peer_handlers
    aborted, audits, events = [], [], []
    monkeypatch.setattr('lib.tasks_pkg.manager.abort_running_tasks_for_conv',
                        lambda c, **k: aborted.append(c) or 2)
    monkeypatch.setattr('lib.conversations.project_peer.audit_log',
                        lambda ev, **k: audits.append((ev, k)))
    monkeypatch.setattr('lib.conversations.project_feed.emit_project_event',
                        lambda *a, **k: None)
    monkeypatch.setattr(peer_handlers, 'append_event',
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
                        _resolve_synthetic_target)
    task = {'id': 't1', 'convId': 'cA', 'messages': [], 'toolRounds': []}
    round_entry = {'query': 'project_intervene', 'status': 'searching'}
    fn_args = {'to_conv_id': 'cB', 'message': 'stop', 'hard_abort': True}
    approval_fn = peer_handlers._make_intervention_approval_fn(
        task, 1, 'tc', round_entry)
    out = pp.execute_peer_tool('project_intervene', fn_args, current_conv_id='cA',
                               project_path='/proj', config={}, approval_fn=approval_fn, user_id=TEST_OWNER_USER_ID)
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
                         approval_fn=lambda p: called.append(p) or 'should-not-run', user_id=TEST_OWNER_USER_ID)
    assert res['ok'] and res['aborted'] == 1
    assert called == [], 'a pre-supplied token must short-circuit the approval request'
