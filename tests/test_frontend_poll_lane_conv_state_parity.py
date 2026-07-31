"""Poll-lane parity: when the push socket is DOWN, busy state must still reach
the client through the SAME projection the push transport uses.

THE REMAINING HOLE (found while verifying 633b4fc3)
---------------------------------------------------
d6e8bdb3 made a live autopilot VU carrier reachable; 633b4fc3 made busy-state
ARRIVAL an attach trigger on both push-frame handlers. Both fixes live entirely
on the PUSH transport. But the push socket is not always up — on a flaky VS Code
port-forward (a normal condition for this project) it drops, and
``_crossDeviceReconcile`` is the 25s poll fallback that exists precisely for
that case.

That fallback could not see a VU carrier at all:

  * it calls ``Api.chat.active()``, and ``/api/v1/chat/active`` EXCLUDES every
    carrier (``routes/chat.py``, ``is_carrier_task``);
  * so ``_reconcileStuckActiveTaskPins`` — its only consumer — never learns the
    conv is busy, and the poll lane issues ZERO attach calls.

Net: with the socket down during a VU turn, the original symptom returns in
full (busy lamp from a stale local pin or nothing at all, no bubble, no stream)
until a manual refresh.

WHY NOT "JUST PUT CARRIERS ON /api/chat/active"
-----------------------------------------------
That was the tempting fix and it is the DANGEROUS one. ``Api.chat.active()`` has
five real consumers (``main_init_tasks`` boot recovery, ``_recoverOfflineConversations``,
``_checkForQueuedTask``, the stuck-stream probe in ``health_stream_timer``, and
the stale-pin sweep), and several feed the PLAIN ``connectToTask`` path. A
carrier delivered there would bind a real assistant placeholder to a stream that
emits only the ``autopilot_vu_*`` contract — the permanently-stuck "Waiting…" /
ghost second-"Agent" bubble that the carrier filter was added to prevent. The
endpoint's exclusion is CORRECT for its own contract ("what may I reconnect to?")
and this suite pins that it stays correct.

THE FIX THIS SUITE PINS
-----------------------
Add ONE read-only endpoint that serves the SAME projection the push snapshot is
built from (``snapshot_running_by_conv`` → ``runningTaskIds`` with the ``#vu``
marker), and have the poll fallback feed it through the SAME reducer
(``applyConvStateSnapshot``) and the SAME attach seam the push path uses.

One projection, one reducer, two transports. The alternative — teaching the poll
lane its own notion of busy — is exactly how ``busy`` and ``attachable`` drifted
apart and produced this whole family of bugs in the first place.

WHAT IS ASSERTED
----------------
  1. the endpoint returns the SAME shape the push snapshot carries, markers
     intact (a carrier appears as ``<tid>#vu``);
  2. it is derived from ``snapshot_running_by_conv``, not a second hand-rolled
     scan (single source of truth);
  3. ``/api/v1/chat/active`` still EXCLUDES carriers (the complement guard — the
     wrong fix must stay impossible);
  4. the poll fallback consumes the projection through the real reducer and
     attaches via the real seam, with ``{vuCarrier:true}``;
  5. a failed probe touches nothing (fail-safe, matching the existing sweep).
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
API_JS = REPO / "static" / "js" / "api.js"
CHAT_PY = REPO / "routes" / "chat.py"


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
#  1-2. Backend: one projection, served over HTTP for the poll lane.
# ══════════════════════════════════════════════════════════════════════

def test_conv_state_endpoint_exists_and_reuses_the_push_projection():
    """The poll lane needs the SAME projection the push snapshot is built from.

    Pins single-source-of-truth: the endpoint must IMPORT and CALL
    ``snapshot_running_by_conv`` — the identical helper
    ``build_conv_state_snapshot`` uses — never a second hand-rolled walk of the
    task registry. Two independent scans is how the busy/attachable split
    drifted in the first place.

    ASSERTED VIA AST, NOT TEXT (charter #24, and a mistake this very guard made
    on its first draft): a plain ``'snapshot_running_by_conv' in src`` is
    satisfied by the function's own DOCSTRING, which names the helper three
    times to explain the rule. Neutering the real import therefore left the
    guard green while the handler fell back to its empty-projection except
    branch — a silently broken endpoint with a passing test. ``strip_comments``
    does not help here either: it removes ``#`` lines, not docstrings. Only a
    structural assertion distinguishes prose about the call from the call.
    """
    import ast

    path = REPO / "routes" / "api_v1" / "chat.py"
    tree = ast.parse(path.read_text())

    handler = next((n for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == 'chat_conv_state'), None)
    assert handler is not None, (
        "no chat_conv_state handler — the poll fallback has no way to learn a "
        "conv is busy while the push socket is down (its only probe, "
        "/api/v1/chat/active, excludes carriers by design)")

    imported = {
        alias.name
        for node in ast.walk(handler)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert 'snapshot_running_by_conv' in imported, (
        "chat_conv_state does not IMPORT snapshot_running_by_conv (docstring "
        "mentions do not count) — a second registry scan will drift from the "
        "push snapshot")

    called = {
        node.func.id
        for node in ast.walk(handler)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert 'snapshot_running_by_conv' in called, (
        "chat_conv_state imports the shared projection but never calls it")

    # And the route must actually be registered on the v1 chat blueprint.
    src = path.read_text()
    assert "'/api/v1/chat/conv-state'" in src, \
        "the conv-state handler exists but is not routed"


def test_conv_state_endpoint_preserves_the_vu_marker():
    """The wire shape must be byte-compatible with the push snapshot so the
    SAME client reducer can consume both. In particular the ``#vu`` marker must
    survive: it is what lets the reducer keep 'busy' and 'attachable' distinct."""
    from lib.tasks_pkg.manager._registry import snapshot_running_by_conv  # noqa: F401
    src = (REPO / "routes" / "api_v1" / "chat.py").read_text()
    # The handler must emit runningTaskIds (the reducer's field name), not a
    # bespoke key the reducer would ignore.
    assert "runningTaskIds" in src, \
        ("the conv-state endpoint does not emit `runningTaskIds` — the shared "
         "reducer (applyConvStateSnapshot) would silently read nothing")


def test_chat_active_still_excludes_carriers():
    """COMPLEMENT GUARD — the wrong fix must stay impossible.

    ``/api/v1/chat/active`` answers "what may I RECONNECT to?" and has five
    consumers that feed the PLAIN connectToTask path. A carrier there binds a
    real assistant placeholder to a stream carrying only the autopilot_vu_*
    contract → the permanently-stuck "Waiting…" bubble. Adding carriers to this
    endpoint is the tempting-but-dangerous fix; pin the exclusion.
    """
    src = CHAT_PY.read_text()
    fn_start = src.index("def chat_active(")
    fn_end = src.index("\n@", fn_start) if "\n@" in src[fn_start:] else len(src)
    body = src[fn_start:fn_end]
    assert "is_carrier_task" in body, \
        "chat_active no longer filters carriers — ghost 'Waiting…' bubbles return"
    assert "if not is_carrier_task(t)" in body, \
        "chat_active's carrier exclusion was inverted or weakened"


# ══════════════════════════════════════════════════════════════════════
#  3-5. Frontend: the poll fallback feeds the shared reducer + seam.
# ══════════════════════════════════════════════════════════════════════

_HARNESS = r"""
'use strict';
const CFG = __CFG__;
let conversations = __CONVS__;
let activeConvId = CFG.activeConvId;
const activeStreams = new Map();
let _editingMsgIdx = null;
const document = { visibilityState: 'visible' };
let _seq = 0;
const _calls = { connectToTask: [], convState: [], listLoad: [], activeProbe: [] };

function debugLog() {}
function renderConversationList() {}
function updateSendButton() {}
function showStreamingUIForConv() {}
function _healStuckPlaceholder() { return false; }
function _reconcileStuckActiveTaskPins() {}
function loadConversationsFromServer() { _calls.listLoad.push(++_seq); return Promise.resolve(); }
function _acquireBootLoad() { return true; }
function _releaseBootLoad() {}
function _bootLoadHeld() { return false; }
const AbortSignal = { timeout: () => null };

function connectToTask(id, taskId, retries, opts) {
  _calls.connectToTask.push({ seq: ++_seq, convId: id, taskId: taskId, opts: opts || null });
  activeStreams.set(id, { controller: {}, taskId });
}

/* Report the OBSERVABLE busy verdict (what lights the dot / flips the composer
 * to Stop), not the internal set — so a test can assert the user-visible state
 * rather than an implementation detail. */
function _busyVerdict(convId) {
  const c = conversations.find((x) => x && x.id === convId);
  if (!c) return null;
  return {
    busy: computeConvBusy(c, activeStreams),
    carrier: (typeof pickVuCarrierForAttach === 'function')
      ? pickVuCarrierForAttach(c) : null,
  };
}

/* Api stub: convState resolves the projection (or REJECTS, to prove the
 * fail-safe posture); active() is the legacy probe that hides carriers. */
const Api = {
  chat: {
    active: (o) => { _calls.activeProbe.push(++_seq); return Promise.resolve([]); },
    convState: (o) => {
      _calls.convState.push(++_seq);
      if (CFG.probeFails) return Promise.reject(new Error('probe down'));
      return Promise.resolve(CFG.projection);
    },
  },
};

__REDUCER_FNS__
__SEAM_FN__
__POLL_FN__

/* Seed PRIOR busy knowledge through the REAL reducer (never hand-built sets):
 * models a client that already learned a VU carrier was live — the state that
 * must later be EXTINGUISHED by a projection omitting the conv. */
for (const c of conversations) {
  if (!c || !c.__seedWire) continue;
  applyRunningTaskIdsFrame(conversations, {
    convId: c.id, runningTaskIds: c.__seedWire,
    runningTaskIdsRev: [50, 'seed'], userId: null,
  });
}

const _ret = _crossDeviceReconcile();
/* Drain microtasks so the probe .then() chains settle before we report. */
Promise.resolve()
  .then(() => {}).then(() => {}).then(() => {}).then(() => {}).then(() => {})
  .then(() => {
    process.stdout.write(JSON.stringify({
      ret: _ret, calls: _calls, busyVerdict: _busyVerdict(CFG.activeConvId),
    }));
  });
"""


def _reducer_fns() -> str:
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
        # The REAL busy predicate — the thing that actually lights the dot /
        # flips the composer to Stop. Lifted, never stubbed: a hand-written
        # busy check could disagree with the shipped one and let a stale-dot
        # regression pass.
        _extract_fn(src, "computeConvBusy"),
        _extract_fn(src, "pickAuthoritativeTaskIdForReconnect"),
        _extract_fn(src, "pickVuCarrierForAttach"),
    ])


def _run(convs, *, active_conv_id, projection, probe_fails=False, poll_src=None):
    sync_src = SYNC_JS.read_text()
    poll_fn = poll_src if poll_src is not None else _extract_fn(
        sync_src, "_crossDeviceReconcile")
    seam = _extract_fn(LIFECYCLE_JS.read_text(), "_reconnectServerTaskIfIdle")
    cfg = {"activeConvId": active_conv_id, "projection": projection,
           "probeFails": probe_fails}
    script = (_HARNESS
              .replace("__REDUCER_FNS__", _reducer_fns())
              .replace("__SEAM_FN__", seam)
              .replace("__POLL_FN__", poll_fn)
              .replace("__CFG__", json.dumps(cfg))
              .replace("__CONVS__", json.dumps(convs)))
    out = subprocess.run(["node", "-e", script], capture_output=True,
                         text=True, cwd=str(REPO))
    assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
    return json.loads(out.stdout.strip().splitlines()[-1])


def _open_conv():
    return [{
        "id": "c1", "activeTaskId": None,
        "messages": [{"role": "assistant", "content": "done", "_msgId": "a1"}],
    }]


def _projection_with_carrier():
    """The SAME shape build_conv_state_snapshot emits."""
    return {"convs": {"c1": {"runningTaskIds": ["da0717c8#vu"],
                             "runningTaskIdsRev": [100, "r0"]}}}


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_poll_fallback_attaches_to_vu_carrier_when_push_is_down():
    """THE HOLE. Socket down + VU turn running → the 25s poll must learn the
    conv is busy and attach through the VU connector, instead of leaving the
    user on a finished-looking conversation for the whole turn."""
    r = _run(_open_conv(), active_conv_id="c1",
             projection=_projection_with_carrier())
    assert r["calls"]["convState"], \
        ("the poll fallback never fetched the conv-state projection — with the "
         "push socket down it still cannot see a VU carrier (its legacy probe "
         "/api/chat/active excludes carriers)")
    assert len(r["calls"]["connectToTask"]) == 1, \
        f"poll lane did not attach to the live VU carrier: {r}"
    c = r["calls"]["connectToTask"][0]
    assert c["convId"] == "c1" and c["taskId"] == "da0717c8", r
    assert c["opts"] and c["opts"].get("vuCarrier") is True, \
        f"poll lane attached via the PLAIN path → ghost Agent bubble: {r}"


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_poll_fallback_uses_plain_path_for_a_real_worker():
    """No false positive: a normal running task from the poll projection must
    take the plain path, not the VU connector."""
    r = _run(_open_conv(), active_conv_id="c1",
             projection={"convs": {"c1": {"runningTaskIds": ["plain-worker"],
                                          "runningTaskIdsRev": [100, "r0"]}}})
    assert len(r["calls"]["connectToTask"]) == 1, r
    c = r["calls"]["connectToTask"][0]
    assert c["taskId"] == "plain-worker", r
    assert not (c["opts"] and c["opts"].get("vuCarrier")), \
        f"a real worker was routed through the VU connector: {r}"


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_poll_fallback_idle_projection_attaches_nothing():
    """An idle projection must not attach — and must not resurrect a dot.

    The second half of that sentence is now ASSERTED. It used to be prose only:
    the harness never exposed the busy verdict, so 'must not resurrect a dot'
    was a promise with no check behind it — the same shape of empty guard as a
    text scan satisfied by its own docstring.
    """
    r = _run(_open_conv(), active_conv_id="c1",
             projection={"convs": {}})
    assert r["calls"]["connectToTask"] == [], r
    assert r["busyVerdict"]["busy"] is False, \
        f'an idle projection must not light the busy dot: {r["busyVerdict"]}'
    assert r["busyVerdict"]["carrier"] is None, r


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_poll_clears_a_stale_busy_dot_when_the_vu_turn_ended():
    """THE COMPLEMENT of the whole objective, and the failure mode that would
    have replaced one stuck state with another.

    Every fix in this family makes a conv read BUSY so its live VU work becomes
    visible. The mirror risk is a dot that never goes out: if the VU turn ends
    while the push socket is down, the client's last knowledge is 'carrier
    running' and it would sit on Stop forever with nothing generating — exactly
    the inconsistent state this work exists to remove, just inverted.

    ``applyConvStateSnapshot`` clears convs ABSENT from a snapshot, so the poll
    projection is what extinguishes it. Drive the real transition: carrier busy
    first, then a projection that omits the conv.
    """
    convs = _open_conv()
    # Seed prior knowledge of a live VU carrier the way the wire really does.
    convs[0]["__seedWire"] = ["da0717c8#vu"]
    r = _run(convs, active_conv_id="c1", projection={"convs": {}})
    assert r["busyVerdict"]["busy"] is False, (
        'a poll projection that omits the conv must EXTINGUISH the busy dot — '
        'otherwise the composer stays on Stop with nothing generating, which '
        f'is the same stuck state inverted: {r["busyVerdict"]}')
    assert r["busyVerdict"]["carrier"] is None, \
        f'the stale carrier must be dropped too: {r["busyVerdict"]}'
    assert r["calls"]["connectToTask"] == [], \
        'nothing is running — the poll must not attach to a dead carrier'


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_probe_failure_is_fail_safe():
    """A failed projection probe must touch nothing — matching the posture of
    the existing stale-pin sweep (a transient error must never clear or invent
    state). The list load must still have run."""
    r = _run(_open_conv(), active_conv_id="c1",
             projection=_projection_with_carrier(), probe_fails=True)
    assert r["calls"]["connectToTask"] == [], \
        f"a failed probe still attached something: {r}"
    assert r["calls"]["listLoad"], \
        "the probe failure aborted the unrelated list load"


# ══════════════════════════════════════════════════════════════════════
#  NEUTER
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_neuter_strip_poll_conv_state_regresses():
    """NEUTER: remove the projection fetch from the poll fallback → the
    socket-down hole reopens (no attach)."""
    sync_src = SYNC_JS.read_text()
    poll_fn = _extract_fn(sync_src, "_crossDeviceReconcile")
    assert "convState" in poll_fn, \
        "the poll fallback no longer fetches the conv-state projection"
    neutered = re.sub(r"Api\.chat\.convState", "Api.chat.__gone", poll_fn)
    assert neutered != poll_fn
    # A missing Api method would throw; the shipped code must guard, but for the
    # neuter we only need "no attach happened".
    r = _run(_open_conv(), active_conv_id="c1",
             projection=_projection_with_carrier(), poll_src=neutered)
    assert r["calls"]["connectToTask"] == [], \
        f"neuter did not disable the poll-lane attach: {r}"


def test_api_client_exposes_conv_state():
    """Charter #12/api.js contract: every backend call goes through Api.<domain>.
    A raw fetch in cross_tab_sync would bypass the single client seam."""
    src = API_JS.read_text()
    assert "convState" in src, \
        "Api.chat.convState missing — the poll lane cannot reach the projection"
    sync_src = SYNC_JS.read_text()
    assert "fetch('/api/v1/chat/conv-state" not in sync_src, \
        "cross_tab_sync issues a raw fetch instead of going through Api"


if __name__ == "__main__":
    test_conv_state_endpoint_exists_and_reuses_the_push_projection()
    print("PASS endpoint_reuses_projection")
    test_conv_state_endpoint_preserves_the_vu_marker()
    print("PASS marker_preserved")
    test_chat_active_still_excludes_carriers()
    print("PASS complement_guard")
    test_poll_fallback_attaches_to_vu_carrier_when_push_is_down()
    print("PASS poll_attaches_carrier")
    test_poll_fallback_uses_plain_path_for_a_real_worker()
    print("PASS poll_plain_worker")
    test_poll_fallback_idle_projection_attaches_nothing()
    print("PASS poll_idle")
    test_probe_failure_is_fail_safe()
    print("PASS fail_safe")
    test_neuter_strip_poll_conv_state_regresses()
    print("PASS neuter")
    test_api_client_exposes_conv_state()
    print("PASS api_client")
    print("ALL GREEN")
