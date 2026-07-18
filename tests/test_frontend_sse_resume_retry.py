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

THE FIX
-------
`_resumeSSEWithRetry(convId, taskId, stream, assistantMsg)` runs a BOUNDED SSE
resume loop before surrendering to poll:
  • no cursor (SSE never delivered a real id:-tagged event) → return false at
    once (proxy strips event-stream wholesale → poll is the right last resort);
  • cursor present → re-open `_trySSE`; if it runs to done → true (SSE owns
    finishStream, poll never runs);
  • a resume that STALLS (cursor unchanged after the attempt) → bail to poll,
    no spin;
  • capped at _MAX_SSE_RESUME_ATTEMPTS so choppy-but-advancing progress can't
    loop forever.

This harness extracts the REAL shipped `_resumeSSEWithRetry` body and drives it
against a scriptable `_trySSE` stub that models a tunnel: N premature closes
(each advancing the cursor) then a clean run-to-done. It asserts SSE is resumed
(not poll), the no-cursor and stalled cases surrender, abort propagates, and the
attempt cap holds. A NEUTER (retry cap → 0) proves the resume is load-bearing.
Plus a source-wiring guard so both fall-through sites keep calling it.
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
_HARNESS = r"""
'use strict';
const CFG = __CFG__;
const SCRIPT = __SCRIPT__;
let _call = 0;
const _trySSECalls = [];

function debugLog(){}
function twStop(){ _events.push('twStop'); }
function finishStream(){ _events.push('finishStream'); }
const _events = [];

const stream = {
  _lastEventId: CFG.startCursor,
  controller: { signal: { aborted: false } },
};

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

__CONST__
__FN__

(async () => {
  let ret, threw = null;
  try {
    ret = await _resumeSSEWithRetry('c1', 'T', stream, {role:'assistant'});
  } catch (e) { threw = e.name; }
  process.stdout.write(JSON.stringify({
    ret, threw,
    trySSECalls: _trySSECalls.length,
    events: _events,
    finalCursor: stream._lastEventId,
  }));
})();
"""


def _run(convs_cfg, script, *, start_cursor=5, cap_override=None):
    src = PIPE_JS.read_text()
    fn = _extract_fn(src, "_resumeSSEWithRetry")
    const = _extract_const(src, "_MAX_SSE_RESUME_ATTEMPTS")
    if cap_override is not None:
        const = f"const _MAX_SSE_RESUME_ATTEMPTS = {cap_override};"
    cfg = {"startCursor": start_cursor}
    js = (_HARNESS
          .replace("__CFG__", json.dumps(cfg))
          .replace("__SCRIPT__", json.dumps(script))
          .replace("__CONST__", const)
          .replace("__FN__", fn))
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, cwd=str(REPO))
    assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_resumes_sse_after_tunnel_drop_instead_of_poll():
    """Cursor present + a drop-that-advanced then a clean done → SSE is resumed
    and runs to done (returns true), so poll never takes over."""
    # attempt 1: reconnect, advance cursor by 3, but drop again (ok=false)
    # attempt 2: reconnect, run to done (ok=true)
    r = _run(None, [{"ok": False, "advance": 3}, {"ok": True, "advance": 2}], start_cursor=5)
    assert r["ret"] is True, r
    assert r["trySSECalls"] == 2, f"expected 2 resume attempts, got {r}"


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_no_cursor_surrenders_immediately_to_poll():
    """SSE never delivered a real id:-tagged event (no cursor) → return false at
    once with ZERO resume attempts (poll is the right last resort; no dead-air
    amplification)."""
    r = _run(None, [{"ok": True, "advance": 1}], start_cursor=None)
    assert r["ret"] is False, r
    assert r["trySSECalls"] == 0, f"must not retry with no cursor: {r}"


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_stalled_resume_bails_to_poll():
    """A reconnect that does NOT advance the cursor (stalled) → stop after one
    attempt and surrender to poll rather than spin."""
    r = _run(None, [{"ok": False, "advance": 0}], start_cursor=5)
    assert r["ret"] is False, r
    assert r["trySSECalls"] == 1, f"stalled resume should try exactly once: {r}"


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_attempt_cap_holds_on_choppy_progress():
    """Choppy-but-advancing progress that never reaches done → bounded by
    _MAX_SSE_RESUME_ATTEMPTS, then surrender to poll."""
    # every attempt advances but never returns done → must stop at the cap
    script = [{"ok": False, "advance": 1} for _ in range(20)]
    r = _run(None, script, start_cursor=5)
    assert r["ret"] is False, r
    # cap is 6 in the shipped const
    assert r["trySSECalls"] == 6, f"expected exactly the cap (6) attempts: {r}"


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_abort_propagates_from_resume():
    """A user-stop AbortError during a resume attempt propagates (connectToTask's
    single catch owns the abort finalize) — not swallowed as a poll fall-through."""
    r = _run(None, [{"abort": True}], start_cursor=5)
    assert r["threw"] == "AbortError", r


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_neuter_zero_cap_regresses():
    """NEUTER: cap → 0 → no resume ever attempted → returns false immediately →
    every premature close falls to poll (the stutter returns). Proves the retry
    is load-bearing."""
    r = _run(None, [{"ok": True, "advance": 3}], start_cursor=5, cap_override=0)
    assert r["ret"] is False, r
    assert r["trySSECalls"] == 0, f"zero-cap must attempt nothing: {r}"


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


if __name__ == "__main__":
    test_resumes_sse_after_tunnel_drop_instead_of_poll()
    print("PASS test_resumes_sse_after_tunnel_drop_instead_of_poll")
    test_no_cursor_surrenders_immediately_to_poll()
    print("PASS test_no_cursor_surrenders_immediately_to_poll")
    test_stalled_resume_bails_to_poll()
    print("PASS test_stalled_resume_bails_to_poll")
    test_attempt_cap_holds_on_choppy_progress()
    print("PASS test_attempt_cap_holds_on_choppy_progress")
    test_abort_propagates_from_resume()
    print("PASS test_abort_propagates_from_resume")
    test_neuter_zero_cap_regresses()
    print("PASS test_neuter_zero_cap_regresses")
    test_source_wires_resume_before_poll_in_both_paths()
    print("PASS test_source_wires_resume_before_poll_in_both_paths")
    print("ALL GREEN")
