#!/usr/bin/env python3
"""Frontend: "click an old conversation → float to top" (durable) + the
server-authoritative elapsed-timer seed that keeps the timer from restarting
at 0 on refresh.

TWO shipped functions are exercised under node with a minimal harness that
runs their REAL source (extracted from the shipped bundle files):

  1. `_bumpConvOnOpen(id)` (static/js/main/main_conv_lifecycle.js)
       • bumps conv.updatedAt = now so _convSorter floats it to the top,
       • persists via Api.conversations.patchSettings(id,{touchUpdatedAt:true}),
       • BUT is a NO-OP when the conv has a live/active task
         (activeStreams.has(id) || conv.activeTaskId) — mirrors the
         saveConversations streaming carve-out that stops sidebar flicker.

  2. `_seedStreamTimerStart(convId, serverStartMs)` (core/health_stream_timer.js)
       • rewinds the elapsed timer's startTime to the server-authoritative
         task start, min-guarded: only ever moves startTime EARLIER, and
         ignores a future/NaN value — so the displayed elapsed can never jump
         backward.

NEUTER tests strip each guard and prove the guarded behaviour breaks — so the
tests pin the real guards, not incidental code.
"""
import json
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parent.parent
LIFECYCLE_JS = REPO / "static" / "js" / "main" / "main_conv_lifecycle.js"
TIMER_JS = REPO / "static" / "js" / "core" / "health_stream_timer.js"


def _extract_fn(src: str, name: str) -> str:
    m = re.search(r"(async\s+)?function %s\s*\(" % re.escape(name), src)
    assert m, f"{name} not found"
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


# ── _bumpConvOnOpen harness ──────────────────────────────────────────
_BUMP_HARNESS = r"""
'use strict';
const NOW = 5_000_000;
Date.now = () => NOW;
let conversations = __CONVS__;
const activeStreams = new Map(__ACTIVE_STREAMS__);
const _rendered = [];
const _patched = [];
function renderConversationList() { _rendered.push(conversations.map(c => c.id)); }
// Real _convSorter (recency-first, active tasks first) — mirror the shipped one.
function _convSorter(a, b) {
  const aAct = (activeStreams.has(a.id) || a.activeTaskId) ? 1 : 0;
  const bAct = (activeStreams.has(b.id) || b.activeTaskId) ? 1 : 0;
  if (aAct !== bAct) return bAct - aAct;
  return (b.updatedAt || b.createdAt || 0) - (a.updatedAt || a.createdAt || 0);
}
const ConvCache = { put() {} };
const Api = { conversations: { patchSettings(id, patch) { _patched.push({ id, patch }); return Promise.resolve(); } } };

__FN__

_bumpConvOnOpen(__OPEN_ID__);
const _byId = {}; conversations.forEach(c => _byId[c.id] = c.updatedAt);
console.log(JSON.stringify({ order: conversations.map(c => c.id), updatedAt: _byId, patched: _patched, rendered: _rendered.length }));
"""


def _run_bump(fn_src, convs, open_id, active_streams=None):
    script = (_BUMP_HARNESS
              .replace("__FN__", fn_src)
              .replace("__CONVS__", json.dumps(convs))
              .replace("__ACTIVE_STREAMS__", json.dumps([[s, {}] for s in (active_streams or [])]))
              .replace("__OPEN_ID__", json.dumps(open_id)))
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, cwd=str(REPO))
    assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
    last = [ln for ln in out.stdout.strip().splitlines() if ln.strip().startswith("{")][-1]
    return json.loads(last)


def _bump_fn():
    return _extract_fn(LIFECYCLE_JS.read_text(), "_bumpConvOnOpen")


# Old conv (low updatedAt) buried below a newer one.
def _convs():
    return [
        {"id": "conv-new", "updatedAt": 4_000_000},
        {"id": "conv-old", "updatedAt": 1_000},
    ]


def test_open_bump_floats_old_conv_to_top():
    """Opening the old conv bumps its updatedAt = now → it sorts to the top."""
    r = _run_bump(_bump_fn(), _convs(), "conv-old")
    assert r["order"][0] == "conv-old", f"old conv should float to top: {r}"
    assert r["updatedAt"]["conv-old"] == 5_000_000, r


def test_open_bump_persists_touch_flag():
    """The bump persists via patchSettings with the touchUpdatedAt control flag
    (durable across reload) — and carries ONLY that flag."""
    r = _run_bump(_bump_fn(), _convs(), "conv-old")
    assert r["patched"] == [{"id": "conv-old", "patch": {"touchUpdatedAt": True}}], r


def test_open_bump_skips_when_active_stream():
    """Active-task guard: a conv with a live stream is NOT bumped/persisted
    (mirrors the saveConversations streaming carve-out that stops flicker)."""
    r = _run_bump(_bump_fn(), _convs(), "conv-old", active_streams=["conv-old"])
    assert r["updatedAt"]["conv-old"] == 1_000, f"streaming conv must not be bumped: {r}"
    assert r["patched"] == [], f"streaming conv must not be persisted: {r}"


def test_open_bump_skips_when_active_task_id():
    """Active-task guard (persisted activeTaskId variant): also a no-op."""
    convs = _convs()
    convs[1]["activeTaskId"] = "tk-live"
    r = _run_bump(_bump_fn(), convs, "conv-old")
    assert r["updatedAt"]["conv-old"] == 1_000, r
    assert r["patched"] == [], r


def test_neuter_active_guard_bumps_streaming_conv():
    """NEUTER: strip the active-task guard → a streaming conv IS bumped (the
    flicker-inducing behaviour the guard prevents)."""
    src = _bump_fn()
    neutered = re.sub(r"if \(activeStreams\.has\(id\) \|\| conv\.activeTaskId\) return;[^\n]*",
                      "/* guard neutered */", src, count=1)
    assert neutered != src and "activeStreams.has(id) || conv.activeTaskId) return" not in neutered, \
        "neuter did not strip the active-task guard"
    r = _run_bump(neutered, _convs(), "conv-old", active_streams=["conv-old"])
    assert r["updatedAt"]["conv-old"] == 5_000_000, f"neutered guard should bump streaming conv: {r}"


# ── _seedStreamTimerStart harness ────────────────────────────────────
_SEED_HARNESS = r"""
'use strict';
const NOW = 1_000_000;
Date.now = () => NOW;
const _streamTimers = new Map();
_streamTimers.set('c1', { startTime: __INITIAL_START__ });
let _uiCalls = 0;
function _updateStreamTimerUI() { _uiCalls++; }

__FN__

_seedStreamTimerStart('c1', __SEED__);
const info = _streamTimers.get('c1');
console.log(JSON.stringify({ startTime: info ? info.startTime : null, uiCalls: _uiCalls }));
"""


def _run_seed(fn_src, initial_start, seed_value):
    script = (_SEED_HARNESS
              .replace("__FN__", fn_src)
              .replace("__INITIAL_START__", json.dumps(initial_start))
              .replace("__SEED__", json.dumps(seed_value)))
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, cwd=str(REPO))
    assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
    last = [ln for ln in out.stdout.strip().splitlines() if ln.strip().startswith("{")][-1]
    return json.loads(last)


def _seed_fn():
    return _extract_fn(TIMER_JS.read_text(), "_seedStreamTimerStart")


def test_seed_rewinds_start_earlier():
    """A server start EARLIER than the client start rewinds startTime (elapsed
    now counts from the real task start, not from the connect instant)."""
    r = _run_seed(_seed_fn(), initial_start=900_000, seed_value=500_000)
    assert r["startTime"] == 500_000, f"startTime should rewind to server start: {r}"
    assert r["uiCalls"] >= 1, "should repaint immediately"


def test_seed_never_moves_start_later():
    """min-guard: a server start LATER than the current start is ignored — the
    displayed elapsed can never jump backward."""
    r = _run_seed(_seed_fn(), initial_start=500_000, seed_value=900_000)
    assert r["startTime"] == 500_000, f"startTime must not move later: {r}"


def test_seed_ignores_future_and_nan():
    """A future timestamp (clock skew) or a non-number is ignored."""
    r_future = _run_seed(_seed_fn(), initial_start=900_000, seed_value=2_000_000)  # > NOW
    assert r_future["startTime"] == 900_000, f"future start ignored: {r_future}"
    r_nan = _run_seed(_seed_fn(), initial_start=900_000, seed_value="not-a-number")
    assert r_nan["startTime"] == 900_000, f"NaN start ignored: {r_nan}"


def test_neuter_min_guard_moves_start_later():
    """NEUTER: strip the `ms < info.startTime` min-guard → a later server start
    would move startTime forward (the backward-jump bug the guard prevents)."""
    src = _seed_fn()
    neutered = src.replace("if (ms < info.startTime) {", "if (true) {", 1)
    assert neutered != src, "neuter did not strip the min-guard"
    # Use a NON-future later value so only the min-guard (not the future guard)
    # is what would have blocked it.
    r = _run_seed(neutered, initial_start=500_000, seed_value=900_000)
    assert r["startTime"] == 900_000, f"neutered min-guard should move start later: {r}"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL GREEN")
