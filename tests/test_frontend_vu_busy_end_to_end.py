#!/usr/bin/env python3
"""END-TO-END (payload → reducer → busy UI state) for the VU busy signal.

WHY (owner-mandated, ms34u49egqwhug)
------------------------------------
The incident was a THREE-LAYER chain and fixing only the backend fact left the
user-visible bug intact. The owner's acceptance criterion is NOT "the backend
reports busy" — it is "the sidebar and composer show GENERATING". Between the
two sit: the notify payload, ``conv_state_reducer.js``'s
``applyRunningTaskIdsFrame``, and ``computeConvBusy``'s mapping of
``_authoritativeActiveTaskIds`` → UI busy state.

This suite drives the REAL reducer in a jsdom harness against the REAL wire
shape the backend now emits, and asserts the UI-side outcome — not the backend
projection. The middle layer is where the "busy and idle looked identical on
the wire" failure lived.

WIRE CONTRACT UNDER TEST
------------------------
  * a conv with a normal running worker    → runningTaskIds = [tid]
  * a conv whose only worker is a VU carrier → runningTaskIds = ['<tid>#vu']
    (the carrier id is NOT independently attachable; the '#vu' marker is the
    signal that means "busy, but do not offer this as a reconnect target")
  * a conv with NO live work               → runningTaskIds = []

INVARIANTS
----------
  1. A '#vu'-marked frame still lights the busy dot (Set non-empty after the
     marker is stripped).
  2. The marker never becomes an attach target — the raw '#vu' string must not
     appear in the busy Set a reconnect would read.
  3. An empty list still reads idle (the signal is not pinned on).
  4. A snapshot carrying a '#vu' entry lights the dot identically.
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

function freshConvs() {
  if (typeof window.resetPendingBusyStateForTests === 'function') {
    window.resetPendingBusyStateForTests();
  }
  window._currentUserId = null;
  return [];
}

/* ── 1. A '#vu'-marked frame lights the busy dot ───────────────────── */
{
  const convs = freshConvs();
  const conv = { id: 'ms34u49egqwhug' };
  convs.push(conv);
  window.applyRunningTaskIdsFrame(convs, {
    convId: conv.id,
    runningTaskIds: ['632a6f3c-8536-4c6f-b140-2db9a55ecee0#vu'],
    runningTaskIdsRev: [100, 'r0'],
    userId: 1,
  });
  check('vu_marked_frame_lights_the_dot',
        window.computeConvBusy(conv, window.activeStreams) === true);
}

/* ── 2. The marker never becomes an attach target ──────────────────── */
{
  const convs = freshConvs();
  const conv = { id: 'c' };
  convs.push(conv);
  window.applyRunningTaskIdsFrame(convs, {
    convId: 'c',
    runningTaskIds: ['tid-carrier#vu'],
    runningTaskIdsRev: [100, 'r0'],
    userId: 1,
  });
  const set = conv._authoritativeActiveTaskIds;
  let hasMarker = false;
  if (set) { for (const v of set) { if (String(v).endsWith('#vu')) hasMarker = true; } }
  check('marker_is_stripped_before_the_busy_set', hasMarker === false);
  check('stripped_id_is_what_reconnect_would_use',
        !!(set && set.has('tid-carrier')));
  // And the reconnect picker must NOT hand back a '#vu' string.
  const pick = window.pickAuthoritativeTaskIdForReconnect(conv);
  check('reconnect_pick_is_not_the_marked_string',
        pick !== 'tid-carrier#vu');
  /* ★ THE LOAD-BEARING ONE: the carrier must not be a reconnect target AT
   *   ALL — not the marked string, and NOT the stripped id either. A VU
   *   carrier's SSE never completes, so attaching to it reproduces the
   *   permanently-stuck "Waiting…" bubble the carrier filter exists to
   *   prevent. Asserting only "!== 'tid-carrier#vu'" passes while the
   *   stripped 'tid-carrier' leaks straight through — a real regression a
   *   marker-only assertion is structurally blind to. */
  check('carrier_is_never_a_reconnect_target', pick === null);
  check('busy_and_attachable_are_separate_sets',
        !!(conv._authoritativeAttachableTaskIds
           && conv._authoritativeAttachableTaskIds.size === 0
           && conv._authoritativeActiveTaskIds.size === 1));
}

/* ── 2b. A NORMAL worker IS still attachable (control) ───────────── */
{
  const convs = freshConvs();
  const conv = { id: 'c2' };
  convs.push(conv);
  window.applyRunningTaskIdsFrame(convs, {
    convId: 'c2', runningTaskIds: ['tid-real-worker'],
    runningTaskIdsRev: [100, 'r0'], userId: 1,
  });
  check('normal_worker_stays_attachable',
        window.pickAuthoritativeTaskIdForReconnect(conv) === 'tid-real-worker');
}

/* ── 2c. Mixed carrier + real worker → attach to the REAL one ────── */
{
  const convs = freshConvs();
  const conv = { id: 'c3' };
  convs.push(conv);
  window.applyRunningTaskIdsFrame(convs, {
    convId: 'c3', runningTaskIds: ['tid-carrier2#vu', 'tid-worker2'],
    runningTaskIdsRev: [100, 'r0'], userId: 1,
  });
  check('mixed_attaches_to_the_real_worker',
        window.pickAuthoritativeTaskIdForReconnect(conv) === 'tid-worker2');
  check('mixed_busy_set_counts_both',
        conv._authoritativeActiveTaskIds.size === 2);
}

/* ── 3. An empty list still reads IDLE (the signal is not pinned on) ── */
{
  const convs = freshConvs();
  const conv = { id: 'idle' };
  convs.push(conv);
  window.applyRunningTaskIdsFrame(convs, {
    convId: 'idle', runningTaskIds: [],
    runningTaskIdsRev: [100, 'r0'], userId: 1,
  });
  check('empty_list_reads_idle',
        window.computeConvBusy(conv, window.activeStreams) === false);
}

/* ── 4. A snapshot carrying a '#vu' entry lights the dot too ────────── */
{
  const convs = freshConvs();
  const conv = { id: 'snap' };
  convs.push(conv);
  window.applyConvStateSnapshot(convs, {
    userId: 1,
    convs: {
      snap: { runningTaskIds: ['tid-vu#vu'], runningTaskIdsRev: [10, 'r'] },
    },
  });
  check('snapshot_vu_entry_lights_the_dot',
        window.computeConvBusy(conv, window.activeStreams) === true);
  const set = conv._authoritativeActiveTaskIds;
  let hasMarker = false;
  if (set) { for (const v of set) { if (String(v).endsWith('#vu')) hasMarker = true; } }
  check('snapshot_marker_also_stripped', hasMarker === false);
}

/* ── 5. PARK → REPLAY keeps the two sets separate ───────────────── */
{
  const convs = freshConvs();
  // Frame arrives BEFORE the conv is known (cold boot / other device).
  window.applyRunningTaskIdsFrame(convs, {
    convId: 'parked', runningTaskIds: ['tid-parked-carrier#vu'],
    runningTaskIdsRev: [100, 'r0'], userId: 1,
  });
  const conv = { id: 'parked' };
  convs.push(conv);
  window.replayPendingBusyState(convs);
  check('parked_vu_frame_lights_the_dot_on_replay',
        window.computeConvBusy(conv, window.activeStreams) === true);
  check('parked_carrier_is_still_not_attachable',
        window.pickAuthoritativeTaskIdForReconnect(conv) === null);
}

report();
process.exit(0);
"""


def test_vu_busy_signal_reaches_the_busy_ui_state():
    """The wire frame for a VU-window conv must drive the client's busy UI
    state, not merely the backend projection — the layer the incident lived in."""
    run_harness(
        target_js=_REDUCER,
        body_js=_BODY,
        min_pass=13,
        label='vu-busy-end-to-end',
    )


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
