"""Connection-toast dedupe (reconnect flicker/noise guard).

On a flaky tunnel, each `_pollFallback` runs PER-CONVERSATION, so N concurrent
streaming convs — or a tunnel that drops→recovers→drops repeatedly — would each
fire their OWN "Connection Lost"/"Reconnected"/"Server Offline" toast. That
connection-noise reads to the user as confusing flicker. `_connToast(phase,...)`
(static/js/ui/sse_poll_fallback.js) coalesces the three connection toasts through
one window-scoped state machine keyed by connection PHASE, not convId.

Runs the REAL shipped `_connToast` body under a minimal node stub (no jsdom —
matches the repo's node-free source-assertion convention). Proves:
  • a repeat 'lost' within the cooldown is suppressed (2 concurrent convs → 1 toast);
  • 'reconnected' only fires if an outage was announced first (no bare "Reconnected");
  • lost→offline escalation is allowed through immediately;
  • after a full lost→reconnected cycle, a fresh 'lost' shows again.
NEUTER: a body that always calls showToast (no state machine) fires on every call.
"""
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PF_JS = REPO / "static" / "js" / "ui" / "sse_poll_fallback.js"


def _extract_fn(src: str, name: str) -> str:
    m = re.search(r"function %s\s*\(" % re.escape(name), src)
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
let _toasts = [];
global.window = {};
function showToast(icon, title){ _toasts.push(title); }

__FN__

// Scenario: two concurrent convs both hit an outage, then recovery, then a
// SECOND outage cycle. Count how many of each toast title actually surfaced.
_connToast('lost', '', 'Connection Lost', 'm', 1);      // conv A → shows
_connToast('lost', '', 'Connection Lost', 'm', 1);      // conv B → suppressed (dup within cooldown)
_connToast('reconnected', '', 'Reconnected', 'm', 1);   // recovery → shows (outage was announced)
_connToast('reconnected', '', 'Reconnected', 'm', 1);   // conv B recovery → suppressed (already ok)
_connToast('lost', '', 'Connection Lost', 'm', 1);      // new outage after recovery → shows again
_connToast('offline', '', 'Server Offline', 'm', 1);    // escalation → shows immediately

const lost = _toasts.filter(t => t === 'Connection Lost').length;
const recon = _toasts.filter(t => t === 'Reconnected').length;
const offline = _toasts.filter(t => t === 'Server Offline').length;

// Control: a bare 'reconnected' with NO preceding outage must NOT toast.
global.window._connToastState = { phase: 'ok', at: 0 };
_toasts = [];
_connToast('reconnected', '', 'Reconnected', 'm', 1);
const spuriousRecon = _toasts.length;

console.log(JSON.stringify({ lost, recon, offline, spuriousRecon }));
"""


def _run(neuter=False):
    src = PF_JS.read_text()
    fn = ("function _connToast(p,i,title,m,d){ showToast(i,title); }"
          if neuter else _extract_fn(src, "_connToast"))
    script = _HARNESS.replace("__FN__", fn)
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, cwd=str(REPO))
    assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
    import json
    line = [l for l in out.stdout.strip().splitlines() if l.strip().startswith("{")][-1]
    return json.loads(line)


def test_dedupes_concurrent_and_repeat_toasts():
    r = _run()
    # 2 outage cycles → 'lost' shows exactly twice (once per real outage, the
    # concurrent duplicate suppressed).
    assert r["lost"] == 2, r
    # 'reconnected' shows once (the concurrent duplicate suppressed).
    assert r["recon"] == 1, r
    # escalation to offline shows immediately.
    assert r["offline"] == 1, r


def test_no_spurious_reconnected_without_outage():
    r = _run()
    assert r["spuriousRecon"] == 0, "a bare 'reconnected' with no outage must not toast"


def test_neuter_fires_every_time():
    """NEUTER: without the state machine every call toasts — bites the guard."""
    r = _run(neuter=True)
    assert r["lost"] == 3 and r["recon"] == 2 and r["offline"] == 1, r
    assert r["spuriousRecon"] == 1, r


if __name__ == "__main__":
    test_dedupes_concurrent_and_repeat_toasts(); print("PASS dedupe")
    test_no_spurious_reconnected_without_outage(); print("PASS no-spurious")
    test_neuter_fires_every_time(); print("PASS neuter")
    print("ALL GREEN")
