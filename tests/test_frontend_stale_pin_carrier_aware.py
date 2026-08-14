"""Carrier-aware stale-pin sweep — the defect d6e8bdb3 introduced.

WHAT WENT WRONG
---------------
d6e8bdb3 made a live autopilot VU carrier reachable on cold attach. Attaching
goes through ``_connectAutopilotKick``, which sets ``conv.activeTaskId =
<carrierId>`` — necessary, because ``connectToTask`` uses that pin as the
accumulation-slot anchor and as the self-heal handle.

But the 25s poll's stale-pin sweep (``_reconcileStuckActiveTaskPins``) builds
its liveness set from ``Api.chat.active()``, and ``/api/v1/chat/active``
deliberately EXCLUDES carriers (``routes/chat.py``, ``is_carrier_task``). That
exclusion is CORRECT for that endpoint's own contract — it answers "what may I
reconnect an SSE to?", and five callers feed its result to the PLAIN
``connectToTask`` path where a carrier births a permanently-stuck "Waiting…"
bubble.

So the sweep saw a pin naming a task that was absent from its running set and
judged it stale. Measured consequences:

  * the sweep calls ``_healStuckPlaceholder(convId, {background: true})`` with
    NO ``status``, so ``_cleanDone`` evaluates FALSE and the background branch
    stamps ``finishReason = 'interrupted'`` on unsettled trailing content —
    the user reads "已中断" while the backend VU turn is still running;
  * it clears ``conv.activeTaskId`` and writes ``conv._activeTaskClearedAt``,
    and ``conv_apply_settings.js`` treats that stamp as "NEVER restore
    activeTaskId from server metadata again".

That is the objective's own disease (showing the user a state that is not
true), introduced by the fix for it.

THE FIX THIS SUITE PINS
-----------------------
Give the sweep a SECOND liveness source: the conv-state projection that
``f80b0446`` already added, which DOES see carriers (``<tid>#vu``). Concretely
the sweep reads ``conv._authoritativeActiveTaskIds`` — the set the REAL reducer
derives from that projection, with markers already stripped — so there is no
second hand-rolled parse of the wire. A pin survives when EITHER source still
knows the task.

Ordering is load-bearing: ``applyConvStateSnapshot`` must run BEFORE the sweep,
or the sets it reads are from the previous tick.

Fail-safe posture is preserved and made explicit: the sweep touches NOTHING
unless BOTH probes succeeded. The pre-existing contract already said "never
clear on a probe failure"; a carrier is exactly the case where a missing second
probe means "I cannot prove this is stale".

WHY NOT put carriers on /api/chat/active
----------------------------------------
That is the tempting one-line change and it is wrong — it would reach five
consumers that feed the plain connectToTask path. ``test_chat_active_still_
excludes_carriers`` in the poll-lane suite is the complement guard for that.

ASSERTED HERE
-------------
  1. a pin naming a LIVE VU carrier is NOT cleared, the trailing turn is NOT
     stamped interrupted, and no ``_activeTaskClearedAt`` is written;
  2. a genuinely stale pin IS still cleared (the reverse defect — a sweep that
     never clears anything would re-open "the dot outlives the work");
  3. a live PLAIN worker is still left alone (unchanged behaviour);
  4. probe failure on EITHER source clears nothing (fail-safe);
  5. the snapshot is applied BEFORE the sweep in the shipped poll body.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parent.parent
SYNC_JS = REPO / "static" / "js" / "core" / "cross_tab_sync.js"
REDUCER_JS = REPO / "static" / "js" / "core" / "conv_state_reducer.js"


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
    """The REAL reducer — the sweep must read ITS output, not a second parse."""
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
        _extract_fn(src, "_withoutTerminalTaskIds"),
        _extract_fn(src, "applyRunningTaskIdsFrame"),
        _extract_fn(src, "applyConvStateSnapshot"),
    ])


_HARNESS = r"""
'use strict';
const CFG = __CFG__;
let conversations = __CONVS__;
let activeConvId = null;
const activeStreams = new Map();
const _healed = [];

function debugLog() {}
function renderConversationList() {}
function twStop() {}
function saveConversations() {}

/* Model the REAL _healStuckPlaceholder background branch closely enough to
 * observe the two user-visible effects the ticket names: an `interrupted`
 * stamp on unsettled trailing content, and the recovery-suppressing stamp. */
function _healStuckPlaceholder(convId, probe) {
  const conv = conversations.find((c) => c && c.id === convId);
  if (!conv) return false;
  if (!conv.activeTaskId) return false;
  _healed.push(convId);
  const last = conv.messages[conv.messages.length - 1];
  const cleanDone = !probe.notFound && ['done','completed','complete','stop','finished']
    .includes(String(probe.status || '').toLowerCase());
  if (!cleanDone && last && last.role === 'assistant' && !last.finishReason && !last.error
      && (last.content || last.thinking
          || (Array.isArray(last.toolRounds) && last.toolRounds.length))) {
    last.finishReason = 'interrupted';
  }
  conv.activeTaskId = null;
  conv._activeTaskClearedAt = Date.now();
  return true;
}

__REDUCER_FNS__
__SWEEP_FN__

/* Apply the projection through the REAL reducer FIRST (the shipped poll body
 * does the same — ordering is asserted separately in source). */
if (CFG.projection) applyConvStateSnapshot(conversations, CFG.projection);

const cleared = _reconcileStuckActiveTaskPins(CFG.activeTasks, CFG.projection);

const c = conversations.find((x) => x && x.id === 'c1') || {};
const last = (c.messages || [])[(c.messages || []).length - 1] || {};
process.stdout.write(JSON.stringify({
  cleared: cleared,
  healed: _healed,
  activeTaskId: c.activeTaskId === undefined ? null : c.activeTaskId,
  clearedStamp: !!c._activeTaskClearedAt,
  finishReason: last.finishReason || null,
}));
"""


def _run(convs, *, active_tasks, projection):
    sweep = _extract_fn(SYNC_JS.read_text(), "_reconcileStuckActiveTaskPins")
    cfg = {"activeTasks": active_tasks, "projection": projection}
    script = (_HARNESS
              .replace("__REDUCER_FNS__", _reducer_fns())
              .replace("__SWEEP_FN__", sweep)
              .replace("__CFG__", json.dumps(cfg))
              .replace("__CONVS__", json.dumps(convs)))
    out = subprocess.run(["node", "-e", script], capture_output=True,
                         text=True, cwd=str(REPO))
    assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
    return json.loads(out.stdout.strip().splitlines()[-1])


def _conv_pinned_to(task_id):
    """A conv whose pin names `task_id`, with UNSETTLED trailing content — the
    shape that gets wrongly stamped `interrupted`."""
    return [{
        "id": "c1",
        "activeTaskId": task_id,
        "messages": [
            {"role": "user", "content": "go", "_msgId": "u1"},
            {"role": "assistant", "content": "partial…", "_msgId": "a1"},
        ],
    }]


def _projection(running_ids):
    return {"convs": {"c1": {"runningTaskIds": running_ids,
                             "runningTaskIdsRev": [100, "r0"]}}}


# ══════════════════════════════════════════════════════════════════════
#  1. THE DEFECT — a live VU carrier pin must survive the sweep.
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_a_live_vu_carrier_pin_is_not_swept():
    """The pin names a carrier that IS live (present in the conv-state
    projection as '<tid>#vu') but is ABSENT from /api/chat/active by design.
    The sweep must leave it alone — and must not stamp the trailing turn."""
    r = _run(_conv_pinned_to("carrier-1"),
             active_tasks=[],                        # carriers hidden here
             projection=_projection(["carrier-1#vu"]))
    assert r["cleared"] == 0, (
        "the sweep cleared a pin whose VU carrier is still live — /api/chat/"
        f"active hides carriers by design, so it cannot be the only source: {r}")
    assert r["healed"] == [], r
    assert r["activeTaskId"] == "carrier-1", \
        f"the live carrier pin was dropped: {r}"
    assert r["clearedStamp"] is False, \
        f"_activeTaskClearedAt was written — suppresses future recovery: {r}"
    assert r["finishReason"] is None, (
        "the trailing turn was stamped 'interrupted' while the VU turn is "
        f"still running — the user reads 已中断 on live work: {r}")


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_a_genuinely_stale_pin_is_still_cleared():
    """COMPLEMENT — the reverse defect. A pin whose task is gone from BOTH
    sources must still be reclaimed, or 'the busy dot outlives the work'
    (the bug this sweep was built for) comes back."""
    r = _run(_conv_pinned_to("dead-task"),
             active_tasks=[],
             projection=_projection([]))
    assert r["cleared"] == 1, \
        f"a genuinely stale pin was NOT cleared — sweep is now inert: {r}"
    assert r["activeTaskId"] is None, r
    assert r["clearedStamp"] is True, r


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_a_live_plain_worker_pin_is_left_alone():
    """Unchanged behaviour: a normal running task is visible in
    /api/chat/active and must not be swept."""
    r = _run(_conv_pinned_to("worker-1"),
             active_tasks=[{"id": "worker-1", "status": "running",
                            "aborted": False, "convId": "c1"}],
             projection=_projection(["worker-1"]))
    assert r["cleared"] == 0, r
    assert r["activeTaskId"] == "worker-1", r


# ══════════════════════════════════════════════════════════════════════
#  2. Fail-safe — a missing probe must never license a clear.
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_active_probe_failure_clears_nothing():
    """Pre-existing contract, preserved: /api/chat/active unreachable
    (null) → touch nothing."""
    r = _run(_conv_pinned_to("carrier-1"),
             active_tasks=None,
             projection=_projection(["carrier-1#vu"]))
    assert r["cleared"] == 0 and r["activeTaskId"] == "carrier-1", r


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_projection_probe_failure_clears_nothing():
    """NEW half of the same posture: without the projection we cannot prove a
    pin is not a live carrier, so clearing would be a guess. A guess that
    stamps 'interrupted' on live work is exactly the defect."""
    r = _run(_conv_pinned_to("carrier-1"),
             active_tasks=[],
             projection=None)
    assert r["cleared"] == 0, (
        "the sweep cleared a pin without the carrier-aware source — it cannot "
        f"distinguish 'stale' from 'live carrier' in that state: {r}")
    assert r["activeTaskId"] == "carrier-1", r
    assert r["finishReason"] is None, r


# ══════════════════════════════════════════════════════════════════════
#  3. Source-drift: ordering + single-source discipline.
# ══════════════════════════════════════════════════════════════════════

def test_sweep_reads_the_reducer_output_not_a_second_wire_parse():
    """Single source of truth: the sweep must consult
    ``_authoritativeActiveTaskIds`` (what the REAL reducer derived) rather than
    re-deriving busy ids from ``runningTaskIds`` itself. A second parse would
    drift from the reducer's marker handling — the exact split that produced
    this whole family of bugs.

    STRIPS COMMENTS FIRST (charter #24). The first draft of this guard banned
    the literal ``#vu`` and was satisfied by the sweep's own explanatory
    comment, which names the marker to explain why the sweep must NOT parse it.
    Same failure family as the docstring-satisfies-text-scan defect earlier in
    this line of work: prose about a rule must never be able to violate — or
    satisfy — that rule.
    """
    from tests._source_scan import strip_comments

    fn = _extract_fn(SYNC_JS.read_text(), "_reconcileStuckActiveTaskPins")
    code = strip_comments(fn, lang='js')
    assert "_authoritativeActiveTaskIds" in code, (
        "the sweep does not read the reducer's busy set — it is either still "
        "carrier-blind or has grown a second wire parse")
    assert "#vu" not in code, (
        "the sweep re-implements marker stripping in CODE; that belongs to the "
        "reducer alone (conv_state_reducer.js::_stripVuMarker)")
    assert "runningTaskIds" not in code, (
        "the sweep parses the raw wire field itself — read the reducer's "
        "derived set instead so marker handling lives in exactly one place")


def test_snapshot_is_applied_before_the_sweep_in_the_poll_body():
    """Ordering is load-bearing: if the sweep runs before
    applyConvStateSnapshot, it reads LAST tick's busy sets and a carrier that
    started this tick is still invisible."""
    body = _extract_fn(SYNC_JS.read_text(), "_crossDeviceReconcile")
    i_apply = body.find("applyConvStateSnapshot")
    i_sweep = body.find("_reconcileStuckActiveTaskPins")
    assert i_apply != -1, "poll body no longer applies the conv-state snapshot"
    assert i_sweep != -1, "poll body no longer runs the stale-pin sweep"
    assert i_apply < i_sweep, (
        "the stale-pin sweep runs BEFORE applyConvStateSnapshot — it would "
        "read the previous tick's busy sets and stay carrier-blind for a "
        "carrier that started during this tick")


def test_sweep_receives_both_liveness_sources():
    """The call site must hand the sweep BOTH sources; passing only
    activeTasks silently restores the carrier-blind behaviour."""
    body = _extract_fn(SYNC_JS.read_text(), "_crossDeviceReconcile")
    m = re.search(r"_reconcileStuckActiveTaskPins\(([^)]*)\)", body)
    assert m, "no call to _reconcileStuckActiveTaskPins in the poll body"
    args = [a.strip() for a in m.group(1).split(",") if a.strip()]
    assert len(args) >= 2, (
        f"the sweep is called with {len(args)} argument(s) — it needs the "
        f"conv-state projection as a second liveness source: {m.group(0)}")


if __name__ == "__main__":
    test_a_live_vu_carrier_pin_is_not_swept()
    print("PASS live_carrier_pin_survives")
    test_a_genuinely_stale_pin_is_still_cleared()
    print("PASS stale_pin_still_cleared")
    test_a_live_plain_worker_pin_is_left_alone()
    print("PASS plain_worker_left_alone")
    test_active_probe_failure_clears_nothing()
    print("PASS active_probe_failsafe")
    test_projection_probe_failure_clears_nothing()
    print("PASS projection_probe_failsafe")
    test_sweep_reads_the_reducer_output_not_a_second_wire_parse()
    print("PASS single_source")
    test_snapshot_is_applied_before_the_sweep_in_the_poll_body()
    print("PASS ordering")
    test_sweep_receives_both_liveness_sources()
    print("PASS both_sources")
    print("ALL GREEN")
