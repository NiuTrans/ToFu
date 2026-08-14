"""tests/test_conv_state_staleness_selfheal.py — pt_cadaa70ffa6b468d.

THE SECOND HALF OF THE REPORTED SYMPTOM
---------------------------------------
The owner's complaint had two parts. The first — state converging wrongly — was
a clock-domain defect, fixed separately (charter #27). This is the second part,
which is not a latency problem at all:

  "before the backend state is fully synchronized, the frontend doesn't even
   know its state is lagging, which might lead to displaying incorrect states"

Two independent gaps produced that, and each is a case of a component that
already KNOWS something and does nothing with it:

  A. THE SERVER DETECTED STALLS AND ONLY LOGGED THEM.
     ``drift_tracker.observe_divergence`` already separates a client that is
     merely sampling late (its value is MOVING) from one that is FROZEN while
     the server advances — the "a notify frame was dropped and this tab will
     never converge" case — and marks the latter ``sustained``. But the probe
     endpoint used that verdict only to pick a log level. The server knew
     exactly which socket was stuck and left the user to discover it by
     pressing F5.

  B. THE CLIENT HAD NO CONCEPT OF ITS OWN STALENESS.
     push.js runs an 8s ping watchdog and force-closes a half-open socket, so
     "frames are not arriving" is already computed. Nothing consumed it for
     state rendering: during an outage the sidebar kept drawing a busy/idle
     verdict as settled fact.

WHAT THIS SUITE PINS

  Repair policy (pure, no hub):
    1. a sustained stall warrants a repair;
    2. a non-sustained divergence does NOT (a single missed beat is not worth a
       frame; the next notify almost always closes it);
    3. cooldown suppresses a second repair to the same socket;
    4. an empty socket id can never trigger one — an untargetable repair would
       have to be broadcast, which hits healthy tabs and MASKS the condition;
    5. a failed delivery must NOT start the cooldown, or one unlucky probe
       silences repair for the whole window.

  Transport:
    6. the hub can deliver to ONE socket by req_id, and does not touch others.

  Client confidence:
    7. it is a SEPARATE dimension — ``computeConvBusy`` must be byte-identical
       in behaviour whatever the channel health is. If a dead socket could flip
       busy to false, a running conversation would offer Send and hide Stop:
       a lost abort handle, strictly worse than the staleness being reported.
    8. a live local stream outranks channel health (bytes are arriving on that
       conv, so it cannot be stale).
    9. a timed-out / offline channel degrades an authoritative-only busy verdict
       to 'unconfirmed'.
   10. 'poor' latency stays confirmed — a slow link still delivers, and
       treating slow as stale would light the degraded UI on every mobile
       connection.

  Rendering (the part that makes it visible rather than internal plumbing):
   11. the unconfirmed flag reaches the DOM (dot + tag class);
   12. it is inside the sidebar's status hash, so a health flip actually
       repaints instead of being swallowed by the fast-path early return.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
from tests._runtime_sections import runtime_section_path

REDUCER = runtime_section_path('core/conv_state_reducer.js')
CONV_LIST = runtime_section_path('ui/conversation_list.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


@pytest.fixture(autouse=True)
def _clean_repair_state():
    from lib.conversations import drift_repair
    drift_repair.reset()
    yield
    drift_repair.reset()


# ═══════════════════════════════════════════════════════════════════
# A. Repair policy — pure, unit-testable without a push hub
# ═══════════════════════════════════════════════════════════════════

def _verdict(sustained: bool):
    """A tracker-shaped verdict. Mirrors observe_divergence's return keys."""
    return {'sustained': sustained, 'stalled': sustained, 'age': 200.0,
            'observations': 4, 'direction': 'client_behind',
            'severity': 'warning' if sustained else 'debug'}


def test_sustained_stall_warrants_a_repair():
    """Face 1 — the whole point: a detected stall must produce an action."""
    from lib.conversations.drift_repair import should_repair
    assert should_repair('sock-1', [_verdict(True)]) is True


def test_non_sustained_divergence_does_not_repair():
    """Face 2 — ordinary sampling lag must not spend a frame.

    The overwhelming majority of divergences are a healthy client whose 60s
    digest is simply older than a live server read. Repairing those would emit
    a frame per report per conv and drown the real signal — the same mistake
    that made the P5 WARNING log unusable before the tracker existed.
    """
    from lib.conversations.drift_repair import should_repair
    assert should_repair('sock-1', [_verdict(False)]) is False
    assert should_repair('sock-1', []) is False
    assert should_repair('sock-1', None) is False


def test_cooldown_suppresses_a_second_repair():
    """Face 3 — a repair that is not working must degrade to rare, not flood.

    A stalled client keeps reporting every 60s. Without a cooldown an unhealed
    stall emits a frame every minute for as long as it lasts — turning the
    repair into a slow flood exactly when the transport is already unhealthy.
    """
    from lib.conversations.drift_repair import (note_repair_attempt,
                                               repair_cooldown_sec,
                                               should_repair)
    v = [_verdict(True)]
    assert should_repair('sock-1', v, now=1000.0) is True
    note_repair_attempt('sock-1', now=1000.0)
    assert should_repair('sock-1', v, now=1000.0 + 10) is False, (
        'a second repair inside the cooldown must be suppressed')
    later = 1000.0 + repair_cooldown_sec() + 1
    assert should_repair('sock-1', v, now=later) is True, (
        'after the cooldown elapses a still-stalled socket may be repaired again')
    # A DIFFERENT socket is independent — one tab's cooldown must not shield
    # another tab that is also stalled.
    assert should_repair('sock-2', v, now=1000.0 + 10) is True


def test_socketless_client_is_still_repairable():
    """Face 4 (INVERTED — the original premise was falsified by owner review).

    This test used to assert the opposite: no socket id ⇒ never repair. That
    was correct only while the correction travelled DOWN the push socket, where
    an untargetable repair was genuinely undeliverable.

    Once the correction rides the HTTP response, the premise collapses — and
    keeping the lockout would have excluded exactly the population that needs
    self-heal most: a client whose WebSocket is blocked by a corporate proxy or
    tunnel has no socket id, no push channel to converge through, and therefore
    no way to recover short of a manual refresh. It is the least able to help
    itself, and the old rule refused it service.

    Anonymous clients share one cooldown bucket — coarser rate-limiting for
    clients we cannot tell apart, which is the right trade against never
    repairing them at all.
    """
    from lib.conversations.drift_repair import (note_repair_attempt,
                                                repair_cooldown_sec,
                                                should_repair)
    v = [_verdict(True)]
    assert should_repair('', v, now=1000.0) is True, (
        'a client with no push socket MUST still be repairable — the '
        'correction rides the HTTP response, which it just proved works by '
        'reaching this endpoint')
    assert should_repair(None, v, now=1000.0) is True

    # The shared bucket still rate-limits.
    note_repair_attempt('', now=1000.0)
    assert should_repair('', v, now=1000.0 + 10) is False, (
        'anonymous clients share one cooldown slot rather than bypassing the '
        'rate limit entirely')
    assert should_repair('', v, now=1000.0 + repair_cooldown_sec() + 1) is True

    # A non-sustained divergence is still not a repair, socket or no socket.
    assert should_repair('', [_verdict(False)], now=5000.0) is False


def test_failed_delivery_does_not_burn_the_cooldown():
    """Face 5 — the cooldown starts on DELIVERY, not on the decision.

    A socket living on another replica (or already gone) yields no delivery. If
    deciding-to-try started the clock, that one unlucky probe would suppress
    repair for the entire window even though nothing was ever sent.
    """
    from lib.conversations.drift_repair import should_repair, tracked_sockets
    v = [_verdict(True)]
    assert should_repair('sock-x', v, now=500.0) is True
    # Caller did NOT call note_repair_attempt (delivery failed).
    assert tracked_sockets() == 0
    assert should_repair('sock-x', v, now=500.0 + 1) is True, (
        'a repair that was never delivered must be retried on the next probe')


# ═══════════════════════════════════════════════════════════════════
# B. Transport — targeted delivery
# ═══════════════════════════════════════════════════════════════════

def test_hub_delivers_to_one_socket_only():
    """Face 6 — deliver_to_socket hits the named socket and nothing else."""
    from lib.agent_core.push import PushClient, PushHub

    hub = PushHub()
    a = PushClient(user_id='u1', req_id='rid-a')
    b = PushClient(user_id='u1', req_id='rid-b')
    hub.register(a)
    hub.register(b)

    frame = {'channel': 'notify', 'type': 'conv_state_snapshot', 'convs': {}}
    assert hub.deliver_to_socket('rid-a', frame) is True

    assert a._queue.qsize() == 1, 'the named socket must receive the frame'
    assert b._queue.qsize() == 0, (
        'a sibling socket must NOT receive a repair aimed at another tab — '
        'fanning out would mask the condition being repaired')


def test_hub_reports_unknown_socket_honestly():
    """An absent socket returns False so the caller can log a real negative."""
    from lib.agent_core.push import PushClient, PushHub
    hub = PushHub()
    hub.register(PushClient(user_id='u1', req_id='rid-a'))
    assert hub.deliver_to_socket('rid-zzz', {'x': 1}) is False
    assert hub.deliver_to_socket('', {'x': 1}) is False


def test_repair_does_not_depend_on_the_push_channel():
    """★ THE CIRCULAR DEPENDENCY — the repair must not ride the broken path.

    A client is judged sustained-stalled mostly BECAUSE notify frames stopped
    arriving, i.e. because the push socket is unhealthy. Sending the correction
    back over that same socket is therefore self-defeating: when the socket
    works the repair is unnecessary, and when it is broken the repair cannot
    land. Measured, three ways:

      * HALF-OPEN SOCKET — the client object is still registered and
        ``_ws.send`` never throws, so nothing errors; the queue simply fills
        (bounded at 1000) and the peer receives nothing. ``deliver_to_socket``
        still returned True, which the caller treated as DELIVERED and used to
        arm a 300s cooldown. The client most in need of repair got a false
        success plus five minutes of silence.
      * NO SOCKET AT ALL — WebSocket blocked by a corporate proxy / tunnel.
        ``deliver_to_socket`` returns False forever, so that population is
        permanently unrepairable. It is also the population that needs the
        repair most, since it has no push channel to self-heal through.
      * HTTP IS PROVEN ALIVE — the stall is detected FROM a digest POST that
        returned 200. At the exact moment we decide to repair, the HTTP path
        has just demonstrated it works.

    So the correction rides the HTTP RESPONSE: the channel the detection signal
    arrived on is the channel the correction goes back on, and that channel is
    self-evidently up.

    This guard pins the property, not the implementation: given a hub with NO
    live sockets whatsoever, the response must still carry a correction the
    client can apply.
    """
    import asyncio

    from lib.conversations import drift_repair
    from lib.conversations import drift_tracker

    from server import app

    drift_repair.reset()
    drift_tracker.reset()

    conv_id = 'conv-circular-probe'

    # Drive the tracker to a SUSTAINED stall: the client value frozen while the
    # server's moves, observed repeatedly, past the threshold. Uses the real
    # tracker rather than a fake verdict so the wiring under test is the
    # production one.
    t0 = 1000.0
    thresh = drift_tracker.sustained_threshold_sec()
    for i in range(4):
        v = drift_tracker.observe_divergence(
            conv_id, 'task_ids',
            client=['frozen'],                 # client never moves
            server=['moving-%d' % i],          # server advances
            now=t0 + i * (thresh / 2),
            observer_id='no-such-socket-anywhere')
    assert v['sustained'], (
        'precondition: the tracker must judge this a sustained stall; got %r' % v)

    async def _call():
        client = app.test_client()
        # A pushRid that names NO live socket — exactly the WS-blocked client,
        # and also what a half-open socket amounts to from the peer's side.
        resp = await client.post(
            '/api/v1/conversations/sync-digest',
            json={'digests': [{'convId': conv_id, 'taskIds': ['frozen'],
                               'rev': None}],
                  'pushRid': 'no-such-socket-anywhere'})
        return resp.status_code, await resp.get_json()

    status, body = asyncio.run(_call())
    drift_repair.reset()
    drift_tracker.reset()

    assert status == 200, 'the probe must stay a 200 diagnostic; got %s' % status
    assert body.get('snapshot') is not None, (
        'a sustained stall MUST return a corrective snapshot in the HTTP '
        'RESPONSE BODY. Delivering only over the push socket is circular: the '
        'socket is the thing that is broken, and a client behind a WS-blocking '
        'proxy has no socket at all. Response keys: %r' % sorted(body))
    snap = body['snapshot']
    assert isinstance(snap, dict) and 'convs' in snap, (
        'the correction must be the ORDINARY conv_state_snapshot shape so the '
        'existing reducer consumes it unchanged; got %r' % (snap,))
    assert isinstance(snap.get('rev'), list) and len(snap['rev']) == 2, (
        'the correction must carry the server-minted frame-level rev, else the '
        'client can clear but not advance its gate')


def test_cooldown_is_not_armed_by_an_unproven_push_enqueue():
    """The cooldown must never start on evidence that does not prove delivery.

    ``PushHub.deliver_to_socket`` returns True when a frame is ENQUEUED. On a
    half-open socket nothing drains that queue, so True means "accepted for
    sending", not "the peer has it" — measured: with the queue at its 1000-frame
    bound the enqueue still reports True while the peer receives nothing.

    Arming a 300s cooldown on that reading is the worst possible combination:
    the one client that genuinely needs repair is told it was repaired, and is
    then silenced for five minutes.

    A synchronous HTTP 200 IS proof of delivery — the response reached the
    client or the request failed. So the cooldown must be keyed to the HTTP
    path. Structural: assert the push enqueue's return value does not gate
    ``note_repair_attempt``.
    """
    import ast
    import inspect
    import textwrap

    from routes.api_v1 import conversations as conv_mod
    from tests._source_scan import strip_comments

    src = strip_comments(inspect.getsource(conv_mod.sync_digest), lang='python')
    tree = ast.parse(textwrap.dedent(src))

    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test_src = ast.unparse(node.test)
        if 'deliver_to_socket' not in test_src:
            continue
        body_src = '\n'.join(ast.unparse(s) for s in node.body)
        assert 'note_repair_attempt' not in body_src, (
            'note_repair_attempt must NOT be gated on deliver_to_socket: that '
            'return value means ENQUEUED, not DELIVERED, so on a half-open '
            'socket it arms a 300s cooldown for a repair the peer never got. '
            'Branch:\n  if %s:\n%s' % (test_src, body_src))


def test_repair_effectiveness_is_observable():
    """A repair whose outcome is never checked cannot be known to work.

    ``drift_repair`` sent and forgot: nothing recorded whether the correction
    actually made the client converge. In production that is indistinguishable
    from the mechanism silently doing nothing — the same "measured or it does
    not exist" rule this project applies to capabilities.

    The tracker already records convergence via ``observe_agreement``. The
    repair module must expose a way to tell that a socket which was repaired
    subsequently agreed, so the loop closes into evidence.
    """
    from lib.conversations import drift_repair

    assert hasattr(drift_repair, 'note_repair_outcome'), (
        'drift_repair must expose note_repair_outcome(socket_id, converged) so '
        'a repair can be judged effective or not')
    assert hasattr(drift_repair, 'repair_stats'), (
        'drift_repair must expose repair_stats() — without an attempted/'
        'converged tally there is no way to tell a working self-heal from one '
        'that never fires')

    drift_repair.reset()
    drift_repair.note_repair_attempt('sock-eff', now=100.0)
    drift_repair.note_repair_outcome('sock-eff', converged=True, now=160.0)
    stats = drift_repair.repair_stats()
    assert stats.get('attempted', 0) >= 1, 'attempts must be counted'
    assert stats.get('converged', 0) >= 1, 'successful repairs must be counted'

    drift_repair.reset()
    drift_repair.note_repair_attempt('sock-bad', now=100.0)
    drift_repair.note_repair_outcome('sock-bad', converged=False, now=160.0)
    stats = drift_repair.repair_stats()
    assert stats.get('ineffective', 0) >= 1, (
        'a repair that did NOT converge must be counted separately — that is '
        'the number that says the mechanism is spinning without effect')
    drift_repair.reset()


@pytest.mark.skipif(not _node_available(), reason='node not available')
def test_client_applies_the_in_band_repair_snapshot():
    """The correction must be APPLIED, not merely received.

    Drives the REAL ``reportSyncDigest`` with a stubbed Api whose response
    carries a repair snapshot, and asserts the conversation's authoritative
    state actually changed. Without this the server could return a perfect
    correction every 60s and the tab would stay just as stale — the exact
    "detected but never fixed" shape this epic exists to remove, moved one hop
    downstream.
    """
    harness = r"""
const fs = require('fs');
const path = require('path');
global.window = global;
global.debugLog = () => {};
global.saveConversations = () => {};
global.activeStreams = new Map();
global._currentUserId = null;
// Silence the reducer's diagnostics but KEEP console.log — it is this
// harness's only output channel back to the test.
const _log = console.log.bind(console);
global.console = { log: _log, warn: () => {}, error: () => {}, debug: () => {} };

const out = [];
function check(name, cond, detail) {
  out.push((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : '  :: ' + (detail || '')));
}

const REDUCER = process.argv[1];
(0, eval)(fs.readFileSync(REDUCER, 'utf8'));

const WALL = Date.now() * 1e6;
// A conv this tab believes is IDLE, holding an old rev — the frozen client.
const conv = { id: 'c1', _serverRev: 5,
               _authoritativeActiveTaskIds: new Set(),
               _authoritativeAttachableTaskIds: new Set(),
               _vuCarrierTaskIds: new Set(),
               _authoritativeActiveTaskIdsRev: [WALL - 1e9, 'r1'] };
global.conversations = [conv];

let repainted = 0;
global.renderConversationList = () => { repainted++; };
global.updateSendButton = () => {};

// Stub Api: the server judged us stalled and returns a corrective snapshot
// saying c1 IS running t-live.
global.Api = { conversations: { reportSyncDigest: async () => ({
  ok: true, checked: 1, repaired: true,
  divergences: [{ convId: 'c1', kind: 'task_ids', client: [], server: ['t-live'] }],
  snapshot: { channel: 'notify', type: 'conv_state_snapshot', userId: '',
              convs: { c1: { runningTaskIds: ['t-live'],
                             runningTaskIdsRev: [WALL + 1e6, 'r1'] } },
              rev: [WALL + 2e6, 'r1'] },
}) } };

reportSyncDigest(conversations).then(() => {
  check('repair_snapshot_applied',
        conv._authoritativeActiveTaskIds.has('t-live'),
        'set=' + JSON.stringify([...conv._authoritativeActiveTaskIds]) +
        ' — the tab must converge from the in-band correction, without F5');
  check('busy_now_true', computeConvBusy(conv, activeStreams) === true);
  check('sidebar_repainted', repainted >= 1,
        'applying a correction that is never rendered leaves the user looking '
        + 'at the same stale UI');
  console.log(out.join('\n'));
}).catch((e) => { console.log('FAIL harness_threw :: ' + (e && e.message)); });
"""
    proc = subprocess.run(['node', '-e', harness, REDUCER],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (
        'harness crashed (rc=%s)\nstdout:\n%s\nstderr:\n%s'
        % (proc.returncode, proc.stdout, proc.stderr))
    lines = proc.stdout.strip().splitlines()
    failed = [ln for ln in lines if ln.startswith('FAIL')]
    assert not failed, ('in-band repair application failed:\n  '
                        + '\n  '.join(failed))
    assert len(lines) >= 3, 'expected 3 checks, got:\n%s' % '\n'.join(lines)


@pytest.mark.skipif(not _node_available(), reason='node not available')
def test_client_repairs_rev_with_body_verify_not_task_snapshot():
    """Rev drift refreshes the active body and lazily marks background bodies."""
    harness = r"""
const fs = require('fs');
const path = require('path');
global.window = global;
global.debugLog = () => {};
global.activeStreams = new Map();
global.activeConvId = 'active';
global._editingMsgIdx = null;
const _log = console.log.bind(console);
global.console = { log: _log, warn: () => {}, error: () => {}, debug: () => {} };

const REDUCER = process.argv[1];
(0, eval)(fs.readFileSync(REDUCER, 'utf8'));

const active = { id: 'active', messages: [{role:'user', content:'old'}], _serverRev: 1 };
const background = { id: 'background', messages: [{role:'user', content:'old'}], _serverRev: 2 };
global.conversations = [active, background];
let verifies = 0;
global._verifyActiveConvFromServer = async (id) => {
  verifies++;
  if (id === 'active') active._serverRev = 99;
  return false;  // GET landed; content happened to be byte-identical
};
global.renderConversationList = () => {};
global.Api = { conversations: { reportSyncDigest: async () => ({
  ok: true, checked: 2, divergences: [
    {convId:'active', kind:'rev', client:1, server:99},
    {convId:'background', kind:'rev', client:2, server:88},
  ], reloadConvIds: ['active', 'background'], repaired: false, snapshot: null,
}) } };

reportSyncDigest(conversations).then(() => {
  const checks = [
    ['active_verified_once', verifies === 1],
    ['active_rev_adopted', active._serverRev === 99],
    ['active_no_longer_stale', active._needsLoad === false],
    ['background_marked_for_lazy_hydration', background._needsLoad === true],
  ];
  console.log(checks.map(([n, ok]) => (ok ? 'PASS ' : 'FAIL ') + n).join('\n'));
}).catch((e) => { console.log('FAIL harness_threw ' + (e && e.message)); });
"""
    proc = subprocess.run(['node', '-e', harness, REDUCER],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    failed = [ln for ln in proc.stdout.splitlines() if ln.startswith('FAIL')]
    assert not failed, '\n'.join(failed)


def test_probe_endpoint_wires_detection_to_repair():
    """The endpoint must actually CALL the repair seam — and REACHABLY.

    Faces 1-6 verify the pieces; this verifies they are connected. Structural
    (AST over the view's source) because forcing a real sustained stall through
    HTTP needs 180s of frozen reports. Comments are stripped first (charter
    #24) so prose describing the repair cannot satisfy this.

    ★ NAME-PRESENCE IS NOT ENOUGH — measured. A first version of this guard
    only asserted the symbols appeared somewhere in the function. Neutering the
    repair to ``if False and should_repair(...)`` — i.e. restoring the exact
    bug this epic exists to fix — left the whole suite GREEN, because every
    name was still lexically present. That is the same vacuous shape as a test
    named for a lane it never touches: the guard has to check the CONDITION the
    call hangs off, not just that the call was typed into the file.
    """
    import ast
    import inspect
    import textwrap

    from routes.api_v1 import conversations as conv_mod
    from tests._source_scan import strip_comments

    src = strip_comments(inspect.getsource(conv_mod.sync_digest), lang='python')
    tree = ast.parse(textwrap.dedent(src))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}

    assert 'should_repair' in names, (
        'sync_digest must consult drift_repair.should_repair — otherwise the '
        'server detects a stall and still does nothing about it')
    assert 'build_conv_state_snapshot' in names, (
        'the repair must reuse the ORDINARY connect snapshot, so the client '
        'needs no new frame handling and a repair is indistinguishable from a '
        'reconnect')
    assert 'deliver_to_socket' in attrs, (
        'the repair must be delivered to the reporting socket specifically')
    assert 'note_repair_attempt' in names, (
        'a delivered repair must arm the cooldown')

    # ── Reachability: the repair must be gated on should_repair ALONE ──
    # Find the `if` whose test contains the should_repair call, then assert the
    # test is not short-circuited by a constant (False/0/None) — the shape a
    # kill-switch or a debug edit takes.
    guards = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        called = any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                     and c.func.id == 'should_repair'
                     for c in ast.walk(node.test))
        if called:
            guards.append(node)
    assert guards, (
        'should_repair must gate an `if` — a call whose result is discarded '
        'detects the stall and still sends nothing')

    for g in guards:
        consts = [n for n in ast.walk(g.test)
                  if isinstance(n, ast.Constant)
                  and (n.value is False or n.value is None or n.value == 0)]
        assert not consts, (
            'the repair branch is short-circuited by a constant '
            '(e.g. `if False and should_repair(...)`), so a detected stall '
            'produces no correction. Guard expression: %s'
            % ast.unparse(g.test))
        # The delivery + cooldown must live INSIDE that branch, not merely
        # somewhere in the function.
        body_src = '\n'.join(ast.unparse(s) for s in g.body)
        assert 'deliver_to_socket' in body_src, (
            'the delivery must happen inside the should_repair branch; '
            'branch body:\n' + body_src)
        assert 'note_repair_attempt' in body_src, (
            'the cooldown must be armed inside the should_repair branch')


# ═══════════════════════════════════════════════════════════════════
# C. Client confidence + rendering
# ═══════════════════════════════════════════════════════════════════

_HARNESS = r"""
const fs = require('fs');
const path = require('path');
global.window = global;
global.debugLog = () => {};
global.saveConversations = () => {};
global.activeStreams = new Map();
global._currentUserId = null;

const out = [];
function check(name, cond, detail) {
  out.push((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : '  :: ' + (detail || '')));
}

const REDUCER = process.argv[1];
(0, eval)(fs.readFileSync(REDUCER, 'utf8'));

const M = new Map();
const WALL = Date.now() * 1e6;

// ── Face 7: confidence is a SEPARATE dimension; busy is untouched by health ──
{
  const convs = [{ id: 'c1' }], c = convs[0];
  applyRunningTaskIdsFrame(convs, { convId: 'c1', runningTaskIds: ['t1'],
                                    runningTaskIdsRev: [WALL, 'r1'] });
  resetAuthoritativeChannelHealthForTests();
  const busyHealthy = computeConvBusy(c, M);
  markAuthoritativeChannelHealth('timeout', true);
  const busyUnhealthy = computeConvBusy(c, M);
  markAuthoritativeChannelHealth('good', false);
  const busyOffline = computeConvBusy(c, M);
  check('F7_busy_is_invariant_to_channel_health',
        busyHealthy === true && busyUnhealthy === true && busyOffline === true,
        'busy must NOT change with channel health — a dead socket flipping busy '
        + 'to false would hide Stop on a running conv. got '
        + [busyHealthy, busyUnhealthy, busyOffline].join(','));
}

// ── Face 8: a live local stream outranks channel health ──
{
  const convs = [{ id: 'c1' }], c = convs[0];
  const streams = new Map([['c1', {}]]);
  markAuthoritativeChannelHealth('timeout', true);
  check('F8_live_local_stream_is_confirmed',
        computeConvStateConfidence(c, streams) === 'confirmed',
        'bytes are arriving on THIS conv, so it cannot be stale; got '
        + computeConvStateConfidence(c, streams));
}

// ── Face 9: timeout / offline degrade an authoritative-only verdict ──
{
  const convs = [{ id: 'c1' }], c = convs[0];
  applyRunningTaskIdsFrame(convs, { convId: 'c1', runningTaskIds: ['t1'],
                                    runningTaskIdsRev: [WALL, 'r1'] });
  resetAuthoritativeChannelHealthForTests();
  check('F9a_healthy_channel_is_confirmed',
        computeConvStateConfidence(c, M) === 'confirmed');
  markAuthoritativeChannelHealth('timeout', true);
  check('F9b_ping_timeout_is_unconfirmed',
        computeConvStateConfidence(c, M) === 'unconfirmed',
        'got ' + computeConvStateConfidence(c, M));
  markAuthoritativeChannelHealth('good', false);
  check('F9c_disconnected_is_unconfirmed',
        computeConvStateConfidence(c, M) === 'unconfirmed',
        'got ' + computeConvStateConfidence(c, M));
  markAuthoritativeChannelHealth('good', true);
  check('F9d_recovery_returns_to_confirmed',
        computeConvStateConfidence(c, M) === 'confirmed',
        'got ' + computeConvStateConfidence(c, M));
}

// ── Face 10: a slow-but-working link stays confirmed ──
{
  const convs = [{ id: 'c1' }], c = convs[0];
  for (const s of ['good', 'ok', 'poor', 'unknown']) {
    markAuthoritativeChannelHealth(s, true);
    check('F10_' + s + '_stays_confirmed',
          computeConvStateConfidence(c, M) === 'confirmed',
          s + ' must not be treated as stale (it still delivers)');
  }
}

// ── Default posture: before any reading exists, do NOT paint the page stale ──
{
  resetAuthoritativeChannelHealthForTests();
  check('F11_default_is_confirmed',
        authoritativeChannelHealthy() === true,
        'the degrade must be opt-IN on positive evidence of trouble');
}

console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _node_available(), reason='node not available')
def test_client_confidence_dimension():
    proc = subprocess.run(['node', '-e', _HARNESS, REDUCER],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (
        'harness crashed (rc=%s)\nstdout:\n%s\nstderr:\n%s'
        % (proc.returncode, proc.stdout, proc.stderr))
    lines = proc.stdout.strip().splitlines()
    failed = [ln for ln in lines if ln.startswith('FAIL')]
    assert not failed, ('confidence faces failed:\n  ' + '\n  '.join(failed)
                        + '\n\nfull:\n  ' + '\n  '.join(lines))
    assert len(lines) >= 11, (
        'expected the full confidence matrix, got %d:\n%s'
        % (len(lines), '\n'.join(lines)))


def test_unconfirmed_reaches_the_dom():
    """Face 11 — the flag must render, not just exist in the reducer.

    A confidence dimension nobody draws is invisible plumbing: the user would
    still be shown a stale state as settled fact, which is the reported
    symptom. Comments stripped first (charter #24).
    """
    from tests._source_scan import strip_comments
    src = strip_comments(open(CONV_LIST, encoding='utf-8').read(), lang='js')

    assert 'computeConvStateConfidence' in src, (
        'the sidebar must consult the confidence predicate')
    assert 'unconfirmed' in src, 'the flag must exist in the status flags'
    assert 'conv-state-unconfirmed' in src, (
        'the degraded state must reach the DOM as a class the stylesheet can '
        'target')


def test_unconfirmed_is_in_the_status_hash():
    """Face 12 — otherwise the repaint never fires.

    renderConversationList early-returns when its status hash is unchanged. A
    confidence flip changes no other flag, so if ``unconfirmed`` is absent from
    that hash the socket can go down, the flag can flip, and the DOM keeps the
    confident dot until some unrelated change happens to bump the hash. The
    feature would appear to work in a unit test and do nothing in the browser.
    """
    from tests._source_scan import strip_comments
    src = strip_comments(open(CONV_LIST, encoding='utf-8').read(), lang='js')

    m = re.search(r'_statusHash\s*=(.*?)\.join\(', src, re.DOTALL)
    assert m, 'could not locate the sidebar status hash construction'
    block = m.group(1)
    assert 'f.streaming' in block, (
        'located the wrong block — expected the per-conv status flags; got:\n'
        + block)
    assert 'unconfirmed' in block, (
        'f.unconfirmed MUST be part of _statusHash, else a channel-health flip '
        'does not invalidate the cached hash and the sidebar never repaints:\n'
        + block)


def test_i18n_key_exists_for_the_degraded_state():
    """A rendered title must not ship as a literal key.

    Measured precedent in this project: a tooltip shipped as the literal string
    ``project.qrScan`` because the render guard never checked the dictionary.
    """
    import json
    locale_root = os.path.join(ROOT, 'frontend', 'src', 'i18n', 'locales')
    for language in ('zh', 'en'):
        with open(os.path.join(locale_root, language + '.json'), encoding='utf-8') as handle:
            catalog = json.load(handle)
        assert catalog.get('sidebar.stateUnconfirmed'), language


def test_css_targets_the_degraded_state():
    """The class must have a rule, or the degrade is semantically present but
    visually identical to a confident dot."""
    css = open(os.path.join(ROOT, 'static', 'styles.css'), encoding='utf-8').read()
    assert '.conv-state-unconfirmed' in css, (
        'styles.css must style the unconfirmed state, else the flag reaches the '
        'DOM and changes nothing the user can see')
