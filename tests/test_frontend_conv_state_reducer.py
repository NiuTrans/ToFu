"""tests/test_frontend_conv_state_reducer.py — pt_conv_state_ssot P2:
frontend reducer for server-authoritative busy state.

Owner hard constraints (2026-07-24, board pt_e1c4693341b24730):

  * Bifurcated fields: ``conv._optimisticActiveTaskIds`` (local send) vs
    ``conv._authoritativeActiveTaskIds`` (server frames only). ``convIsBusy``
    reads the UNION; NEVER merge/overwrite the two into one field.
  * Task registry is the ONLY physical SSOT — reducer NEVER touches
    ``settings.activeTaskId``.
  * ``runningTaskIdsRev`` is a ``[monotonic_ns, replica_id]`` tuple; a stale
    frame (older rev) must be a no-op via a plain lex compare.

Backward-compat pragma: the shipped codebase writes ``conv.activeTaskId``
in ~20 sender/regen/edit/continue/reconnect sites. Rather than touch every
writer this phase, ``conv.activeTaskId`` is redefined as the LOCAL
optimistic single-value alias — the writer contract is unchanged; the
reducer ADDS ``_authoritativeActiveTaskIds`` (Set) as a parallel field,
and ``convIsBusy`` reads the UNION. That way a stale ``activeTaskId``
left after a bad shutdown can be superseded by a server snapshot without
a schema migration, and the phone-vs-PC visible bug (background conv
whose ``activeTaskId`` is null in this tab because
``loadConversationsFromServer`` refuses to overwrite it) is fixed by the
server-authoritative Set lighting the dot.

Faces (failing-first; NEUTER-verified where the invariant is non-trivial):

  1. convIsBusy reads UNION: authoritative-only conv (activeTaskId null,
     authoritative Set non-empty) → busy=true. Local-only (Set empty,
     activeTaskId set) → busy=true. Both empty → false.
  2. notify frame with runningTaskIds writes ``_authoritativeActiveTaskIds``
     AND stamps ``_authoritativeActiveTaskIdsRev``.
  3. Older-rev frame → no-op (idempotent gate on lex compare).
  4. conv_state_snapshot frame overwrites ALL convs (full projection), and
     a conv NOT present in the snapshot has its authoritative Set CLEARED
     (a server that no longer has the conv running must extinguish the dot).
  5. Reducer NEVER writes ``settings.activeTaskId`` (assert no call to
     saveConversations for pure server-authoritative frame consumption on
     a background conv — actually zero write of any settings key).
  6. _reconnectServerTaskIfIdle prefers a taskId from ``_authoritativeActiveTaskIds``
     when the local ``activeTaskId`` is null (the phone-vs-PC scenario:
     PC's conv.activeTaskId=null because loadConversationsFromServer refuses
     to overwrite it, but the authoritative Set has the phone's task).
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CONV_STATE_REDUCER = runtime_section_path('core/conv_state_reducer.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
global.window = global;

const out = [];
function check(name, cond) {
  out.push((cond ? 'PASS ' : 'FAIL ') + name);
}

// Load required source files in the order the bundler ships them:
// core/conv_state_reducer.js is P2's NEW module (must exist by the end of P2).
// It defines: applyRunningTaskIdsFrame({convId, runningTaskIds, runningTaskIdsRev}),
//             applyConvStateSnapshot({convs, userId}),
//             convIsBusy(conv), and reads/writes conv._authoritativeActiveTaskIds +
//             conv._authoritativeActiveTaskIdsRev.
function loadModule(rel) {
  const p = process.argv[2];
  const src = fs.readFileSync(p, 'utf8');
  (0, eval)(src);
}

global.debugLog = () => {};
global.saveConversations = () => { saveCalls++; };
let saveCalls = 0;
global.activeStreams = new Map();
global._currentUserId = null;

loadModule('core/conv_state_reducer.js');

check('reducer_exposes_applyRunningTaskIdsFrame',
      typeof applyRunningTaskIdsFrame === 'function');
check('reducer_exposes_applyConvStateSnapshot',
      typeof applyConvStateSnapshot === 'function');
check('reducer_exposes_computeConvBusy',
      typeof computeConvBusy === 'function');
check('reducer_exposes_pickAuthoritativeTaskIdForReconnect',
      typeof pickAuthoritativeTaskIdForReconnect === 'function');

// ─────────────────────────────────────────────────────────────────
// Face 1: convIsBusy reads UNION of optimistic (activeTaskId) + authoritative Set
// ─────────────────────────────────────────────────────────────────
{
  saveCalls = 0;
  activeStreams = new Map();
  const c = { id: 'c1' };
  check('idle_conv_not_busy', computeConvBusy(c, activeStreams) === false);

  c.activeTaskId = 'tid-local';
  check('optimistic_only_busy', computeConvBusy(c, activeStreams) === true);

  delete c.activeTaskId;
  c._authoritativeActiveTaskIds = new Set(['tid-server']);
  check('authoritative_only_busy', computeConvBusy(c, activeStreams) === true);

  c.activeTaskId = 'tid-local';
  check('both_busy', computeConvBusy(c, activeStreams) === true);

  c._authoritativeActiveTaskIds = new Set();
  delete c.activeTaskId;
  check('both_empty_not_busy', computeConvBusy(c, activeStreams) === false);

  activeStreams.set('c1', {});
  check('active_stream_wins_regardless', computeConvBusy(c, activeStreams) === true);
}

// ─────────────────────────────────────────────────────────────────
// Face 2: applyRunningTaskIdsFrame writes authoritative Set + stamps rev
// ─────────────────────────────────────────────────────────────────
{
  saveCalls = 0;
  const c = { id: 'c1' };
  const conversations = [c];
  applyRunningTaskIdsFrame(conversations, {
    convId: 'c1',
    runningTaskIds: ['tid-A', 'tid-B'],
    runningTaskIdsRev: [1000, 'r1'],
  });
  check('authoritative_set_written',
        c._authoritativeActiveTaskIds instanceof Set &&
        c._authoritativeActiveTaskIds.has('tid-A') &&
        c._authoritativeActiveTaskIds.has('tid-B'));
  check('authoritative_rev_stamped',
        Array.isArray(c._authoritativeActiveTaskIdsRev) &&
        c._authoritativeActiveTaskIdsRev[0] === 1000 &&
        c._authoritativeActiveTaskIdsRev[1] === 'r1');
  check('reducer_never_writes_activeTaskId_optimistic',
        c.activeTaskId === undefined);
  check('reducer_no_settings_save', saveCalls === 0);
}

// A terminal event can beat a delayed running projection. Its tombstone must
// suppress only the completed task while preserving unrelated live work.
{
  const c = {
    id: 'c-terminal',
    _terminalTaskIds: new Set(['tid-finished']),
  };
  applyRunningTaskIdsFrame([c], {
    convId: 'c-terminal',
    runningTaskIds: ['tid-finished', 'tid-other', 'tid-vu#vu'],
    runningTaskIdsRev: [7000, 'r1'],
  });
  check('terminal_tombstone_blocks_busy_resurrection',
        !c._authoritativeActiveTaskIds.has('tid-finished'));
  check('terminal_tombstone_blocks_reconnect_resurrection',
        !c._authoritativeAttachableTaskIds.has('tid-finished'));
  check('terminal_tombstone_preserves_other_live_task',
        c._authoritativeActiveTaskIds.has('tid-other') &&
        c._authoritativeAttachableTaskIds.has('tid-other'));
  check('terminal_tombstone_preserves_vu_carrier',
        c._vuCarrierTaskIds.has('tid-vu'));
}

// ─────────────────────────────────────────────────────────────────
// Face 3: older-rev frame is a no-op (idempotent lex compare)
// ─────────────────────────────────────────────────────────────────
{
  saveCalls = 0;
  const c = { id: 'c1',
              _authoritativeActiveTaskIds: new Set(['tid-current']),
              _authoritativeActiveTaskIdsRev: [5000, 'r1'] };
  // Older ns on the SAME replica — must be dropped.
  applyRunningTaskIdsFrame([c], {
    convId: 'c1',
    runningTaskIds: ['tid-stale'],
    runningTaskIdsRev: [1000, 'r1'],
  });
  check('older_rev_dropped',
        c._authoritativeActiveTaskIds.has('tid-current') &&
        !c._authoritativeActiveTaskIds.has('tid-stale'));

  // Same ns, replica_id lex-smaller → tiebreak drops.
  // Current rev is [5000, 'r1']; frame [5000, 'r0'] has 'r0' < 'r1' → drop.
  applyRunningTaskIdsFrame([c], {
    convId: 'c1',
    runningTaskIds: ['tid-stale2'],
    runningTaskIdsRev: [5000, 'r0'],
  });
  check('older_rev_dropped_lex_tiebreak',
        !c._authoritativeActiveTaskIds.has('tid-stale2') &&
        c._authoritativeActiveTaskIds.has('tid-current'));

  // Newer ns → accepted (state was never mutated by the two dropped frames).
  applyRunningTaskIdsFrame([c], {
    convId: 'c1',
    runningTaskIds: ['tid-newer'],
    runningTaskIdsRev: [6000, 'r1'],
  });
  check('newer_rev_accepted',
        c._authoritativeActiveTaskIds.has('tid-newer') &&
        !c._authoritativeActiveTaskIds.has('tid-current'));
}

// ─────────────────────────────────────────────────────────────────
// Face 4: conv_state_snapshot overwrites all convs; missing conv CLEARED
// ─────────────────────────────────────────────────────────────────
{
  saveCalls = 0;
  const conversations = [
    { id: 'c1', _authoritativeActiveTaskIds: new Set(['old-A']),
      _authoritativeActiveTaskIdsRev: [100, 'r0'] },
    { id: 'c2', _authoritativeActiveTaskIds: new Set(['old-B']),
      _authoritativeActiveTaskIdsRev: [100, 'r0'] },
    { id: 'c3' },   // no prior authoritative field
  ];
  applyConvStateSnapshot(conversations, {
    userId: 1,
    convs: {
      // c1: still running with a new task
      c1: { runningTaskIds: ['new-A'], runningTaskIdsRev: [2000, 'r1'] },
      // c3: freshly started
      c3: { runningTaskIds: ['new-C'], runningTaskIdsRev: [2001, 'r1'] },
      // c2 ABSENT — snapshot says it is no longer running → must clear
    },
  });
  check('snapshot_c1_updated',
        conversations[0]._authoritativeActiveTaskIds.has('new-A') &&
        !conversations[0]._authoritativeActiveTaskIds.has('old-A'));
  check('snapshot_c2_cleared_when_absent',
        conversations[1]._authoritativeActiveTaskIds.size === 0);
  check('snapshot_c3_added',
        conversations[2]._authoritativeActiveTaskIds.has('new-C'));
  check('snapshot_no_settings_save', saveCalls === 0);
}

// ─────────────────────────────────────────────────────────────────
// Face 4b: snapshot NEVER writes settings.activeTaskId (SSOT invariant)
// ─────────────────────────────────────────────────────────────────
{
  saveCalls = 0;
  const conversations = [{ id: 'c1' }];
  applyConvStateSnapshot(conversations, {
    userId: 1,
    convs: { c1: { runningTaskIds: ['tid'], runningTaskIdsRev: [1, 'r0'] } },
  });
  check('snapshot_does_not_write_settings', saveCalls === 0);
  // Also assert we did not touch settings itself.
  check('snapshot_does_not_touch_settings_obj',
        conversations[0].settings === undefined);
}

// ─────────────────────────────────────────────────────────────────
// Face 5: pickAuthoritativeTaskIdForReconnect prefers Set when local is null.
// NOTE: the picker reads the ATTACHABLE set (_authoritativeAttachableTaskIds),
// NOT the busy set (_authoritativeActiveTaskIds) — since 7daf7c28 a VU carrier
// lights the busy dot but can never complete a stream, so it must NOT be a
// reconnect target. This face therefore seeds the attachable set; the busy set
// alone (carrier-only) must yield null (checked at the bottom of this face).
// ─────────────────────────────────────────────────────────────────
{
  const c1 = { id: 'c1',
               _authoritativeAttachableTaskIds: new Set(['tid-server']) };
  check('reconnect_picks_from_set_when_local_null',
        pickAuthoritativeTaskIdForReconnect(c1) === 'tid-server');

  const c2 = { id: 'c2', activeTaskId: 'tid-local',
               _authoritativeAttachableTaskIds: new Set(['tid-server']) };
  // Local wins when present — local reflects THIS tab's own send, which
  // is the natural reconnect target.
  check('reconnect_prefers_local_when_present',
        pickAuthoritativeTaskIdForReconnect(c2) === 'tid-local');

  const c3 = { id: 'c3' };
  check('reconnect_null_when_both_empty',
        pickAuthoritativeTaskIdForReconnect(c3) === null);

  const c4 = { id: 'c4', _authoritativeAttachableTaskIds: new Set() };
  check('reconnect_null_when_set_empty', pickAuthoritativeTaskIdForReconnect(c4) === null);

  // 7daf7c28 invariant: a VU carrier is BUSY but never ATTACHABLE. A conv whose
  // only live worker is a carrier (busy set non-empty, attachable set absent /
  // empty) must return null — offering it would resurrect the permanently-stuck
  // "Waiting…" bubble the carrier filter exists to prevent.
  const c5 = { id: 'c5', _authoritativeActiveTaskIds: new Set(['tid-vu-carrier']) };
  check('reconnect_null_for_carrier_only',
        pickAuthoritativeTaskIdForReconnect(c5) === null);
}

// ─────────────────────────────────────────────────────────────────
// Face 6: multi-user gate — a frame for a different user is dropped.
// ─────────────────────────────────────────────────────────────────
{
  global._currentUserId = 1;
  const c = { id: 'c1' };
  applyRunningTaskIdsFrame([c], {
    convId: 'c1',
    runningTaskIds: ['tid-other'],
    runningTaskIdsRev: [1, 'r0'],
    userId: 2,   // NOT us
  });
  check('cross_user_frame_dropped',
        c._authoritativeActiveTaskIds === undefined);
  applyConvStateSnapshot([c], {
    userId: 2,   // NOT us
    convs: { c1: { runningTaskIds: ['tid-other2'], runningTaskIdsRev: [2, 'r0'] } },
  });
  check('cross_user_snapshot_dropped',
        c._authoritativeActiveTaskIds === undefined);
  // When our identity is unset (single-user default) everything applies.
  global._currentUserId = null;
  applyRunningTaskIdsFrame([c], {
    convId: 'c1',
    runningTaskIds: ['tid-ours'],
    runningTaskIdsRev: [10, 'r0'],
    userId: 42,   // ignored because we have no identity → forward-safe accept
  });
  check('no_local_user_accepts_frame',
        c._authoritativeActiveTaskIds &&
        c._authoritativeActiveTaskIds.has('tid-ours'));
}

console.log(out.join('\n'));
process.exit(0);
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_conv_state_reducer_frontend():
    harness = os.path.join(HERE, '_conv_state_reducer_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, CONV_STATE_REDUCER],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'reducer failures:\n' + output
    passes = [ln for ln in output.splitlines() if ln.startswith('PASS')]
    assert len(passes) >= 24, f'expected >=24 PASS, got {len(passes)}:\n{output}'
