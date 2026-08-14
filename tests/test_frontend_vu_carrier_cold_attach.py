"""Cold-attach to a live autopilot VU carrier — the "generating forever, and the
frontend has no idea what" bug (owner-reported 2026-07-29, conv ms5j3qi7wd1g7u).

WHAT THE USER SAW
-----------------
Three visits to one conversation, three inconsistent states, none of them
showing any generation:
  1. a conversation that looked completely finished, but with the composer in
     Stop (generating) state;
  2. a queued message sitting in the input area with nothing generating;
  3. an autopilot-authored user turn that appeared with NO agent bubble
     starting to generate after it.

MEASURED GROUND TRUTH (app.log, conv ms5j3qi7wd1g7u)
----------------------------------------------------
  15:00:50  VU carrier da0717c8 created
  15:00:50→15:06:03  it ran 282.6s / 69 events / 4 LLM rounds WITH TOOLS
  15:02:21  the user filed the report — mid-window
  15:06:03  "SSE stream da0717c8 emitting carrier done (VU sub-task terminal)"

So the backend was doing real, billed work for ~4.7 minutes and the UI showed a
finished conversation stuck in Stop.

THE ROOT CAUSE — TWO ATTACH PATHS, OPPOSITE VERDICTS ON ONE CARRIER
-------------------------------------------------------------------
``snapshot_running_by_conv`` surfaces a live VU carrier as ``<tid>#vu``. The
reducer deliberately derives TWO sets from that (conv_state_reducer.js):

  _authoritativeActiveTaskIds      → BUSY-ness   (carrier INCLUDED)
  _authoritativeAttachableTaskIds  → ATTACH tgts (carrier EXCLUDED)

That split is CORRECT and this suite does not touch it: a carrier handed to the
plain ``connectToTask`` path would bind a real assistant placeholder to a stream
that emits only the ``autopilot_vu_*`` contract → the ghost second-"Agent"
bubble the split exists to prevent.

The bug is that the split left the cold path with NOTHING:

  HOT hop  (client already attached): the parent's terminal frame carries
      ``latestLiveTaskId`` + ``latestLiveTaskIsVu`` → _runTerminalContinuation
      → connectToTask(..., {vuCarrier:true}) → _connectAutopilotKick.  WORKS.
  COLD attach (F5 / click-away-and-back / another tab): the ONLY resolver is
      ``pickAuthoritativeTaskIdForReconnect``, which reads the ATTACHABLE set
      → null → ``_reconnectServerTaskIfIdle`` returns false → static render.
      ``/api/chat/active`` hides carriers too. NOTHING can reach the carrier.

Net state: **busy = true, attachable = none.** Stop button + "generating" + no
bubble + no stream. Miss the one live hop frame and the entire autopilot chain
(VU → follow-up → next VU) stays invisible until a manual refresh — which is
exactly symptoms 1-3, one cause.

A NOTE ON THE STALE PREMISE
---------------------------
``is_carrier_task``'s docstring justifies hiding the VU with "NEVER streams a
``done`` event of its own" / "reconnecting an SSE that never completes". For the
VU carrier that is no longer true — ``lib/chat_dispatch.py`` `_live_tick` emits
``build_carrier_terminal_done`` for ``_vu_subtask`` and closes the stream (seen
live above). ``test_carrier_docstring_matches_shipped_behaviour`` pins the
corrected wording so the next reader does not re-derive the exclusion from a
falsified fact.

WHAT THIS SUITE PINS
--------------------
  1. reducer: a conv whose only worker is a ``#vu`` carrier exposes that carrier
     via ``pickVuCarrierForAttach`` (and still NOT via the attachable set).
  2. reducer: a conv with a real worker exposes NO vu carrier (no false positive
     that would route a normal task through the VU connector).
  3. lifecycle: cold attach with only a carrier live → connectToTask IS called,
     WITH ``{vuCarrier:true}`` (the detached-dummy connector).
  4. lifecycle: a real worker still routes through the PLAIN path (no opts).
  5. lifecycle: idempotent — a live local stream still short-circuits.
  6. the busy/attachable split is NOT collapsed (the carrier must never enter
     the attachable set).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._jsdom import JS_DIR, run_harness

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parent.parent
LIFECYCLE_JS = REPO / "static" / "js" / "main" / "main_conv_lifecycle.js"
REDUCER_JS = REPO / "static" / "js" / "core" / "conv_state_reducer.js"
REGISTRY_PY = REPO / "lib" / "tasks_pkg" / "manager" / "_registry.py"


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


# ══════════════════════════════════════════════════════════════════════
#  1-2 + 6. Reducer — the VU carrier must be reachable, but only as a
#           carrier, and never as a plain attach target.
# ══════════════════════════════════════════════════════════════════════

_REDUCER_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body></body>',
  targets: [process.argv[2]],
  globals: { activeStreams: new Map() },
});

function freshConvs() {
  if (typeof window.resetPendingBusyStateForTests === 'function') {
    window.resetPendingBusyStateForTests();
  }
  window._currentUserId = null;
  return [];
}

/* ── 1. Only worker is a VU carrier → reachable via pickVuCarrierForAttach ── */
{
  const convs = freshConvs();
  const conv = { id: 'ms5j3qi7wd1g7u' };
  convs.push(conv);
  window.applyRunningTaskIdsFrame(convs, {
    convId: conv.id,
    runningTaskIds: ['da0717c8-63f1-413c-9839-d08c74051991#vu'],
    runningTaskIdsRev: [100, 'r0'],
    userId: 1,
  });
  /* Still busy (unchanged contract). */
  check('carrier_still_lights_busy',
        window.computeConvBusy(conv, window.activeStreams) === true);
  /* Still NOT a plain attach target (the split is intact). */
  check('carrier_not_a_plain_attach_target',
        window.pickAuthoritativeTaskIdForReconnect(conv) === null);
  /* NEW: reachable as a CARRIER, with the marker stripped. */
  check('carrier_reachable_via_vu_picker',
        window.pickVuCarrierForAttach(conv)
        === 'da0717c8-63f1-413c-9839-d08c74051991');
  /* The raw marker must never leak into any consumer-facing id. */
  check('vu_marker_stripped_from_carrier_id',
        String(window.pickVuCarrierForAttach(conv)).indexOf('#vu') === -1);
}

/* ── 2. A real worker → NO carrier reported (no false positive) ─────────── */
{
  const convs = freshConvs();
  const conv = { id: 'c-real' };
  convs.push(conv);
  window.applyRunningTaskIdsFrame(convs, {
    convId: 'c-real',
    runningTaskIds: ['plain-worker-task'],
    runningTaskIdsRev: [100, 'r0'],
    userId: 1,
  });
  check('real_worker_is_attachable',
        window.pickAuthoritativeTaskIdForReconnect(conv) === 'plain-worker-task');
  check('real_worker_is_not_reported_as_vu_carrier',
        window.pickVuCarrierForAttach(conv) === null);
}

/* ── 2b. Mixed: a real worker AND a carrier → the real worker wins the plain
        path, and it must NOT be misreported as a carrier. ───────────────── */
{
  const convs = freshConvs();
  const conv = { id: 'c-mixed' };
  convs.push(conv);
  window.applyRunningTaskIdsFrame(convs, {
    convId: 'c-mixed',
    runningTaskIds: ['worker-1', 'carrier-2#vu'],
    runningTaskIdsRev: [100, 'r0'],
    userId: 1,
  });
  check('mixed_prefers_real_worker_for_plain_attach',
        window.pickAuthoritativeTaskIdForReconnect(conv) === 'worker-1');
  check('mixed_does_not_offer_worker_as_carrier',
        window.pickVuCarrierForAttach(conv) !== 'worker-1');
}

/* ── 3. Idle conv → neither picker returns anything ────────────────────── */
{
  const convs = freshConvs();
  const conv = { id: 'c-idle' };
  convs.push(conv);
  window.applyRunningTaskIdsFrame(convs, {
    convId: 'c-idle',
    runningTaskIds: [],
    runningTaskIdsRev: [100, 'r0'],
    userId: 1,
  });
  check('idle_has_no_plain_target',
        window.pickAuthoritativeTaskIdForReconnect(conv) === null);
  check('idle_has_no_vu_carrier',
        window.pickVuCarrierForAttach(conv) === null);
}

report();
"""


def test_reducer_exposes_vu_carrier_without_collapsing_the_split():
    """The reducer must expose a live VU carrier for cold attach WITHOUT ever
    putting it into the attachable set (which would resurrect the stuck-bubble
    bug the busy/attachable split exists to prevent)."""
    run_harness(
        target_js=os.path.join(JS_DIR, 'core', 'conv_state_reducer.js'),
        body_js=_REDUCER_BODY,
        min_pass=10,
        label='vu-carrier-cold-attach reducer',
    )


# ══════════════════════════════════════════════════════════════════════
#  3-5. Lifecycle — the cold attach seam actually routes to the VU
#       connector.
# ══════════════════════════════════════════════════════════════════════

_LIFECYCLE_HARNESS = r"""
'use strict';
const CFG = __CFG__;
let conversations = __CONVS__;
const activeStreams = new Map();
if (CFG.seedStream) activeStreams.set(CFG.seedStream, { controller: {}, taskId: 'LIVE' });

const _calls = { connectToTask: [], showStreaming: [] };

function showStreamingUIForConv(id) { _calls.showStreaming.push(id); }

/* Models the REAL connectToTask contract: it sets the activeStreams entry
 * SYNCHRONOUSLY before its first await (both the plain path and
 * _connectAutopilotKick do), which is what makes the caller's
 * `activeStreams.has(id)` repaint gate meaningful. */
function connectToTask(id, taskId, retries, opts) {
  _calls.connectToTask.push([id, taskId, opts || null]);
  activeStreams.set(id, { controller: {}, taskId });
}

/* The reducer helpers the seam delegates to — REAL implementations, lifted
 * from the shipped reducer so this harness cannot drift from it. */
__REDUCER_FNS__

const console = { info(){}, warn(){}, error(){}, log(){}, debug(){} };

/* ★ Seed each conv's authoritative state by driving the REAL reducer with the
 *   REAL wire shape the backend emits (`runningTaskIds` incl. '#vu' markers),
 *   instead of hand-assembling _vuCarrierTaskIds / _authoritative*.
 *
 *   Hand-built fixtures were WRONG here in a way that mattered: they made the
 *   sets plain arrays, while the reducer writes Sets (the pickers read
 *   `.size`). A fixture that fakes the very state under test can go green on a
 *   broken reducer — and can go red on a correct one, which is what happened.
 *   Driving the shipped reducer means the wire shape, the reducer and the
 *   attach seam are all exercised as one path. */
for (const c of conversations) {
  if (!c || !c.__wire) continue;
  applyRunningTaskIdsFrame(conversations, {
    convId: c.id,
    runningTaskIds: c.__wire,
    runningTaskIdsRev: [100, 'r0'],
    userId: null,
  });
}

__FN__

const _ret = _reconnectServerTaskIfIdle(__OPEN_ID__);
process.stdout.write(JSON.stringify({ ret: _ret, calls: _calls }));
"""


def _reducer_fns() -> str:
    """Lift the REAL reducer write path + pickers out of the shipped module.

    Deliberately NOT hand-written fakes. The failure being guarded is a
    disagreement between what the reducer WRITES and what the attach seam
    READS, so a stub could satisfy the seam while the shipped reducer does
    something else — a guard that goes green on a broken product (JOURNAL
    2026-07-29: "守卫用手搓夹具绕过被改的生产路径", which I then reproduced
    here: the first version faked the sets as arrays while the reducer writes
    Sets, so the seam's `.size` read saw undefined).

    Lifting ``applyRunningTaskIdsFrame`` too means the harness feeds the REAL
    wire shape through the REAL derivation into the REAL pickers.
    """
    src = REDUCER_JS.read_text()
    return "\n".join([
        _extract_fn(src, "_revStrictlyGreater"),
        _extract_fn(src, "_frameIsOurs"),
        _extract_fn(src, "_parkPendingBusyState"),
        _extract_fn(src, "_isVuMarked"),
        _extract_fn(src, "_stripVuMarker"),
        _extract_fn(src, "_busyIdsFrom"),
        _extract_fn(src, "_attachableIdsFrom"),
        _extract_fn(src, "_vuCarrierIdsFrom"),
        _extract_fn(src, "_withoutTerminalTaskIds"),
        _extract_fn(src, "applyRunningTaskIdsFrame"),
        _extract_fn(src, "pickAuthoritativeTaskIdForReconnect"),
        _extract_fn(src, "pickVuCarrierForAttach"),
        # _parkPendingBusyState's module-level deps.
        "const _PENDING_BUSY_MAX = 200;",
        "const _pendingBusyState = new Map();",
    ])


def _run_lifecycle(fn_src, convs, open_id, *, seed_stream=None):
    cfg = {"seedStream": seed_stream}
    script = (_LIFECYCLE_HARNESS
              .replace("__REDUCER_FNS__", _reducer_fns())
              .replace("__FN__", fn_src)
              .replace("__CFG__", json.dumps(cfg))
              .replace("__CONVS__", json.dumps(convs))
              .replace("__OPEN_ID__", json.dumps(open_id)))
    out = subprocess.run(["node", "-e", script], capture_output=True,
                         text=True, cwd=str(REPO))
    assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
    return json.loads(out.stdout.strip().splitlines()[-1])


def _seam():
    return _extract_fn(LIFECYCLE_JS.read_text(), "_reconnectServerTaskIfIdle")


def _carrier_only_conv():
    """The production shape: parent finished (activeTaskId cleared), the VU
    carrier is the only live worker, surfaced by the backend as '<tid>#vu'.

    ``__wire`` is fed through the REAL reducer by the harness — the conv's
    authoritative sets are DERIVED, never hand-assembled."""
    return [{
        "id": "c1",
        "activeTaskId": None,
        "__wire": ["da0717c8#vu"],
        "messages": [
            {"role": "user", "content": "go", "_msgId": "u1"},
            {"role": "assistant", "content": "done", "_msgId": "a1"},
        ],
    }]


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_cold_attach_routes_carrier_to_vu_connector():
    """THE BUG. Only a VU carrier is live → the cold seam must attach, and must
    do it through the {vuCarrier:true} connector (detached dummy, no ghost
    Agent placeholder). Before the fix this returned false and attached
    nothing — 282s of invisible generation."""
    r = _run_lifecycle(_seam(), _carrier_only_conv(), "c1")
    assert r["ret"] is True, f"cold attach refused a live VU carrier: {r}"
    assert len(r["calls"]["connectToTask"]) == 1, r
    cid, tid, opts = r["calls"]["connectToTask"][0]
    assert cid == "c1" and tid == "da0717c8", r
    assert opts and opts.get("vuCarrier") is True, (
        f"carrier attached through the PLAIN path — this renders the VU's "
        f"frames as a second 'Agent' bubble: {r}")


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_real_worker_still_uses_plain_path():
    """No false positive: a normal running task must NOT be routed through the
    VU connector (that would suppress its real assistant bubble)."""
    convs = [{
        "id": "c1",
        "activeTaskId": "T",
        "__wire": ["T"],
        "messages": [{"role": "user", "content": "hi", "_msgId": "u1"}],
    }]
    r = _run_lifecycle(_seam(), convs, "c1")
    assert r["ret"] is True, r
    cid, tid, opts = r["calls"]["connectToTask"][0]
    assert tid == "T", r
    assert not (opts and opts.get("vuCarrier")), \
        f"a real worker was routed through the VU connector: {r}"


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_carrier_attach_is_idempotent_with_live_stream():
    """A stream already live in THIS tab still short-circuits — the carrier
    branch must not bypass the idempotency guard and double-connect."""
    r = _run_lifecycle(_seam(), _carrier_only_conv(), "c1", seed_stream="c1")
    assert r["ret"] is False, r
    assert r["calls"]["connectToTask"] == [], \
        f"double-connected over a live stream: {r}"


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_idle_conv_still_static_renders():
    """Nothing live → no attach (the static-render path is preserved)."""
    convs = [{
        "id": "c1", "activeTaskId": None,
        "__wire": [],
        "messages": [{"role": "assistant", "content": "done", "_msgId": "a1"}],
    }]
    r = _run_lifecycle(_seam(), convs, "c1")
    assert r["ret"] is False, r
    assert r["calls"]["connectToTask"] == [], r


# ══════════════════════════════════════════════════════════════════════
#  NEUTER + source-drift guards
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_neuter_strip_carrier_branch_regresses():
    """NEUTER: remove the carrier branch from the seam → the reported bug
    returns (live carrier, nothing attached)."""
    src = _seam()
    m = re.search(r"const _vuCarrierTid = [^;]+;", src)
    assert m, "carrier-resolution line not found in _reconnectServerTaskIfIdle"
    neutered = src.replace(m.group(0), "const _vuCarrierTid = null;", 1)
    assert neutered != src
    r = _run_lifecycle(neutered, _carrier_only_conv(), "c1")
    assert r["calls"]["connectToTask"] == [], \
        "neutered seam should attach nothing"
    assert r["ret"] is False, r


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_neuter_drop_vu_flag_regresses():
    """NEUTER: attach the carrier but WITHOUT the vuCarrier flag → the ghost
    second-'Agent'-bubble shape. Pins that the flag is load-bearing, not
    decoration."""
    src = _seam()
    assert "{ vuCarrier: true }" in src, \
        "carrier attach no longer passes the vuCarrier flag"
    neutered = src.replace("{ vuCarrier: true }", "{}", 1)
    r = _run_lifecycle(neutered, _carrier_only_conv(), "c1")
    assert r["calls"]["connectToTask"], r
    _, _, opts = r["calls"]["connectToTask"][0]
    assert not (opts and opts.get("vuCarrier")), \
        "neuter did not actually drop the flag"


def test_reducer_never_puts_carrier_in_attachable_set():
    """The busy/attachable split must not be collapsed 'to simplify'. Putting a
    carrier into the attachable set is the WRONG fix for this bug — it would
    hand the carrier to the plain connectToTask path."""
    src = REDUCER_JS.read_text()
    fn = _extract_fn(src, "_attachableIdsFrom")
    assert "_isVuMarked(t)" in fn and "continue" in fn, \
        ("_attachableIdsFrom no longer excludes '#vu' carriers — a carrier in "
         "the attachable set becomes a plain reconnect target and births the "
         "permanently-stuck 'Waiting…' bubble")
    assert "function pickVuCarrierForAttach" in src, \
        "pickVuCarrierForAttach removed — cold attach loses its only carrier route"


def test_seam_wires_carrier_branch_after_plain_target():
    """Source-drift: the plain target must still be preferred; the carrier
    branch is the FALLBACK when no attachable worker exists."""
    src = LIFECYCLE_JS.read_text()
    fn = _extract_fn(src, "_reconnectServerTaskIfIdle")
    assert "pickAuthoritativeTaskIdForReconnect" in fn, \
        "plain attach resolution removed from the seam"
    assert "pickVuCarrierForAttach" in fn, \
        "carrier fallback removed from the seam"
    plain_i = fn.index("pickAuthoritativeTaskIdForReconnect")
    carrier_i = fn.index("pickVuCarrierForAttach")
    assert plain_i < carrier_i, \
        ("the carrier fallback must come AFTER the plain target — otherwise a "
         "conv with a real worker could be routed through the VU connector")
    assert "activeStreams.has(id)) return false;" in fn, \
        "idempotency guard removed from the seam"


def test_carrier_docstring_matches_shipped_behaviour():
    """``is_carrier_task``'s docstring justified hiding the VU with 'NEVER
    streams a done event of its own'. lib/chat_dispatch.py emits
    build_carrier_terminal_done for _vu_subtask and closes the stream, so that
    sentence is FALSE for the VU carrier and was the stated reason the cold
    path had no route to it. Pin the corrected wording so the exclusion is not
    re-derived from a falsified fact."""
    src = REGISTRY_PY.read_text()
    fn_start = src.index("def is_carrier_task(")
    doc = src[fn_start:src.index("return bool(", fn_start)]
    assert "NEVER streams a ``done`` event of its own" not in doc, (
        "is_carrier_task still claims the VU carrier never streams a done "
        "event — lib/chat_dispatch.py `_live_tick` emits "
        "build_carrier_terminal_done(task) for _vu_subtask and closes the "
        "stream (observed live: conv ms5j3qi7wd1g7u, task da0717c8). Leaving "
        "the stale claim invites the next reader to re-derive the cold-attach "
        "exclusion from a fact that is no longer true.")
    assert "pickVuCarrierForAttach" in doc, (
        "is_carrier_task's docstring should name the cold-attach route so the "
        "reader knows carriers ARE reachable, just not via the plain path")


if __name__ == "__main__":
    test_reducer_exposes_vu_carrier_without_collapsing_the_split()
    print("PASS reducer")
    test_cold_attach_routes_carrier_to_vu_connector()
    print("PASS cold_attach_routes_carrier")
    test_real_worker_still_uses_plain_path()
    print("PASS real_worker_plain_path")
    test_carrier_attach_is_idempotent_with_live_stream()
    print("PASS idempotent")
    test_idle_conv_still_static_renders()
    print("PASS idle_static")
    test_neuter_strip_carrier_branch_regresses()
    print("PASS neuter_strip_branch")
    test_neuter_drop_vu_flag_regresses()
    print("PASS neuter_drop_flag")
    test_reducer_never_puts_carrier_in_attachable_set()
    print("PASS split_intact")
    test_seam_wires_carrier_branch_after_plain_target()
    print("PASS seam_order")
    test_carrier_docstring_matches_shipped_behaviour()
    print("PASS docstring")
    print("ALL GREEN")
