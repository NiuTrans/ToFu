"""tests/test_frontend_pending_busy_state.py — pt_e1c4693341b24730 follow-up.

THE HOLE (owner-reproduced, 2026-07-26)
---------------------------------------
A conversation CREATED ON ANOTHER DEVICE never lights its busy dot here.

  1. Phone creates conv `brand-new` and starts generating.
  2. Server emits notify with ``runningTaskIds:['tid-phone']``.
  3. PC's ``applyRunningTaskIdsFrame`` does
     ``conversations.find(c => c.id === convId)`` → **undefined** (the PC has
     never seen this conv) → silent ``return``. THE AUTHORITATIVE BUSY FACT IS
     DISCARDED.
  4. ``_onConvNotifyPush`` separately notices the unknown conv and schedules a
     debounced ``loadConversationsFromServer()``, which discovers it ~400 ms
     later — but the list payload carries NO busy field (verified: zero hits
     for ``runningTaskIds`` in routes/conversations.py + api_v1/conversations.py)
     and the list-load path never writes ``_authoritativeActiveTaskIds``.

Net effect: the conv appears in the sidebar as IDLE while the phone is
actively generating, until some later notify frame happens to arrive (needs a
message to land or the task to finish) or the user hits F5.

``applyConvStateSnapshot`` has the SAME hole from the other direction: it
iterates the LOCAL ``conversations`` array, so a snapshot entry for a conv the
client hasn't loaded is skipped entirely.

THE FIX SHAPE (owner-directed)
------------------------------
Stash, don't discard. An authoritative frame for an unknown conv parks in a
pending map; when the conv later materialises (list refresh, cold-open
hydration, anything) the parked state is applied.

Explicitly REJECTED alternative: teaching the conversation-list endpoint to
return ``runningTaskIds``. That would create a SECOND busy-state source and
violate hard constraint #3 of pt_e1c4693341b24730 — the task registry is the
only physical SSOT, reaching the client through exactly one channel.

Invariants this suite pins:

  1. A frame for an unknown conv is PARKED, not dropped.
  2. Replaying after the conv materialises lights the dot.
  3. The parked entry is rev-gated like any other: a newer parked frame wins,
     an older one is ignored.
  4. Parking is bounded — an unbounded map is a leak, since a frame can name a
     conv this client will never load (deleted, or another tenant's).
  5. Snapshot entries for unknown convs park too.
  6. A parked entry is consumed exactly once (no resurrection after the conv is
     later legitimately cleared).
  7. Replay is idempotent and safe when nothing is parked.
"""

from __future__ import annotations

import os

import pytest

from tests._jsdom import JS_DIR, node_deps_available, run_harness

pytestmark = pytest.mark.unit

_REDUCER = os.path.join(JS_DIR, 'core', 'conv_state_reducer.js')


_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body></body>',
  targets: [process.argv[2]],
  globals: {
    debugLog: () => {},
    saveConversations: () => {},
    activeStreams: new Map(),
    conversations: [],
  },
});

const NEUTER = process.env.NEUTER || '';

function fresh() {
  if (typeof window.resetPendingBusyStateForTests === 'function') {
    window.resetPendingBusyStateForTests();
  }
  window._currentUserId = null;
  return [];
}

/* ── 1. Unknown conv → PARKED, not discarded ───────────────────────── */
{
  const convs = fresh();
  window.applyRunningTaskIdsFrame(convs, {
    convId: 'brand-new', runningTaskIds: ['tid-phone'],
    runningTaskIdsRev: [100, 'r0'], userId: 1,
  });
  check('unknown_conv_frame_is_parked',
        typeof window.pendingBusyStateSize === 'function'
        && window.pendingBusyStateSize() === 1);
}

/* ── 2. Replay after the conv materialises lights the dot ──────────── */
{
  const convs = fresh();
  window.applyRunningTaskIdsFrame(convs, {
    convId: 'brand-new', runningTaskIds: ['tid-phone'],
    runningTaskIdsRev: [100, 'r0'], userId: 1,
  });
  // The debounced list refresh discovers the conv.
  const conv = { id: 'brand-new' };
  convs.push(conv);
  window.replayPendingBusyState(convs);
  check('replay_lights_the_busy_dot',
        window.computeConvBusy(conv, window.activeStreams) === true);
  check('replay_writes_authoritative_set',
        !!(conv._authoritativeActiveTaskIds
           && conv._authoritativeActiveTaskIds.has('tid-phone')));
}

/* ── 3. Parked entries are rev-gated ───────────────────────────────── */
{
  const convs = fresh();
  window.applyRunningTaskIdsFrame(convs, {
    convId: 'x', runningTaskIds: ['old'], runningTaskIdsRev: [100, 'r0'], userId: 1,
  });
  window.applyRunningTaskIdsFrame(convs, {
    convId: 'x', runningTaskIds: ['new'], runningTaskIdsRev: [200, 'r0'], userId: 1,
  });
  window.applyRunningTaskIdsFrame(convs, {
    convId: 'x', runningTaskIds: ['stale'], runningTaskIdsRev: [150, 'r0'], userId: 1,
  });
  const conv = { id: 'x' };
  convs.push(conv);
  window.replayPendingBusyState(convs);
  check('parked_newest_rev_wins',
        !!(conv._authoritativeActiveTaskIds
           && conv._authoritativeActiveTaskIds.has('new')));
  check('parked_older_rev_ignored',
        !!(conv._authoritativeActiveTaskIds
           && !conv._authoritativeActiveTaskIds.has('stale')
           && !conv._authoritativeActiveTaskIds.has('old')));
}

/* ── 4. Parking is bounded (a frame can name a conv we never load) ─── */
{
  fresh();
  for (let i = 0; i < 400; i++) {
    window.applyRunningTaskIdsFrame([], {
      convId: 'ghost-' + i, runningTaskIds: ['t'],
      runningTaskIdsRev: [i, 'r0'], userId: 1,
    });
  }
  const size = window.pendingBusyStateSize();
  check('pending_map_is_bounded', size > 0 && size <= 200);
}

/* ── 5. Snapshot entries for unknown convs park too ────────────────── */
{
  const convs = fresh();
  convs.push({ id: 'known' });
  window.applyConvStateSnapshot(convs, {
    userId: 1,
    convs: {
      known:  { runningTaskIds: ['t-known'],  runningTaskIdsRev: [10, 'r'] },
      unseen: { runningTaskIds: ['t-unseen'], runningTaskIdsRev: [11, 'r'] },
    },
  });
  check('snapshot_known_conv_applied_directly',
        window.computeConvBusy(convs[0], window.activeStreams) === true);
  const unseen = { id: 'unseen' };
  convs.push(unseen);
  window.replayPendingBusyState(convs);
  check('snapshot_unknown_conv_parked_then_replayed',
        window.computeConvBusy(unseen, window.activeStreams) === true);
}

/* ── 6. A parked entry is consumed exactly once ────────────────────── */
{
  const convs = fresh();
  window.applyRunningTaskIdsFrame(convs, {
    convId: 'once', runningTaskIds: ['t1'],
    runningTaskIdsRev: [100, 'r0'], userId: 1,
  });
  const conv = { id: 'once' };
  convs.push(conv);
  window.replayPendingBusyState(convs);
  check('consumed_entry_leaves_map_empty', window.pendingBusyStateSize() === 0);
  // The task finished; a normal frame clears the set.
  window.applyRunningTaskIdsFrame(convs, {
    convId: 'once', runningTaskIds: [],
    runningTaskIdsRev: [200, 'r0'], userId: 1,
  });
  // A second replay must NOT resurrect the stale parked value.
  window.replayPendingBusyState(convs);
  check('replay_does_not_resurrect_cleared_state',
        window.computeConvBusy(conv, window.activeStreams) === false);
}

/* ── 7. Replay is a safe no-op when nothing is parked ──────────────── */
{
  const convs = fresh();
  const conv = { id: 'quiet' };
  convs.push(conv);
  let threw = false;
  try { window.replayPendingBusyState(convs); } catch (e) { threw = true; }
  check('empty_replay_does_not_throw', threw === false);
  check('empty_replay_leaves_conv_idle',
        window.computeConvBusy(conv, window.activeStreams) === false);
}

report();
process.exit(0);
"""


def test_unknown_conv_busy_state_is_parked_and_replayed():
    """An authoritative busy frame for a conv this client has not loaded yet
    must survive until the conv materialises — the 'phone created a new conv,
    PC shows it idle' hole."""
    run_harness(
        target_js=_REDUCER,
        body_js=_BODY,
        min_pass=11,
        label='pending busy-state park/replay',
    )


def test_list_endpoint_still_has_no_busy_field():
    """GUARD (SSOT hard constraint #3): the fix must NOT be 'make the list
    endpoint return runningTaskIds'. The task registry reaches the client
    through the push channel ONLY. If a second source appears here, the two
    can drift and the whole epic's premise is broken.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for rel in ('routes/conversations.py', 'routes/api_v1/conversations.py'):
        with open(os.path.join(root, rel), encoding='utf-8') as f:
            src = f.read()
        assert 'runningTaskIds' not in src, (
            f'{rel} now emits runningTaskIds — that is a SECOND busy-state '
            'source. pt_e1c4693341b24730 hard constraint #3: the task '
            'registry is the ONLY physical SSOT and reaches the client via '
            'the push channel alone. Park-and-replay in the reducer is the '
            'sanctioned fix for unknown convs.')


def test_list_refresh_replays_parked_state():
    """WIRING. The reducer half is inert unless something calls the replay
    after the conversations array grows. loadConversationsFromServer IS that
    moment — it is what turns an unknown conv into a known one.

    Pinned structurally because the failure mode is silent: drop the call and
    every unit test above still passes while the real cross-device scenario
    regresses.
    """
    convs_js = os.path.join(JS_DIR, 'core', 'conversations.js')
    with open(convs_js, encoding='utf-8') as f:
        src = f.read()
    assert 'replayPendingBusyState(conversations)' in src, (
        'core/conversations.js must replay parked busy state after the '
        'server merge — otherwise a conv created on another device is '
        'discovered by the list refresh but stays visually idle')
    # It must run inside the list loader, and BEFORE the render that paints
    # the dot — a replay after the paint shows stale state for one frame.
    loader_at = src.index('async function loadConversationsFromServer')
    replay_at = src.index('replayPendingBusyState(conversations)')
    assert replay_at > loader_at, (
        'the replay call must live inside loadConversationsFromServer')
    render_at = src.index('renderConversationList();', replay_at)
    assert replay_at < render_at, (
        'replay must run BEFORE the sidebar render so the busy dot paints in '
        'the same frame the conv first appears')


@pytest.mark.parametrize('neuter', ['park', 'replay'])
def test_NEUTER_park_and_replay_are_load_bearing(neuter):
    """Strip the parking (or the replay) and the cross-device scenario breaks
    again — proving both halves carry the fix."""
    if not node_deps_available():
        pytest.skip('node + jsdom dev-deps not installed')
    import re
    import subprocess
    import tempfile

    with open(_REDUCER, encoding='utf-8') as f:
        src = f.read()

    if neuter == 'park':
        # Restore the old silent bail: unknown conv → return, nothing parked.
        neutered = re.sub(
            r'if \(!conv\) \{\s*_parkPendingBusyState\([^;]*?\);\s*return;\s*\}',
            'if (!conv) { return; }', src, count=1, flags=re.S)
    else:
        # Replay becomes a no-op.
        neutered = re.sub(
            r'function replayPendingBusyState\(conversations\) \{',
            'function replayPendingBusyState(conversations) { return;', src,
            count=1)
    assert neutered != src, f'NEUTER({neuter}) anchor not found'

    tmp = []
    try:
        with tempfile.NamedTemporaryFile('w', suffix='.js',
                                         dir=os.path.dirname(_REDUCER),
                                         delete=False, encoding='utf-8') as fh:
            path = fh.name
            fh.write(neutered)
        tmp.append(path)
        harness_dir = os.path.dirname(os.path.abspath(__file__))
        with tempfile.NamedTemporaryFile('w', suffix='.js', dir=harness_dir,
                                         delete=False, encoding='utf-8') as hf:
            harness = hf.name
            hf.write(_BODY)
        tmp.append(harness)
        root = os.path.dirname(harness_dir)
        proc = subprocess.run(
            ['node', harness, path, root],
            capture_output=True, text=True, timeout=60,
            env={**os.environ,
                 'JSDOM_HARNESS': os.path.join(harness_dir, '_jsdom_harness.js')},
        )
        out = (proc.stdout or '') + (proc.stderr or '')
        assert 'FAIL' in out or proc.returncode != 0, (
            f'NEUTER({neuter}) did not bite — the cross-device scenario still '
            f'passed with that half removed:\n{out}')
    finally:
        for p in tmp:
            try:
                os.remove(p)
            except OSError:
                pass
