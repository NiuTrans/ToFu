"""Frontend reconnect-on-open — the "click into a conversation → stuck/stalled
bubble that only a full page refresh clears" bug.

WHY
---
`loadConversation` (static/js/main/main_conv_lifecycle.js) historically
re-attached ONLY a stream already live in THIS tab (`activeStreams`). It never
reconnected to a task still RUNNING on the SERVER when this tab held no stream
entry — the task was started in another tab, or the SSE dropped and
`finishStream` cleared `activeStreams` while the backend kept generating. The
conversation then rendered STATICALLY with no SSE, no poll, no `twStart`, so the
trailing assistant placeholder sat frozen ("等待中…") until a full page refresh
ran `initActiveTasks`' reconnect.

THE FIX
-------
`_reconnectServerTaskIfIdle(id)` — invoked in BOTH loadConversation open
branches before the static-render fall-through. It keys off the
SERVER-AUTHORITATIVE `conv.activeTaskId` (persisted `settings.activeTaskId`),
never a client guess, and delegates to the existing `connectToTask` (the single
reconnect mechanism used by boot-init / send / regen / edit / cross-tab).
`connectToTask` resolves its accumulation slot by identity (`_taskId` /
`_msgId`), so it re-targets the running task's already-persisted placeholder
(no duplicate bubble) and self-heals a stale `activeTaskId` for an
already-finished task via its poll → 404 → `finishStream` path.

This harness drives the REAL shipped `_reconnectServerTaskIfIdle` body under
node with a minimal stubbed environment, plus asserts the loadConversation
wiring + gate in source so the regression rots with the code. A NEUTER that
strips the `connectToTask` call proves the reconnect is load-bearing (the frozen
placeholder returns).
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parent.parent
LIFECYCLE_JS = REPO / "static" / "js" / "main" / "main_conv_lifecycle.js"


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


# The harness stubs every global the real _reconnectServerTaskIfIdle reads:
#   activeStreams (Map), conversations (array), connectToTask, showStreamingUIForConv, console.
# connectToTask is a configurable stub whose "behaviour" models what the real
# reconnect mechanism does: 'running' arms a live stream (activeStreams.set +
# twStart), 'finished' arms then self-heals (finishStream clears the stream and
# appends NO placeholder). Both record the invocation.
_HARNESS = r"""
'use strict';
const CFG = __CFG__;                       // {behaviour, seedStream}
let conversations = __CONVS__;
const activeStreams = new Map();
if (CFG.seedStream) activeStreams.set(CFG.seedStream, { controller: {}, taskId: 'LIVE' });

const _calls = { connectToTask: [], showStreaming: [], twStart: [], finishStream: [] };

function twStart(id) { _calls.twStart.push(id); }
function finishStream(id) {
  _calls.finishStream.push(id);
  activeStreams.delete(id);   // the settle path clears the running predicate
}
function showStreamingUIForConv(id) { _calls.showStreaming.push(id); }

function connectToTask(id, taskId) {
  _calls.connectToTask.push([id, taskId]);
  const conv = conversations.find(c => c.id === id);
  if (!conv) return;
  // Real connectToTask re-targets an existing bound slot by identity and does
  // NOT append a second placeholder for the running task's own persisted one.
  // (We never push here → asserts "no duplicate bubble".)
  const controller = { signal: { aborted: false } };
  activeStreams.set(id, { controller, taskId, assistantMsg: conv.messages[conv.messages.length - 1] || null });
  conv.activeTaskId = taskId;
  twStart(id);
  if (CFG.behaviour === 'finished') {
    // Stale activeTaskId → the task already finished server-side. The real
    // poll → 404/terminal → finishStream path settles it: clears the stream,
    // leaves the already-persisted content, appends NO ghost placeholder.
    finishStream(id);
  }
}

const console = { info(){}, warn(){}, error(){}, log(){}, debug(){} };

__FN__

const _ret = _reconnectServerTaskIfIdle(__OPEN_ID__);
console.log && 0;
process.stdout.write(JSON.stringify({
  ret: _ret,
  calls: _calls,
  streamLive: activeStreams.has(__OPEN_ID__),
  assistantCount: (conversations.find(c => c.id === __OPEN_ID__) || {messages:[]}).messages.filter(m => m.role === 'assistant').length,
}));
"""


def _run(fn_src, convs, open_id, *, behaviour="running", seed_stream=None):
    cfg = {"behaviour": behaviour, "seedStream": seed_stream}
    script = (_HARNESS
              .replace("__FN__", fn_src)
              .replace("__CFG__", json.dumps(cfg))
              .replace("__CONVS__", json.dumps(convs))
              .replace("__OPEN_ID__", json.dumps(open_id)))
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, cwd=str(REPO))
    assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
    return json.loads(out.stdout.strip().splitlines()[-1])


def _fn():
    return _extract_fn(LIFECYCLE_JS.read_text(), "_reconnectServerTaskIfIdle")


# A conv with a persisted server-side running task but NO local stream + its
# already-persisted trailing placeholder (the running task's own slot).
def _running_conv():
    return [{
        "id": "c1",
        "activeTaskId": "T",
        "messages": [
            {"role": "user", "content": "hi", "_msgId": "u1"},
            {"role": "assistant", "content": "", "_taskId": "T", "_msgId": "a1"},
        ],
    }]


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_reconnects_to_running_server_task():
    """activeTaskId set + no live stream → connectToTask invoked, twStart arms,
    streaming UI painted, and NO duplicate assistant bubble appended."""
    r = _run(_fn(), _running_conv(), "c1", behaviour="running")
    assert r["ret"] is True, r
    assert r["calls"]["connectToTask"] == [["c1", "T"]], r
    assert r["calls"]["twStart"] == ["c1"], f"twStart did not arm: {r}"
    assert r["calls"]["showStreaming"] == ["c1"], r
    assert r["streamLive"] is True, r
    # The running task's already-persisted placeholder is re-targeted, not
    # duplicated: still exactly one assistant message.
    assert r["assistantCount"] == 1, f"duplicate assistant bubble appended: {r}"


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_stale_finished_task_settles_no_permanent_placeholder():
    """Open a conv whose activeTaskId points at an ALREADY-FINISHED task → the
    reconnect delegates to connectToTask's self-heal (poll → finishStream),
    which clears the running predicate and leaves NO permanent frozen
    placeholder — instead of static-rendering a stuck bubble."""
    r = _run(_fn(), _running_conv(), "c1", behaviour="finished")
    assert r["ret"] is True, r
    assert r["calls"]["connectToTask"] == [["c1", "T"]], r
    assert r["calls"]["finishStream"] == ["c1"], f"did not settle via finishStream: {r}"
    # finishStream cleared the stream → no lingering live-stream ghost.
    assert r["streamLive"] is False, f"stream not cleared after settle: {r}"
    assert r["assistantCount"] == 1, r


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_idempotent_when_stream_already_live():
    """A stream already live in THIS tab → no double-connect (the caller's
    activeStreams.has branch owns it)."""
    r = _run(_fn(), _running_conv(), "c1", behaviour="running", seed_stream="c1")
    assert r["ret"] is False, r
    assert r["calls"]["connectToTask"] == [], f"double-connected over a live stream: {r}"
    assert r["calls"]["twStart"] == [], r


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_running_push_does_not_preempt_send_owned_attachment():
    """A runningTaskIds push may expose the task id before /chat/send returns.
    The send pipeline still owns the canonical placeholder and SSE attachment
    during that lease, so reconnect must be suppressed.  ``True`` tells callers
    to suppress their static-render fallback too."""
    convs = _running_conv()
    convs[0]["_sendInFlight"] = True
    r = _run(_fn(), convs, "c1", behaviour="running")
    assert r["ret"] is True, r
    assert r["calls"]["connectToTask"] == [], (
        f"running-state push stole stream ownership from sendMessage: {r}")
    assert r["calls"]["showStreaming"] == [], r
    assert r["calls"]["twStart"] == [], r
    assert r["streamLive"] is False, r
    assert r["assistantCount"] == 1, r


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_no_reconnect_without_active_task_id():
    """No persisted activeTaskId → static-render path preserved (reconnect is a
    no-op). Gates strictly on the server-authoritative field, never a guess."""
    convs = [{"id": "c1", "messages": [
        {"role": "user", "content": "hi", "_msgId": "u1"},
        {"role": "assistant", "content": "done", "finishReason": "stop", "_msgId": "a1"},
    ]}]  # no activeTaskId
    r = _run(_fn(), convs, "c1", behaviour="running")
    assert r["ret"] is False, r
    assert r["calls"]["connectToTask"] == [], r


@pytest.mark.skipif(not _node_available(), reason="node not installed")
def test_neuter_strip_connect_regresses():
    """NEUTER: strip the connectToTask call(s) from the fn body → the running
    task is never reconnected → the frozen-placeholder bug returns (no arm).

    The seam now attaches on EITHER of two branches: the plain worker attach
    (``connectToTask(id, targetTid)``) and the VU-carrier fallback
    (``connectToTask(id, _vuCarrierTid, 0, {vuCarrier:true})``). Neutering only
    the plain one would leave the carrier branch calling connectToTask — and a
    neuter that left ANY attach firing would not prove the reconnect is
    load-bearing, so we strip BOTH and then assert neither fired."""
    src = _fn()
    # Anchor on the connectToTask CALLS, not their argument expressions — the
    # plain call resolves its tid via pickAuthoritativeTaskIdForReconnect and
    # the carrier call passes a 3-arg (opts) form, so neither is a fixed
    # 2-arg literal. Match any connectToTask(...) invocation regardless of
    # arg count, but never the delegated definition elsewhere.
    neutered, n = re.subn(r"connectToTask\(id, [^;]*\);",
                          "/* connectToTask neutered */ void 0;", src)
    assert n >= 1, "no connectToTask call found in _reconnectServerTaskIfIdle"
    assert neutered != src, "neuter pattern did not match connectToTask call"
    r = _run(neutered, _running_conv(), "c1", behaviour="running")
    assert r["calls"]["connectToTask"] == [], \
        f"neutered fn should not call connectToTask ({n} sites stripped)"
    assert r["calls"]["twStart"] == [], \
        f"neutered fn should not arm a stream: {r}"


def test_source_wires_reconnect_in_both_open_branches():
    """The shipped loadConversation must call _reconnectServerTaskIfIdle in BOTH
    open branches (the _needsLoad .then and the already-loaded else), gated on
    the server-authoritative conv.activeTaskId — so this rots with the code."""
    src = LIFECYCLE_JS.read_text()
    assert "function _reconnectServerTaskIfIdle(" in src, \
        "_reconnectServerTaskIfIdle helper removed"
    # Exactly two call sites inside loadConversation's open branches (the
    # `if (...)` guard form — excludes the `function ...(id) {` definition).
    call_sites = src.count("if (_reconnectServerTaskIfIdle(id))")
    assert call_sites == 2, \
        f"expected 2 if(_reconnectServerTaskIfIdle(id)) call sites (both open branches), found {call_sites}"
    # Gate must require a non-null reconnect target tid — resolved via
    # pickAuthoritativeTaskIdForReconnect (union of conv.activeTaskId and the
    # server-authoritative Set), never a bare client guess — and guard
    # idempotency on activeStreams.
    assert "pickAuthoritativeTaskIdForReconnect" in src, \
        "reconnect no longer resolves its target via pickAuthoritativeTaskIdForReconnect"
    assert re.search(r"if \(!targetTid\) return false;", src), \
        "reconnect gate no longer requires a non-null target task id"
    assert "activeStreams.has(id)) return false;" in src, \
        "reconnect idempotency guard (skip when a stream is already live) removed"
    assert re.search(r"connectToTask\(id, targetTid\);", src), \
        "reconnect no longer delegates to the existing connectToTask mechanism"


if __name__ == "__main__":
    test_reconnects_to_running_server_task()
    print("PASS test_reconnects_to_running_server_task")
    test_stale_finished_task_settles_no_permanent_placeholder()
    print("PASS test_stale_finished_task_settles_no_permanent_placeholder")
    test_idempotent_when_stream_already_live()
    print("PASS test_idempotent_when_stream_already_live")
    test_no_reconnect_without_active_task_id()
    print("PASS test_no_reconnect_without_active_task_id")
    test_neuter_strip_connect_regresses()
    print("PASS test_neuter_strip_connect_regresses")
    test_source_wires_reconnect_in_both_open_branches()
    print("PASS test_source_wires_reconnect_in_both_open_branches")
    print("ALL GREEN")
