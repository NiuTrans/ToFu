"""Frontend "回来即新" resume-revalidation (epic ③).

On every RESUME moment — tab re-visible / browser online / push-socket
RECONNECT — the client must IMMEDIATELY reconcile the conversation list AND the
open conversation's body, instead of waiting for the slow fallback poll (the
"等半天才同步" symptom on a flaky tunnel where the `notify` WebSocket drops).

Two real shipped units are exercised under a node harness (no bundler/DOM):
  • `_revalidateOnResume` (static/js/core/cross_tab_sync.js) — proves it (a)
    loads the list AND verifies the active conv, (b) is IDEMPOTENT via the
    shared `_bootLoadInFlight` lease (a second concurrent resume is a no-op, so
    3 near-simultaneous resume events can't storm the endpoint), (c) is gated
    off during an edit and skips the body verify while streaming.
  • `_push.onReconnect` (static/js/push.js) — proves reconnect listeners fire
    on a GENUINE re-open but NOT on the first connect (boot already loads).

NEUTER: reverting `_revalidateOnResume` to `return false` proves nothing
reconciles (the logic is load-bearing).
"""
import re
import subprocess
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CS_JS = REPO / "static" / "js" / "core" / "cross_tab_sync.js"
PUSH_JS = REPO / "static" / "js" / "push.js"


def _extract_fn(src: str, name: str, prefix: str = "function") -> str:
    m = re.search(r"%s\s+%s\s*\(" % (re.escape(prefix), re.escape(name)), src)
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


_HARNESS = r"""
'use strict';
let _editingMsgIdx = null;
const activeStreams = new Set();
let activeConvId = 'c1';
function debugLog() {}

// Instrumentation counters.
let listLoads = 0;
let activeVerifies = 0;

// Shared in-flight lease (mirrors the real window-scoped latch semantics).
const _BOOT_LOAD_LEASE_MS = 45000;
if (typeof window === 'undefined') { global.window = {}; }
window._bootLoadInFlight = 0;
function _bootLoadHeld() {
  const t = window._bootLoadInFlight;
  if (!t) return false;
  if (typeof t === 'number' && (Date.now() - t) > _BOOT_LOAD_LEASE_MS) return false;
  return true;
}
function _acquireBootLoad() { if (_bootLoadHeld()) return false; window._bootLoadInFlight = Date.now(); return true; }
function _releaseBootLoad() { window._bootLoadInFlight = 0; }

// A CONTROLLABLE list load: stays pending until we resolve it, so we can test
// that a SECOND resume during an in-flight load is a no-op (idempotence).
let _resolveList = null;
function loadConversationsFromServer() {
  listLoads++;
  return new Promise((res) => { _resolveList = res; });
}
function _verifyActiveConvFromServer(cid) {
  activeVerifies++;
  return Promise.resolve();
}

__FN__

module.exports = {
  run: async (scenario) => {
    if (scenario === 'edit') _editingMsgIdx = 3;
    if (scenario === 'stream') activeStreams.add('c1');

    // First resume — should acquire the lease and load.
    const r1 = _revalidateOnResume('t1');
    // Second resume WHILE the first load is still pending — must be a no-op.
    const r2 = _revalidateOnResume('t2');

    // Resolve the in-flight list load → releases lease → fires active verify.
    if (_resolveList) _resolveList();
    await new Promise((res) => setTimeout(res, 5));

    return { r1, r2, listLoads, activeVerifies, leaseAfter: window._bootLoadInFlight };
  },
};
"""


def _run_scenario(scenario: str, neuter: bool = False) -> dict:
    src = CS_JS.read_text()
    if neuter:
        fn = "function _revalidateOnResume(trigger) { return false; }"
    else:
        fn = _extract_fn(src, "_revalidateOnResume")
    harness = _HARNESS.replace("__FN__", fn)
    script = harness + "\n(async()=>{const m=module.exports;console.log(JSON.stringify(await m.run(%r)));})();" % scenario
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, cwd=str(REPO))
    assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
    import json
    line = [l for l in out.stdout.strip().splitlines() if l.strip().startswith("{")][-1]
    return json.loads(line)


def test_resume_loads_list_and_verifies_active():
    """Normal resume (settled conv): loads the list ONCE and verifies the open
    conv's body ONCE."""
    r = _run_scenario('normal')
    assert r["r1"] is True, r
    assert r["listLoads"] == 1, r
    assert r["activeVerifies"] == 1, r
    assert r["leaseAfter"] == 0, "lease must be released after the load settles"


def test_second_concurrent_resume_is_noop():
    """A second resume while the first load is in flight is a NO-OP — the shared
    lease dedupes it, so 3 near-simultaneous resume events can't storm."""
    r = _run_scenario('normal')
    assert r["r2"] is False, "second concurrent resume must short-circuit on the lease"
    assert r["listLoads"] == 1, "must NOT double-load"


def test_edit_gates_everything():
    """An in-progress edit blocks the whole resume (no list load, no verify)."""
    r = _run_scenario('edit')
    assert r["r1"] is False, r
    assert r["listLoads"] == 0, r
    assert r["activeVerifies"] == 0, r


def test_streaming_active_conv_skips_body_verify():
    """While the active conv is streaming: the list still reconciles (merge
    guards streaming convs) but the body verify is SKIPPED so it never fights a
    live turn."""
    r = _run_scenario('stream')
    assert r["r1"] is True, r
    assert r["listLoads"] == 1, r
    assert r["activeVerifies"] == 0, "must not verify a streaming active conv"


def test_neuter_reconciles_nothing():
    """NEUTER: `return false` → no list load, no verify. Biting control."""
    r = _run_scenario('normal', neuter=True)
    assert r["r1"] is False, r
    assert r["listLoads"] == 0, r
    assert r["activeVerifies"] == 0, r


# ── push.js: reconnect fires ONLY on a genuine re-open, not first connect ──

_PUSH_HARNESS = r"""
'use strict';
// Minimal WebSocket stub whose lifecycle we drive manually.
let _openCb = null;
class FakeWS {
  constructor() { this.readyState = 0; FakeWS.last = this; }
  send() {}
  close() { this.readyState = 3; if (this.onclose) this.onclose({ code: 1006 }); }
}
FakeWS.OPEN = 1; FakeWS.CONNECTING = 0; FakeWS.CLOSED = 3;
global.WebSocket = FakeWS;
global.window = { location: { protocol: 'http:', host: 'x' } };
function apiUrl(p) { return p; }
// silence console noise
global.console = Object.assign({}, console, { info(){}, debug(){}, warn(){} });

__PUSH_SRC__

let fires = 0;
pushOnReconnect(() => { fires++; });

// Simulate: first connect (onopen) — must NOT fire (initial connect).
pushConnect();
FakeWS.last.readyState = 1; FakeWS.last.onopen();
const afterFirst = fires;
// Drop, then reconnect (onclose → we manually connect again + onopen).
FakeWS.last.onclose({ code: 1006 });
pushConnect();
FakeWS.last.readyState = 1; FakeWS.last.onopen();
const afterReconnect = fires;

console.log(JSON.stringify({ afterFirst, afterReconnect }));
"""


def test_push_reconnect_fires_only_on_genuine_reopen():
    """`onReconnect` listeners fire on a genuine reconnect but NOT on the first
    connect (boot already loads the list — firing there would double-load)."""
    src = PUSH_JS.read_text()
    # Strip the trailing bundler tags / nothing needed; the IIFE + public fns
    # are self-contained. Run the whole file.
    script = _PUSH_HARNESS.replace("__PUSH_SRC__", src)
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, cwd=str(REPO))
    assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
    import json
    line = [l for l in out.stdout.strip().splitlines() if l.strip().startswith("{")][-1]
    r = json.loads(line)
    assert r["afterFirst"] == 0, "must NOT fire on the first connect"
    assert r["afterReconnect"] == 1, "must fire exactly once on a genuine reconnect"


if __name__ == "__main__":
    test_resume_loads_list_and_verifies_active(); print("PASS load+verify")
    test_second_concurrent_resume_is_noop(); print("PASS idempotent")
    test_edit_gates_everything(); print("PASS edit-gate")
    test_streaming_active_conv_skips_body_verify(); print("PASS stream-skip")
    test_neuter_reconciles_nothing(); print("PASS neuter")
    test_push_reconnect_fires_only_on_genuine_reopen(); print("PASS push-reconnect")
    print("ALL GREEN")
