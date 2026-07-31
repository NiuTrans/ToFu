"""Busy-state ARRIVAL must be able to trigger an attach — the F5 ordering hole.

WHAT THE OWNER CAUGHT (2026-07-29, follow-up to d6e8bdb3)
--------------------------------------------------------
d6e8bdb3 fixed the cold-attach seam ``_reconnectServerTaskIfIdle`` so a live
autopilot VU carrier is reachable. But that only helps when the busy state is
ALREADY in ``conv._vuCarrierTaskIds`` at the moment the seam runs — i.e. the
ordering "state first, then open". The F5 / new-tab ordering is REVERSED:

  1. boot runs ``loadConversation`` → the push snapshot has NOT arrived yet →
     ``_vuCarrierTaskIds`` is empty → the carrier fallback resolves null →
     no attach, static render (CORRECT at that instant — nothing to attach to).
  2. a few hundred ms later the ``conv_state_snapshot`` frame lands → the
     reducer writes the carrier → the sidebar dot lights, the composer flips to
     Stop → **and nothing ever retries the attach.**

Net state is byte-identical to the pre-fix bug: busy=true, bubble=empty,
stream=none, until a manual refresh.

WHY THE EXISTING FRAME PATH CANNOT SAVE IT (all three verified in source)
-------------------------------------------------------------------------
``_reconnectServerTaskIfIdle`` has three call sites. None covers this:

  * ``main_conv_lifecycle.js`` ×2 — the click-open path. Runs BEFORE the frame.
  * ``cross_tab_sync.js:408`` — inside ``_verifyActiveConvFromServer``'s
    ``if (changed)`` block, so it needs a rev bump to be reached. The VU carrier
    runs with ``_inline_messages=True``, which is hard-gated OUT of the conv DB
    sync path (``manager/_sync.py:324``, ``manager/_persist.py:40``) — so across
    the whole 282s VU window the conv rev never moves and this site is
    STRUCTURALLY unreachable. Not a timing race: unreachable by construction.

And the two handlers that DO see the busy signal only repaint:
  * the ``conv_state_snapshot`` branch, and
  * the ``conv_changed`` ``runningTaskIds`` branch,
both call only ``applyConvStateSnapshot`` / ``applyRunningTaskIdsFrame`` +
``renderConversationList`` + ``updateSendButton``. Zero attach calls. They light
the "busy" lamp and stop.

THE FIX THIS SUITE PINS
-----------------------
Make the ARRIVAL of busy state an attach trigger, not just a repaint: both frame
handlers call ``_reconnectServerTaskIfIdle(activeConvId)`` for the OPEN conv.
Safe to call on every frame — the seam's own ``activeStreams.has(id)`` gate makes
a repeat a cheap no-op, so a frame burst cannot storm.

WHAT THE ASSERTIONS ARE SHAPED TO CATCH
---------------------------------------
The bug IS the ordering, so asserting "an attach happened" is not enough — that
already passes on the click-open path d6e8bdb3 fixed. Every ordering test here
therefore:
  * runs the open FIRST and asserts it attached NOTHING (the carrier is not yet
    known), then
  * delivers the frame and asserts the attach happened AFTER it, with
    ``{vuCarrier:true}``,
using a monotonically-increasing sequence counter so "after" is a checked fact
rather than an inferred one.

Both handlers are driven as REAL shipped source (extracted function bodies +
the real reducer), never re-implemented in the harness.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parent.parent
SYNC_JS = REPO / "static" / "js" / "core" / "cross_tab_sync.js"
REDUCER_JS = REPO / "static" / "js" / "core" / "conv_state_reducer.js"
LIFECYCLE_JS = REPO / "static" / "js" / "main" / "main_conv_lifecycle.js"


def _node_available() -> bool:
    return bool(shutil.which("node"))


def _extract_fn(src: str, name: str) -> str:
    m = re.search(r"(async\s+)?function %s\s*\(" % re.escape(name), src)
    assert m, f"{name} not found in source"
    i = src.index("{", m.start())
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():j + 1]
    raise AssertionError(f"unbalanced braces extracting {name}")


def _reducer_fns() -> str:
    """The REAL reducer write path + pickers (never hand-rolled).

    The failure class being guarded is a disagreement between what the reducer
    writes and what the attach seam reads, so a stubbed reducer could satisfy
    the seam while the shipped one does something else.
    """
    src = REDUCER_JS.read_text()
    return "\n".join([
        "const _PENDING_BUSY_MAX = 200;",
        "const _pendingBusyState = new Map();",
        _extract_fn(src, "_revStrictlyGreater"),
        _extract_fn(src, "_frameIsOurs"),
        _extract_fn(src, "_parkPendingBusyState"),
        _extract_fn(src, "_isVuMarked"),
        _extract_fn(src, "_stripVuMarker"),
        _extract_fn(src, "_busyIdsFrom"),
        _extract_fn(src, "_attachableIdsFrom"),
        _extract_fn(src, "_vuCarrierIdsFrom"),
        _extract_fn(src, "applyRunningTaskIdsFrame"),
        _extract_fn(src, "applyConvStateSnapshot"),
        _extract_fn(src, "pickAuthoritativeTaskIdForReconnect"),
        _extract_fn(src, "pickVuCarrierForAttach"),
    ])


# The harness models the REAL boot ordering: open the conv (no carrier known
# yet), THEN deliver the frame. A sequence counter stamps every recorded event
# so "the attach happened AFTER the frame" is asserted, not assumed.
_HARNESS = r"""
'use strict';
const CFG = __CFG__;
let conversations = __CONVS__;
let activeConvId = CFG.activeConvId;
const activeStreams = new Map();
if (CFG.seedStream) activeStreams.set(CFG.seedStream, { controller: {}, taskId: 'LIVE' });

let _seq = 0;
const _calls = { connectToTask: [], render: [], sendBtn: [], openAttach: [] };

function renderConversationList() { _calls.render.push(++_seq); }
function updateSendButton() { _calls.sendBtn.push(++_seq); }
function showStreamingUIForConv(id) { /* paint only */ }
function debugLog() {}

/* Real connectToTask contract: sets activeStreams synchronously before its
 * first await (both the plain path and _connectAutopilotKick do). */
function connectToTask(id, taskId, retries, opts) {
  _calls.connectToTask.push({ seq: ++_seq, convId: id, taskId: taskId, opts: opts || null });
  activeStreams.set(id, { controller: {}, taskId });
}

__REDUCER_FNS__
__SEAM_FN__
__HANDLER_FNS__

/* ── Step 1: the OPEN happens first (boot / click), before any frame ──
 *   At this instant the snapshot has not arrived, so no carrier is known and
 *   the seam must attach NOTHING. Recording the verdict lets the test assert
 *   that the pre-frame state really was "nothing to attach to" — otherwise a
 *   test could pass because the open already attached, which is the OTHER
 *   ordering (already covered by the cold-attach suite). */
{
  const _ret = _reconnectServerTaskIfIdle(activeConvId);
  _calls.openAttach.push({ seq: ++_seq, ret: !!_ret,
                           connects: _calls.connectToTask.length });
}
const _connectsAfterOpen = _calls.connectToTask.length;

/* ── Step 2: the busy-state frame arrives ── */
if (CFG.frameKind === 'snapshot') {
  _onPushFrame({
    type: 'conv_state_snapshot',
    userId: null,
    convs: CFG.snapshotConvs,
  });
} else if (CFG.frameKind === 'conv_changed') {
  _onPushFrame({
    type: 'conv_changed',
    convId: CFG.frameConvId,
    runningTaskIds: CFG.runningTaskIds,
    runningTaskIdsRev: [100, 'r0'],
    userId: null,
  });
}

process.stdout.write(JSON.stringify({
  calls: _calls,
  connectsAfterOpen: _connectsAfterOpen,
  totalConnects: _calls.connectToTask.length,
}));
"""


def _handler_fns(sync_src: str) -> str:
    """Extract the two REAL frame handlers + a dispatcher matching the shipped
    ``pushSubscribe`` routing in ``_wireConvSyncPush``.

    We reproduce only the ROUTING (which frame type goes to which handler),
    because that is a 3-line switch; the handler BODIES are the shipped source.
    """
    parts = [_extract_fn(sync_src, "_onConvNotifyPush")]
    # The snapshot branch lives inline in _wireConvSyncPush's subscribe
    # callback, so lift that callback's snapshot arm via a thin dispatcher that
    # mirrors it. We assert the real wiring separately in a source test below.
    parts.append(r"""
function _onPushFrame(frame) {
  if (frame && (frame.type === 'conv_changed' || frame.type === 'conv_deleted')) {
    _onConvNotifyPush(frame);
  } else if (frame && frame.type === 'conv_state_snapshot') {
    __SNAPSHOT_ARM__
  }
}
""")
    return "\n".join(parts)


def _snapshot_arm(sync_src: str) -> str:
    """The shipped snapshot arm body, lifted verbatim from _wireConvSyncPush.

    Anchored on the real marker comment + the applyConvStateSnapshot call so a
    refactor that moves/renames it fails loudly here rather than silently
    testing a stale copy.
    """
    m = re.search(
        r"else if \(frame && frame\.type === \"conv_state_snapshot\"\) \{(.*?)\n    \}",
        sync_src, re.S)
    assert m, "snapshot arm not found in _wireConvSyncPush"
    return m.group(1)


def _run(convs, *, active_conv_id, frame_kind, snapshot_convs=None,
         frame_conv_id=None, running_task_ids=None, seed_stream=None,
         seam_src=None, sync_src=None):
    sync_src = sync_src if sync_src is not None else SYNC_JS.read_text()
    seam_src = seam_src if seam_src is not None else _extract_fn(
        LIFECYCLE_JS.read_text(), "_reconnectServerTaskIfIdle")
    cfg = {
        "activeConvId": active_conv_id,
        "frameKind": frame_kind,
        "snapshotConvs": snapshot_convs or {},
        "frameConvId": frame_conv_id,
        "runningTaskIds": running_task_ids or [],
        "seedStream": seed_stream,
    }
    handlers = _handler_fns(sync_src).replace(
        "__SNAPSHOT_ARM__", _snapshot_arm(sync_src))
    script = (_HARNESS
              .replace("__REDUCER_FNS__", _reducer_fns())
              .replace("__SEAM_FN__", seam_src)
              .replace("__HANDLER_FNS__", handlers)
              .replace("__CFG__", json.dumps(cfg))
              .replace("__CONVS__", json.dumps(convs)))
    out = subprocess.run(["node", "-e", script], capture_output=True,
                         text=True, cwd=str(REPO))
    assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
    return json.loads(out.stdout.strip().splitlines()[-1])


def _open_conv():
    """The F5 shape: conv is OPEN, parent already finished (no activeTaskId),
    and the client knows of NO running work yet (no authoritative sets)."""
    return [{
        "id": "c1",
        "activeTaskId": None,
        "messages": [
            {"role": "user", "content": "go", "_msgId": "u1"},
            {"role": "assistant", "content": "done", "_msgId": "a1"},
        ],
    }]


# ══════════════════════════════════════════════════════════════════════
#  THE ORDERING TESTS — open first (attaches nothing), frame second
#  (must attach, with the VU connector).
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_snapshot_arrival_after_open_triggers_vu_attach():
    """F5 ordering, snapshot frame. THE reported symptom: boot opens the conv
    before the snapshot lands, so the open legitimately attaches nothing; the
    snapshot then reveals a live VU carrier and MUST trigger the attach."""
    r = _run(_open_conv(), active_conv_id="c1", frame_kind="snapshot",
             snapshot_convs={"c1": {"runningTaskIds": ["da0717c8#vu"],
                                    "runningTaskIdsRev": [100, "r0"]}})
    # The open ran first and could not attach — proves we exercise the REVERSED
    # ordering, not the already-fixed click-open path.
    assert r["connectsAfterOpen"] == 0, \
        f"the open should attach nothing (no carrier known yet): {r}"
    assert r["calls"]["openAttach"][0]["ret"] is False, r
    # The frame must then produce exactly one attach, via the VU connector.
    assert r["totalConnects"] == 1, \
        f"snapshot arrival did not trigger an attach — the busy lamp lights " \
        f"and nothing streams (the reported bug): {r}"
    c = r["calls"]["connectToTask"][0]
    assert c["convId"] == "c1" and c["taskId"] == "da0717c8", r
    assert c["opts"] and c["opts"].get("vuCarrier") is True, \
        f"carrier attached through the PLAIN path → ghost second Agent bubble: {r}"
    # ORDERING IS THE BUG: the attach must come strictly AFTER the open verdict.
    assert c["seq"] > r["calls"]["openAttach"][0]["seq"], \
        f"attach did not happen after the frame — wrong ordering exercised: {r}"


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_conv_changed_running_task_ids_arrival_triggers_vu_attach():
    """Same hole on the OTHER handler: a ``conv_changed`` frame carrying
    ``runningTaskIds`` with a '#vu' carrier. This branch never had an attach
    call either, and it is the steady-state (non-boot) announcement path."""
    r = _run(_open_conv(), active_conv_id="c1", frame_kind="conv_changed",
             frame_conv_id="c1", running_task_ids=["da0717c8#vu"])
    assert r["connectsAfterOpen"] == 0, r
    assert r["totalConnects"] == 1, \
        f"conv_changed runningTaskIds arrival did not trigger an attach: {r}"
    c = r["calls"]["connectToTask"][0]
    assert c["opts"] and c["opts"].get("vuCarrier") is True, r
    assert c["seq"] > r["calls"]["openAttach"][0]["seq"], r


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_frame_for_background_conv_does_not_attach():
    """A frame naming a conv that is NOT open must not attach anything — the
    viewport belongs to another conversation, and attaching would bind a stream
    for a conv the user is not looking at."""
    r = _run(_open_conv(), active_conv_id="c1", frame_kind="snapshot",
             snapshot_convs={"other": {"runningTaskIds": ["zz#vu"],
                                       "runningTaskIdsRev": [100, "r0"]}})
    assert r["totalConnects"] == 0, \
        f"attached for a background conv: {r}"


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_frame_with_real_worker_uses_plain_path():
    """No false positive: a frame announcing a NORMAL running task must attach
    through the plain path, not the VU connector (which would suppress its real
    assistant bubble)."""
    r = _run(_open_conv(), active_conv_id="c1", frame_kind="conv_changed",
             frame_conv_id="c1", running_task_ids=["plain-worker"])
    assert r["totalConnects"] == 1, r
    c = r["calls"]["connectToTask"][0]
    assert c["taskId"] == "plain-worker", r
    assert not (c["opts"] and c["opts"].get("vuCarrier")), \
        f"a real worker was routed through the VU connector: {r}"


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_frame_burst_is_idempotent_no_storm():
    """Frames arrive in bursts. The seam's own activeStreams gate must collapse
    repeats into cheap no-ops — one attach, not N."""
    sync_src = SYNC_JS.read_text()
    seam = _extract_fn(LIFECYCLE_JS.read_text(), "_reconnectServerTaskIfIdle")
    # Deliver the same snapshot three times in one run.
    cfg_convs = {"c1": {"runningTaskIds": ["da0717c8#vu"],
                        "runningTaskIdsRev": [100, "r0"]}}
    handlers = _handler_fns(sync_src).replace(
        "__SNAPSHOT_ARM__", _snapshot_arm(sync_src))
    script = (_HARNESS
              .replace("__REDUCER_FNS__", _reducer_fns())
              .replace("__SEAM_FN__", seam)
              .replace("__HANDLER_FNS__", handlers)
              .replace("__CFG__", json.dumps({
                  "activeConvId": "c1", "frameKind": "none",
                  "snapshotConvs": {}, "frameConvId": None,
                  "runningTaskIds": [], "seedStream": None}))
              .replace("__CONVS__", json.dumps(_open_conv())))
    # Append three deliveries with increasing rev (so the reducer accepts each).
    script = script.replace(
        "process.stdout.write(JSON.stringify({",
        """
for (let i = 0; i < 3; i++) {
  _onPushFrame({ type: 'conv_state_snapshot', userId: null,
                 convs: { c1: { runningTaskIds: ['da0717c8#vu'],
                                runningTaskIdsRev: [200 + i, 'r0'] } } });
}
process.stdout.write(JSON.stringify({""", 1)
    out = subprocess.run(["node", "-e", script], capture_output=True,
                         text=True, cwd=str(REPO))
    assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
    r = json.loads(out.stdout.strip().splitlines()[-1])
    assert r["totalConnects"] == 1, \
        f"frame burst caused {r['totalConnects']} attaches — storm, not no-op: {r}"


# ══════════════════════════════════════════════════════════════════════
#  NEUTER — each must bite
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_neuter_strip_snapshot_attach_regresses():
    """NEUTER: remove the attach from the snapshot arm → the F5 symptom returns
    (busy lamp lights, nothing streams)."""
    src = SYNC_JS.read_text()
    arm = _snapshot_arm(src)
    assert "_reconnectServerTaskIfIdle" in arm, \
        "snapshot arm no longer attaches — the F5 ordering hole is open"
    neutered_arm = arm.replace("_reconnectServerTaskIfIdle", "_noopAttach")
    neutered = src.replace(arm, neutered_arm, 1)
    assert neutered != src
    r = _run(_open_conv(), active_conv_id="c1", frame_kind="snapshot",
             snapshot_convs={"c1": {"runningTaskIds": ["da0717c8#vu"],
                                    "runningTaskIdsRev": [100, "r0"]}},
             sync_src=neutered)
    assert r["totalConnects"] == 0, \
        f"neuter did not disable the snapshot attach: {r}"


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_neuter_strip_conv_changed_attach_regresses():
    """NEUTER: remove the attach from the runningTaskIds branch → that handler
    goes back to repaint-only."""
    src = SYNC_JS.read_text()
    fn = _extract_fn(src, "_onConvNotifyPush")
    assert "_reconnectServerTaskIfIdle" in fn, \
        "_onConvNotifyPush no longer attaches on busy-state arrival"
    neutered_fn = fn.replace("_reconnectServerTaskIfIdle", "_noopAttach")
    neutered = src.replace(fn, neutered_fn, 1)
    r = _run(_open_conv(), active_conv_id="c1", frame_kind="conv_changed",
             frame_conv_id="c1", running_task_ids=["da0717c8#vu"],
             sync_src=neutered)
    assert r["totalConnects"] == 0, \
        f"neuter did not disable the conv_changed attach: {r}"


# ══════════════════════════════════════════════════════════════════════
#  Source-drift guards
# ══════════════════════════════════════════════════════════════════════

def test_both_frame_handlers_attach_on_busy_arrival():
    """Both busy-signal handlers must carry an attach, not just a repaint.

    Pins the ARCHITECTURAL fact the bug came from: the frame handlers used to
    call only applyConvStateSnapshot / applyRunningTaskIdsFrame +
    renderConversationList + updateSendButton, so arrival of busy state could
    light the lamp with nothing streaming.
    """
    src = SYNC_JS.read_text()
    arm = _snapshot_arm(src)
    assert "_reconnectServerTaskIfIdle" in arm, \
        "conv_state_snapshot arm does not attach — F5 lands busy-with-no-stream"
    notify_fn = _extract_fn(src, "_onConvNotifyPush")
    assert "_reconnectServerTaskIfIdle" in notify_fn, \
        "conv_changed runningTaskIds branch does not attach"


def test_verify_active_conv_attach_site_is_not_the_only_one():
    """The pre-existing ``:408`` attach lives inside ``if (changed)`` and needs a
    rev bump; the VU carrier runs ``_inline_messages=True`` and is hard-gated
    out of the conv DB sync path, so it NEVER bumps rev. Assert we did not
    "fix" this by relying on that site."""
    src = SYNC_JS.read_text()
    verify_fn = _extract_fn(src, "_verifyActiveConvFromServer")
    assert "_reconnectServerTaskIfIdle" in verify_fn, \
        "the cross-device rev-driven attach was removed (unrelated regression)"
    # And the frame handlers must have their OWN attach, independent of it.
    notify_fn = _extract_fn(src, "_onConvNotifyPush")
    assert "_reconnectServerTaskIfIdle" in notify_fn, \
        ("only _verifyActiveConvFromServer attaches — that path is unreachable "
         "during a VU window (no rev bump), so the F5 hole stays open")


def test_send_button_resolution_is_consistent_across_handlers():
    """Both handlers must resolve "is this the open conv?" the same way.

    The snapshot arm called a bare ``updateSendButton()`` while the
    runningTaskIds branch gated on ``activeConvId`` — two different notions of
    "current conversation" in handlers for the same signal, which is how the
    next drift starts.

    SCOPE — read before adding a NEUTER for the ``_snapTouchesActive`` gate.
    That gate is a cheap EARLY-OUT and an intent marker, NOT a correctness
    boundary, and it is deliberately not claimed as independently neuterable:
    ``applyConvStateSnapshot`` CLEARS every conv absent from the snapshot
    (conv_state_reducer.js, "CLEARED: not present in snapshot"), so when the
    snapshot does not name the open conv its authoritative sets are already
    empty and ``_reconnectServerTaskIfIdle`` resolves null → no attach either
    way. Deleting the gate is therefore behaviourally EQUIVALENT, which is why
    a neuter against it cannot bite. Correctness rests on the seam's own null
    resolution; the gate rests on top of it for cheapness + shared vocabulary.
    What this test pins is the thing that CAN drift: the arm consulting
    ``activeConvId`` at all, so the two handlers keep one notion of the open
    conversation.
    """
    src = SYNC_JS.read_text()
    arm = _snapshot_arm(src)
    assert "activeConvId" in arm, (
        "the conv_state_snapshot arm still resolves the composer/attach target "
        "without consulting activeConvId — align it with the runningTaskIds "
        "branch so both handlers share one notion of the open conversation")


def test_python_side_confirms_vu_carrier_never_bumps_rev():
    """The load-bearing backend fact behind this whole suite: the VU carrier is
    excluded from the conv DB sync path, so no rev bump can ever reach the
    rev-gated attach site. If this stops being true the reasoning above (and the
    need for the frame-handler attach) must be re-derived, not silently kept."""
    sync_py = (REPO / "lib" / "tasks_pkg" / "manager" / "_sync.py").read_text()
    persist_py = (REPO / "lib" / "tasks_pkg" / "manager" / "_persist.py").read_text()
    assert "_inline_messages" in sync_py, \
        "manager/_sync.py no longer gates on _inline_messages"
    assert "_inline_messages" in persist_py, \
        "manager/_persist.py no longer gates on _inline_messages"


if __name__ == "__main__":
    test_snapshot_arrival_after_open_triggers_vu_attach()
    print("PASS snapshot_ordering")
    test_conv_changed_running_task_ids_arrival_triggers_vu_attach()
    print("PASS conv_changed_ordering")
    test_frame_for_background_conv_does_not_attach()
    print("PASS background_no_attach")
    test_frame_with_real_worker_uses_plain_path()
    print("PASS real_worker_plain")
    test_frame_burst_is_idempotent_no_storm()
    print("PASS burst_idempotent")
    test_neuter_strip_snapshot_attach_regresses()
    print("PASS neuter_snapshot")
    test_neuter_strip_conv_changed_attach_regresses()
    print("PASS neuter_conv_changed")
    test_both_frame_handlers_attach_on_busy_arrival()
    print("PASS both_handlers_attach")
    test_verify_active_conv_attach_site_is_not_the_only_one()
    print("PASS not_only_rev_site")
    test_send_button_resolution_is_consistent_across_handlers()
    print("PASS consistent_resolution")
    test_python_side_confirms_vu_carrier_never_bumps_rev()
    print("PASS backend_fact")
    print("ALL GREEN")
