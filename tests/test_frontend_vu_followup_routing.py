"""Guard: the queued/follow-up check must ROUTE on a pin, not merely test it.

THE DEFECT (pt_f7a292dc13de47f0, measured on shipped code)
---------------------------------------------------------
``_checkForQueuedTask`` opens with

    if (_conv && (_conv.activeTaskId || activeStreams.has(convId))) return;

i.e. "this conv already has an active task, someone else is driving". That
reading was true while a pin was short-lived. It stopped being true when
``d6e8bdb3`` made cold attach pin ``conv.activeTaskId = <carrierId>`` and
batch 6 (``pt_d97f9098776c48e9``) deliberately stopped the stale-pin sweep
from clearing a live carrier's pin. The pin became DURABLE, so the guard is
permanently satisfied and the function returns BEFORE it ever probes for the
successor worker the backend already spawned.

Measured A/B against the shipped function (node, clean signal):

    A  activeTaskId=<carrierId>, no local stream, worker running server-side
       →  probes=0   attached=null      ← the third screenshot
    B  activeTaskId=null, identical backend state
       →  probes=1   attached=<worker>

The endpoint was never the blocker: ``is_carrier_task`` (_registry.py:106)
returns ``_inline_messages or _vu_subtask`` only, so the successor worker is
plainly visible at ``/api/v1/chat/active``. The guard returns one line earlier.

WHY "CARRIER ⇒ TREAT AS IDLE" IS THE WRONG FIX (owner, this round)
------------------------------------------------------------------
``_registry.py:76`` records that the historical reason for hiding carriers is
NO LONGER TRUE: ``_live_tick`` emits ``build_carrier_terminal_done`` for a
``_vu_subtask`` and closes the stream. The surviving constraint is narrower —
a carrier is *not a PLAIN reconnect target*, but it IS routable through the VU
connector (``pickVuCarrierForAttach`` → ``connectToTask(..., {vuCarrier:true})``
→ ``_connectAutopilotKick``, detached dummy assistant).

So the verdict is a ROUTE, not a boolean, and it has four cases. Simply
letting a carrier pin fall through to the probe would assume the carrier is
finished and regress case 1: probing for a successor while the VU is still
generating finds nothing and gives up, re-creating dead air.

THE FOUR CASES THIS FILE PINS
-----------------------------
  1. pin → carrier STILL LIVE       ⇒ 'route-vu'  — attach via the VU
                                       connector; do NOT probe for a successor.
  2. pin → carrier gone TERMINAL    ⇒ 'probe'     — the successor worker is
                                       what the user is waiting for.
  3. pin → plain worker / live local stream ⇒ 'skip' — today's behaviour; this
                                       is the guard's REAL job (no double attach).
  4. pin → task the projection never heard of ⇒ 'probe' — a stale pin across a
                                       server restart must not wedge silently.

The verdict is derived from the REDUCER'S OWN output (``_vuCarrierTaskIds`` /
``_authoritativeActiveTaskIds``, markers already stripped by
``_vuCarrierIdsFrom``), never by re-parsing the ``#vu`` marker at this call
site. A second marker parser drifting from the reducer's is the split that
produced this whole defect family.
"""

import os

import pytest

from tests._jsdom import run_harness, JS_DIR

pytestmark = pytest.mark.unit

_REDUCER = os.path.join(JS_DIR, 'core', 'conv_state_reducer.js')
_PIPELINE = os.path.join(JS_DIR, 'main', 'main_send_pipeline.js')

# The verdict function under test must be a pure reducer over (conv, streams).
_BODY = r'''
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="chatInner"></div></body>',
  targets: [process.argv[2], process.argv[4]],
  globals: { activeStreams: new Map(), conversations: [], activeConvId: null },
});

const CONV = 'c1', CARRIER = 'carrier-1', WORKER = 'worker-1';

/* Seed the authoritative sets through the REAL reducer, from a REAL wire
 * frame — never by hand-building the Sets. A hand-built set could disagree
 * with what _vuCarrierIdsFrom actually produces and would happily green-light
 * a regression in the marker handling. */
function convWith(runningTaskIds, pin, rev) {
  const conv = { id: CONV, messages: [], activeTaskId: pin };
  global.conversations = [conv];
  applyRunningTaskIdsFrame(global.conversations, {
    convId: CONV,
    runningTaskIds: runningTaskIds,
    runningTaskIdsRev: rev || [100, 'r1'],
  });
  return conv;
}

if (typeof computeFollowupRoute !== 'function') {
  console.log('FAIL computeFollowupRoute is not defined (the routing verdict does not exist)');
  report();
} else {

  /* ── Case 1: pin names a carrier that is STILL LIVE ─────────────────── */
  {
    const conv = convWith([CARRIER + '#vu'], CARRIER);
    const v = computeFollowupRoute(conv, new Map());
    check('case1 live carrier → route-vu', v && v.action === 'route-vu');
    check('case1 carries the carrier id for the VU connector',
          v && v.taskId === CARRIER);
    /* The whole point of the VU connector: never the plain path. */
    check('case1 demands the vuCarrier connector',
          v && v.vuCarrier === true);
  }

  /* ── Case 2: pin names a carrier that has gone TERMINAL ─────────────── */
  {
    /* Projection no longer lists the carrier (its terminal done landed and
     * the registry dropped it) but the pin is still on the conv, because
     * batch 6 deliberately stops the sweep from clearing a carrier pin. */
    const conv = convWith([WORKER], CARRIER, [200, 'r1']);
    const v = computeFollowupRoute(conv, new Map());
    check('case2 terminal carrier → probe for the successor',
          v && v.action === 'probe');
  }

  /* ── Case 3a: pin names a PLAIN worker → skip (no double attach) ────── */
  {
    const conv = convWith([WORKER], WORKER, [300, 'r1']);
    const v = computeFollowupRoute(conv, new Map());
    check('case3a plain worker pin → skip', v && v.action === 'skip');
  }

  /* ── Case 3b: a live LOCAL stream → skip regardless of the pin ──────── */
  {
    const conv = convWith([CARRIER + '#vu'], CARRIER, [400, 'r1']);
    const streams = new Map([[CONV, { taskId: CARRIER }]]);
    const v = computeFollowupRoute(conv, streams);
    check('case3b live local stream → skip even for a carrier pin',
          v && v.action === 'skip');
  }

  /* ── Case 4: pin the projection has never heard of (server restart) ─── */
  {
    const conv = convWith([], 'ghost-task-from-a-dead-server', [500, 'r1']);
    const v = computeFollowupRoute(conv, new Map());
    check('case4 unknown pin → probe, never a silent wedge',
          v && v.action === 'probe');
  }

  /* ── Fail-safe: no projection at all (endpoint down / never fetched) ── */
  {
    const conv = { id: CONV, messages: [], activeTaskId: CARRIER };
    const v = computeFollowupRoute(conv, new Map());
    check('no projection → skip (preserve the legacy guard, never guess)',
          v && v.action === 'skip');
  }

  /* ── Complement: idle conv with no pin still probes (batch-B control) ─ */
  {
    const conv = convWith([], null, [600, 'r1']);
    const v = computeFollowupRoute(conv, new Map());
    check('no pin, no stream → probe (the control that already worked)',
          v && v.action === 'probe');
  }

  report();
}
'''


def test_followup_route_is_a_four_case_verdict():
    """The verdict routes a pin; it does not merely test the pin's presence."""
    run_harness(
        target_js=_REDUCER,
        body_js=_BODY,
        extra_targets=[_PIPELINE],
        min_pass=9,
        label='followup-route',
    )
