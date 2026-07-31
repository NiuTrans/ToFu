"""End-to-end guard: the follow-up funnel must reach the successor worker.

Companion to ``test_frontend_vu_followup_routing.py``. That file pins the pure
VERDICT; this one drives the REAL ``_checkForQueuedTask`` and asserts the
observable outcome the user actually sees — an Agent bubble appearing.

THE REPORTED SYMPTOM (the objective's third screenshot)
------------------------------------------------------
An autopilot-driven VU user message appears, the backend is generating the
reply, and no Agent bubble is ever created. Measured cause: with the VU
carrier's id pinned on ``conv.activeTaskId``, the funnel's opening guard
returned before probing ``/api/v1/chat/active`` — so the successor worker,
which that endpoint reports perfectly well, was never discovered.

WHAT THIS FILE ASSERTS, AND THE COMPLEMENTS THAT KEEP THE FIX HONEST
--------------------------------------------------------------------
  * terminal carrier pin  → probe runs, successor worker attached (the fix);
  * LIVE carrier pin      → routed to the VU connector with {vuCarrier:true},
                            NOT the plain path — binding a real assistant
                            placeholder to a carrier's stream is what renders
                            the VU's frames as a ghost second "Agent" bubble,
                            which the detached-dummy connector exists to
                            prevent. Asserting merely "did not probe" would
                            let that regression through;
  * no successor exists   → nothing attached and NO placeholder bubble, so we
                            never invent an Agent bubble that spins forever;
  * plain worker pin      → still skipped (no double attach — the guard's
                            original and still-valid job);
  * live local stream     → still skipped.
"""

import os

import pytest

from tests._jsdom import run_harness, JS_DIR

pytestmark = pytest.mark.unit

_REDUCER = os.path.join(JS_DIR, 'core', 'conv_state_reducer.js')
_PIPELINE = os.path.join(JS_DIR, 'main', 'main_send_pipeline.js')

_BODY = r'''
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="chatInner"></div></body>',
  targets: [process.argv[2], process.argv[4]],
  globals: { activeStreams: new Map(), conversations: [], activeConvId: null, Api: null },
});

const CONV = 'c1', CARRIER = 'carrier-1', WORKER = 'worker-1';

/* Stubs reinstated AFTER eval — the target's own top-level declarations land
 * in global scope and would otherwise shadow them. */
let probes = 0, attached = null, attachOpts = null;
global._refreshServerQueue = () => {};
global._dispatchableQueueCount = () => 0;
global._ensureMsgId = (m) => { m._msgId = 'm'; return m; };
global.loadConversationMessages = async () => {};
global.renderConversationList = () => {};
global.debugLog = () => {};
global.connectToTask = (c, t, r, o) => { attached = t; attachOpts = o || null; };
global.window.ConvView = { replaceAll: () => {} };

function setActive(list) {
  global.Api = { chat: { active: async () => { probes++; return list; } } };
}

/* Seed authoritative state through the REAL reducer from a REAL wire frame. */
function seed(runningTaskIds, pin, rev) {
  const conv = { id: CONV, messages: [], activeTaskId: pin };
  global.conversations = [conv];
  global.activeStreams = new Map();
  applyRunningTaskIdsFrame(global.conversations, {
    convId: CONV, runningTaskIds: runningTaskIds,
    runningTaskIdsRev: rev || [100, 'r1'],
  });
  return conv;
}
function reset() { probes = 0; attached = null; attachOpts = null; }

(async () => {
  const RUNNING_WORKER = [{ id: WORKER, convId: CONV, status: 'running', aborted: false }];

  /* ── THE FIX: carrier went terminal, successor worker is running ────── */
  reset();
  setActive(RUNNING_WORKER);
  seed([WORKER], CARRIER, [200, 'r1']);
  await _checkForQueuedTask(CONV);
  check('terminal carrier pin → endpoint probed', probes === 1);
  check('terminal carrier pin → successor worker attached', attached === WORKER);
  check('successor uses the PLAIN connector (a worker, not a carrier)',
        !attachOpts || attachOpts.vuCarrier !== true);

  /* ── Complement 1: LIVE carrier routes to the VU connector ──────────── */
  reset();
  setActive([]);                     // no successor exists yet — VU still running
  seed([CARRIER + '#vu'], CARRIER, [300, 'r1']);
  await _checkForQueuedTask(CONV);
  check('live carrier → NOT probed (successor does not exist yet)', probes === 0);
  check('live carrier → attached via the VU connector', attached === CARRIER);
  check('live carrier → {vuCarrier:true}, never the plain path',
        !!(attachOpts && attachOpts.vuCarrier === true));

  /* ── Complement 2: no successor → attach nothing, invent no bubble ─── */
  reset();
  setActive([]);
  seed([], CARRIER, [400, 'r1']);    // carrier terminal, nothing succeeded it
  await _checkForQueuedTask(CONV);
  check('no successor → probed', probes === 1);
  check('no successor → nothing attached', attached === null);
  check('no successor → no phantom Agent bubble in the DOM',
        document.getElementById('streaming-msg') === null);

  /* ── Complement 3: plain worker pin still skips (no double attach) ──── */
  reset();
  setActive(RUNNING_WORKER);
  seed([WORKER], WORKER, [500, 'r1']);
  await _checkForQueuedTask(CONV);
  check('plain worker pin → still skipped, no double attach',
        probes === 0 && attached === null);

  /* ── Complement 4: live local stream still skips ────────────────────── */
  reset();
  setActive(RUNNING_WORKER);
  const conv = seed([CARRIER + '#vu'], CARRIER, [600, 'r1']);
  global.activeStreams = new Map([[CONV, { taskId: CARRIER }]]);
  await _checkForQueuedTask(CONV);
  check('live local stream → still skipped',
        probes === 0 && attached === null);

  report();
})();
'''


def test_followup_funnel_reaches_the_successor_worker():
    """A VU carrier pin must never strand the successor worker."""
    run_harness(
        target_js=_REDUCER,
        body_js=_BODY,
        extra_targets=[_PIPELINE],
        min_pass=11,
        label='followup-funnel',
    )
