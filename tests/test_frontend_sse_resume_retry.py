"""Frontend SSE resume-retry — the "stuttering stream on a buffering tunnel" bug.

WHY
---
On a VS Code port-forward / nginx / corporate proxy that BUFFERS or DROPS the
SSE connection mid-turn, `_trySSE` (static/js/ui/sse_pipeline.js) returns false
on the premature close. The OLD `connectToTask` tail dropped STRAIGHT to
`_pollFallback` and stayed there for the rest of the turn → lumpy 300–2000ms
RTT-adaptive full-repaints (the reported stutter). Yet `_trySSE` had already
stashed `stream._lastEventId` (the last received `id:` cursor) and re-armed a
fresh controller — everything needed to RESUME a live SSE via the server's
Last-Event-ID replay path (routes/chat.py).

THE FIX (original)
------------------
`_resumeSSEWithRetry(convId, taskId, stream, assistantMsg)` runs a BOUNDED SSE
resume loop before surrendering to poll: no cursor → false at once; a resume
that runs to done → true; bounded by _MAX_SSE_RESUME_ATTEMPTS.

★★ 2026-08-06 AMENDMENT (epic pt_6cb1607e, owner-mandated contract change)
---------------------------------------------------------------------------
Measured incident 2026-08-06 14:02:25: the VS Code tunnel killed ALL FOUR live
streams in the same second. The old ladder fired every retry with ZERO gap, so
the whole budget was spent inside the ~3s flap window ("resume stalled …
surrendering to poll" ×4 at 14:02:28) → PERMANENT poll surrender against a
tunnel that healed seconds later. Two coupled defects, two coupled fixes:

  • BACKOFF — attempts 2..N ride `_SSE_RESUME_BACKOFF_MS` = 0.5→16s
    exponential (slept BEFORE the attempt, abort-aware via `_sleepOrAbort`),
    so retries land AFTER the flap passes instead of burning out inside it.
  • STALLED TOLERANCE — a resume whose cursor didn't advance no longer
    surrenders on the FIRST strike: during a flap a reconnect routinely
    attaches a beat before the server has anything new to replay, so one
    stalled reading is noise. Only `_SSE_RESUME_MAX_STALLED` CONSECUTIVE
    stalls are a verdict; an advancing cursor resets the count.

This harness extracts the REAL shipped `_resumeSSEWithRetry` body plus the
three behavioural consts and drives it against a scriptable `_trySSE` stub
(models a tunnel) with `_sleepOrAbort` stubbed to record the backoff schedule
instantly. NEUTERs: cap → 0 (no resume at all) and stalled-tolerance → 1
(the old one-strike behaviour) prove both mechanisms are load-bearing. A
source-wiring guard keeps both fall-through sites calling it before poll.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parent.parent
PIPE_JS = REPO / "static" / "js" / "ui" / "sse_pipeline.js"


def _node_available() -> bool:
    return bool(shutil.which("node"))


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


def _extract_const(src: str, name: str) -> str:
    m = re.search(r"const %s\s*=\s*[^;]+;" % re.escape(name), src)
    assert m, f"const {name} not found"
    return m.group(0)


# _trySSE stub script (`SCRIPT`) is an array of outcomes, one per call:
#   {ok: bool, advance: int, abort: bool}
#   - advance: how much the cursor (stream._lastEventId) moves BEFORE returning
#              (models the server replaying some post-cursor events, then dropping)
#   - ok:      _trySSE's return (true = ran to done)
#   - abort:   throw an AbortError from this _trySSE call (user stop)
# Initial cursor is CFG.startCursor (null = SSE never delivered a real event).
# `_sleepOrAbort` is stubbed to record the backoff schedule without sleeping.
_HARNESS = r"""
'use strict';
const CFG = __CFG__;
const SCRIPT = __SCRIPT__;
let _call = 0;
const _trySSECalls = [];
const _sleeps = [];

function debugLog(){}

const stream = {
  _lastEventId: CFG.startCursor,
  controller: { signal: { aborted: false } },
};

async function _sleepOrAbort(ms, signal) { _sleeps.push(ms); }

async function _trySSE(convId, taskId, strm, msg) {
  const step = SCRIPT[_call] || { ok: false, advance: 0 };
  _call++;
  _trySSECalls.push(step);
  if (step.advance) {
    strm._lastEventId = (strm._lastEventId || 0) + step.advance;
  }
  if (step.abort) { const e = new Error('aborted'); e.name = 'AbortError'; throw e; }
  return !!step.ok;
}

__CONSTS__
__FN__

(async () => {
  let ret, threw = null;
  try {
    ret = await _resumeSSEWithRetry('c1', 'T', stream, {role:'assistant'});
  } catch (e) { threw = e.name; }
  process.stdout.write(JSON.stringify({
    ret, threw,
    trySSECalls: _trySSECalls.length,
    sleeps: _sleeps,
    finalCursor: stream._lastEventId,
  }));
})();
"""


def _run(script, *, start_cursor=5, cap_override=None, stalled_override=None):
    src = PIPE_JS.read_text()
    fn = _extract_fn(src, "_resumeSSEWithRetry")
    cap = _extract_const(src, "_MAX_SSE_RESUME_ATTEMPTS")
    backoff = _extract_const(src, "_SSE_RESUME_BACKOFF_MS")
    max_stalled = _extract_const(src, "_SSE_RESUME_MAX_STALLED")
    if cap_override is not None:
        cap = f"const _MAX_SSE_RESUME_ATTEMPTS = {cap_override};"
    if stalled_override is not None:
        max_stalled = f"const _SSE_RESUME_MAX_STALLED = {stalled_override};"
    consts = "\n".join([cap, backoff, max_stalled])
    cfg = {"startCursor": start_cursor}
    js = (_HARNESS
          .replace("__CFG__", json.dumps(cfg))
          .replace("__SCRIPT__", json.dumps(script))
          .replace("__CONSTS__", consts)
          .replace("__FN__", fn))
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, cwd=str(REPO))
    assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_resumes_sse_after_tunnel_drop_instead_of_poll():
    """Cursor present + a drop-that-advanced then a clean done → SSE is resumed
    and runs to done (returns true), so poll never takes over. Attempt 2 pays
    the first backoff rung (500ms) — the fast path stays immediate only for
    attempt 1."""
    r = _run([{"ok": False, "advance": 3}, {"ok": True, "advance": 2}], start_cursor=5)
    assert r["ret"] is True, r
    assert r["trySSECalls"] == 2, f"expected 2 resume attempts, got {r}"
    assert r["sleeps"] == [500], f"attempt 2 must pay the first backoff rung: {r}"


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_no_cursor_surrenders_immediately_to_poll():
    """SSE never delivered a real id:-tagged event (no cursor) → return false at
    once with ZERO resume attempts (poll is the right last resort; no dead-air
    amplification)."""
    r = _run([{"ok": True, "advance": 1}], start_cursor=None)
    assert r["ret"] is False, r
    assert r["trySSECalls"] == 0, f"must not retry with no cursor: {r}"
    assert r["sleeps"] == [], f"no-cursor surrender must not sleep: {r}"


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_stalled_resume_tolerates_k_then_surrenders():
    """★ 2026-08-06 contract (owner mandate): ONE stalled attempt is noise
    during a tunnel flap — the reconnect routinely attaches a beat before the
    server has anything new to replay. Surrender only after
    _SSE_RESUME_MAX_STALLED (3) CONSECUTIVE stalls, with the backoff ladder
    between them."""
    r = _run([{"ok": False, "advance": 0} for _ in range(5)], start_cursor=5)
    assert r["ret"] is False, r
    assert r["trySSECalls"] == 3, f"must tolerate K=3 consecutive stalls: {r}"
    assert r["sleeps"] == [500, 1000], f"stalled attempts ride the ladder: {r}"


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_stall_counter_resets_on_progress():
    """Stalls interleaved with cursor-advancing progress never reach K
    consecutive — the attempt cap, not the stall rule, bounds the loop."""
    script = ([{"ok": False, "advance": 0}, {"ok": False, "advance": 0},
               {"ok": False, "advance": 1}] * 4)
    r = _run(script, start_cursor=5)
    assert r["ret"] is False, r
    assert r["trySSECalls"] == 7, f"advancing cursor resets the stall count; cap (7) owns the bound: {r}"


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_attempt_cap_holds_on_choppy_progress():
    """Choppy-but-advancing progress that never reaches done → bounded by
    _MAX_SSE_RESUME_ATTEMPTS (7), then surrender to poll. The recorded sleep
    schedule pins the FULL 0.5→16s backoff ladder (gaps before attempts 2-7)."""
    script = [{"ok": False, "advance": 1} for _ in range(20)]
    r = _run(script, start_cursor=5)
    assert r["ret"] is False, r
    assert r["trySSECalls"] == 7, f"expected exactly the cap (7) attempts: {r}"
    assert r["sleeps"] == [500, 1000, 2000, 4000, 8000, 16000], \
        f"the 0.5→16s exponential ladder is load-bearing source: {r}"


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_abort_propagates_from_resume():
    """A user-stop AbortError during a resume attempt propagates (connectToTask's
    single catch owns the abort finalize) — not swallowed as a poll fall-through."""
    r = _run([{"abort": True}], start_cursor=5)
    assert r["threw"] == "AbortError", r


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_neuter_zero_cap_regresses():
    """NEUTER: cap → 0 → no resume ever attempted → returns false immediately →
    every premature close falls to poll (the stutter returns). Proves the retry
    is load-bearing."""
    r = _run([{"ok": True, "advance": 3}], start_cursor=5, cap_override=0)
    assert r["ret"] is False, r
    assert r["trySSECalls"] == 0, f"zero-cap must attempt nothing: {r}"


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_neuter_stalled_1_restores_one_strike_surrender():
    """NEUTER: stalled-tolerance → 1 restores the OLD one-strike behaviour —
    a single stalled attempt surrenders immediately. Proves the K-tolerance
    (not the cap) is what carries the flap survival."""
    r = _run([{"ok": False, "advance": 0}, {"ok": True, "advance": 1}],
             start_cursor=5, stalled_override=1)
    assert r["ret"] is False, r
    assert r["trySSECalls"] == 1, f"tolerance 1 = one-strike surrender: {r}"


def test_source_wires_resume_before_poll_in_both_paths():
    """Both fall-through sites (connectToTask + _connectAutopilotKick) must call
    _resumeSSEWithRetry BEFORE _pollFallback, so this rots with the code."""
    src = PIPE_JS.read_text()
    assert "async function _resumeSSEWithRetry(" in src, "resume helper removed"
    # Match the CALL form (`await _resumeSSEWithRetry(...)`) so the function
    # DEFINITION line's identical signature isn't counted.
    assert src.count("await _resumeSSEWithRetry(convId, taskId, stream, assistantMsg)") == 1, \
        "connectToTask no longer calls the resume helper before poll"
    assert src.count("await _resumeSSEWithRetry(convId, taskId, stream, dummyAssistant)") == 1, \
        "_connectAutopilotKick no longer calls the resume helper before poll"
    # The resume call must precede the poll fall-through in the file (both sites).
    for kind, arg in (("connectToTask", "assistantMsg"), ("kick", "dummyAssistant")):
        rpos = src.index(f"await _resumeSSEWithRetry(convId, taskId, stream, {arg})")
        ppos = src.index(f"_pollFallback(convId, taskId, stream, {arg})", rpos)
        assert rpos < ppos, f"{kind}: resume must come before poll fall-through"
    # Gate: no-cursor surrender is what keeps poll the last resort.
    assert "if (!cursor) return false;" in src, "no-cursor surrender guard removed"


def test_source_wires_backoff_and_stall_tolerance():
    """The 2026-08-06 amendment is load-bearing SOURCE: strip the ladder or the
    K-tolerance and the flap-window burn returns while every behavioural test
    above could still be neutered green. Pin both seams."""
    src = PIPE_JS.read_text()
    assert "const _SSE_RESUME_BACKOFF_MS = [500, 1000, 2000, 4000, 8000, 16000];" in src, \
        "backoff ladder removed or degraded — retries fire inside the flap window again"
    assert "const _SSE_RESUME_MAX_STALLED = 3;" in src, \
        "stalled tolerance removed — one-strike surrender returns"
    assert "function _sleepOrAbort(" in src, \
        "abortable sleep removed — a user Stop would wait out a 16s backoff gap"
    assert "await _sleepOrAbort(" in src, "backoff sleep never runs"
    assert "stalled = 0;" in src, "stall counter never resets on progress"


if __name__ == "__main__":
    test_resumes_sse_after_tunnel_drop_instead_of_poll()
    print("PASS test_resumes_sse_after_tunnel_drop_instead_of_poll")
    test_no_cursor_surrenders_immediately_to_poll()
    print("PASS test_no_cursor_surrenders_immediately_to_poll")
    test_stalled_resume_tolerates_k_then_surrenders()
    print("PASS test_stalled_resume_tolerates_k_then_surrenders")
    test_stall_counter_resets_on_progress()
    print("PASS test_stall_counter_resets_on_progress")
    test_attempt_cap_holds_on_choppy_progress()
    print("PASS test_attempt_cap_holds_on_choppy_progress")
    test_abort_propagates_from_resume()
    print("PASS test_abort_propagates_from_resume")
    test_neuter_zero_cap_regresses()
    print("PASS test_neuter_zero_cap_regresses")
    test_neuter_stalled_1_restores_one_strike_surrender()
    print("PASS test_neuter_stalled_1_restores_one_strike_surrender")
    test_source_wires_resume_before_poll_in_both_paths()
    print("PASS test_source_wires_resume_before_poll_in_both_paths")
    test_source_wires_backoff_and_stall_tolerance()
    print("PASS test_source_wires_backoff_and_stall_tolerance")
    print("ALL GREEN")
