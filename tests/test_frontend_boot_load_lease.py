"""Frontend reconcile-wedge resilience — the stranded in-flight latch.

Reproduces the reported "always have to refresh to see backend state" bug in
the target environment (Tofu Android WebView behind a VS Code port-forward
tunnel, where the ``wss://`` push upgrade is unreliable so the POLL tier is the
only safety net) and proves the root fix.

## The wedge (root cause)
Every list-reload path — ``_crossDeviceReconcile``, ``_scheduleConvListRefresh``
(notify), and main.js's ``_bootReconnectWithBackoff`` — shares ONE latch,
``window._bootLoadInFlight``, so at most one load runs at a time. It used to be
a bare boolean cleared ONLY in a ``.finally()``. If a load never settles (a hung
tunnel fetch that neither resolves nor rejects), the ``.finally()`` never runs
and the latch is stuck truthy FOREVER → every future reconcile + boot reconnect
early-returns → the user must refresh. That is the whole-session freeze.

## The fix (two layers, both asserted here)
1. ``loadConversationsFromServer``'s ``?meta=1`` fetch is now BOUNDED with an
   ``AbortSignal`` timeout, so the known hang becomes an AbortError → the outer
   try/catch resolves → the latch releases. (Asserted by source inspection —
   the raw ``fetch`` in that fn must carry a ``signal``.)
2. ``_bootLoadInFlight`` is now a SELF-HEALING LEASE: it stores the acquire
   timestamp and a lease older than ``_BOOT_LOAD_LEASE_MS`` is reclaimable, so
   ANY future stranding cause (not just this fetch) can't wedge reconcile
   forever. (Asserted by driving the REAL extracted helpers under node.)

Runs the REAL shipped helper bodies under a minimal node harness (no bundler,
no DOM), mirroring tests/test_frontend_cache_hydrate_boot.py, so a regression in
the actual source is what the assertion catches. The biting NEUTER reverts the
lease to the old bare-boolean latch and proves reconcile then wedges forever.
"""
import json
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SYNC_JS = REPO / "static" / "js" / "core" / "cross_tab_sync.js"
CONV_JS = REPO / "static" / "js" / "core" / "conversations.js"


def _extract_fn(src: str, name: str) -> str:
    """Extract a top-level `function <name>(...) { ... }` by brace matching."""
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


def _extract_const(src: str, name: str) -> str:
    """Extract a top-level `const <name> = <literal>;` line."""
    m = re.search(r"const %s\s*=\s*([^;]+);" % re.escape(name), src)
    assert m, f"const {name} not found"
    return m.group(0)


# ── Harness: the REAL lease helpers + a fake `window` whose clock we control ──
_LEASE_HARNESS = r"""
'use strict';
function debugLog(){}
// Controllable clock so we can age the lease past the cap without real waiting.
let _now = 1000000;
const _realDateNow = Date.now;
Date.now = () => _now;
const window = {};

__CONST__
__BOOT_HELD__
__ACQUIRE__
__RELEASE__

// Wire the same window exposure the source does at load.
window._bootLoadInFlight = 0;

(async () => {
  const out = {};

  // 1. Fresh acquire succeeds; a second concurrent acquire is refused.
  out.firstAcquire = _acquireBootLoad();       // true
  out.secondAcquireWhileHeld = _acquireBootLoad(); // false (fresh lease held)

  // 2. THE WEDGE: the holder never releases (hung fetch). Without self-heal a
  //    reconcile would be blocked forever. Advance the clock PAST the cap and
  //    prove a new caller can reclaim.
  _now += (_BOOT_LOAD_LEASE_MS + 1000);
  out.inFlightAfterCap = _bootLoadHeld();   // false → reclaimable
  out.reacquireAfterCap = _acquireBootLoad();   // true → wedge self-healed

  // 3. A HEALTHY load that releases frees the latch immediately (no waiting).
  _releaseBootLoad();
  out.inFlightAfterRelease = _bootLoadHeld(); // false

  // 4. Just-acquired lease is NOT prematurely reclaimable (never steals a live load).
  _acquireBootLoad();
  _now += 5000;  // 5s < cap
  out.inFlightWellWithinCap = _bootLoadHeld(); // true (still held)

  console.log(JSON.stringify(out));
})();
"""


def _run_lease(neuter=False):
    src = SYNC_JS.read_text()
    cap = _extract_const(src, "_BOOT_LOAD_LEASE_MS")
    if neuter:
        # NEUTER: revert to the OLD bare-boolean latch semantics — no stale
        # reclaim. Once held it stays held until an explicit release, so a
        # stranded (never-released) load wedges forever.
        boot = "function _bootLoadHeld(){ return !!window._bootLoadInFlight; }"
        acquire = ("function _acquireBootLoad(){ if(window._bootLoadInFlight) return false;"
                   " window._bootLoadInFlight = Date.now(); return true; }")
        release = "function _releaseBootLoad(){ window._bootLoadInFlight = 0; }"
    else:
        boot = _extract_fn(src, "_bootLoadHeld")
        acquire = _extract_fn(src, "_acquireBootLoad")
        release = _extract_fn(src, "_releaseBootLoad")
    script = (_LEASE_HARNESS
              .replace("__CONST__", cap)
              .replace("__BOOT_HELD__", boot)
              .replace("__ACQUIRE__", acquire)
              .replace("__RELEASE__", release))
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, cwd=str(REPO))
    assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
    last = [ln for ln in out.stdout.strip().splitlines() if ln.strip().startswith("{")][-1]
    return json.loads(last)


def test_lease_self_heals_a_stranded_load():
    """REAL helpers: a load that never releases (hung tunnel fetch) is reclaimed
    once its lease ages past the cap — reconcile is NOT wedged forever."""
    r = _run_lease()
    assert r["firstAcquire"] is True, r
    assert r["secondAcquireWhileHeld"] is False, "a fresh held lease must block a second load"
    # The bite: after the cap the stranded lease is reclaimable.
    assert r["inFlightAfterCap"] is False, "stale lease past cap must report NOT-in-flight (self-heal)"
    assert r["reacquireAfterCap"] is True, "a new caller must be able to reclaim a wedged lease"
    # Sanity: normal release + never-steal-a-live-load.
    assert r["inFlightAfterRelease"] is False, r
    assert r["inFlightWellWithinCap"] is True, "a load well within the cap must NOT be reclaimable"


def test_neuter_bare_boolean_latch_wedges_forever():
    """NEUTER (biting control): revert to the OLD bare-boolean latch → a
    stranded load is NEVER reclaimable, so `_bootLoadInFlight` stays true past
    the cap and a new caller can't acquire → the whole-session freeze."""
    r = _run_lease(neuter=True)
    assert r["firstAcquire"] is True, r
    assert r["secondAcquireWhileHeld"] is False, r
    # With the old semantics the stranded latch is STILL held after the cap —
    # this is exactly the permanent wedge the fix removes.
    assert r["inFlightAfterCap"] is True, "old bare-boolean latch must stay wedged past the cap"
    assert r["reacquireAfterCap"] is False, "old latch must REFUSE reclaim → reconcile frozen forever"


def test_meta_fetch_is_bounded():
    """The `?meta=1` sidebar fetch in loadConversationsFromServer must be
    bounded with an AbortSignal, so a hung tunnel fetch becomes an AbortError
    (→ try/catch resolves → latch releases) instead of hanging forever.

    Source-level assertion: the raw `fetch(url, ...)` inside the fn must pass a
    `signal`. This is the layer-1 fix that prevents the KNOWN hang; the lease is
    the layer-2 backstop for any other stranding cause."""
    src = CONV_JS.read_text()
    fn = _extract_fn(src, "loadConversationsFromServer")
    # The meta fetch must carry a signal (bounded). The raw `fetch(url, …)` was
    # refactored behind the unified Api client — the ?meta=1 sidebar load now
    # goes through `Api.conversations.listMeta({ …, signal: _mkTimeoutSignal(…) })`
    # (per the §3.2.0 no-raw-fetch rule). The bounded-fetch INTENT is unchanged;
    # match the current call shape (a `signal` threaded into listMeta).
    assert re.search(r"listMeta\(\s*\{[^}]*signal", fn), \
        "the ?meta=1 listMeta call must be bounded with an AbortSignal (signal: ...)"
    assert "AbortSignal.timeout" in fn or "AbortController" in fn, \
        "a timeout signal source must be present in loadConversationsFromServer"


if __name__ == "__main__":
    test_lease_self_heals_a_stranded_load()
    print("PASS test_lease_self_heals_a_stranded_load")
    test_neuter_bare_boolean_latch_wedges_forever()
    print("PASS test_neuter_bare_boolean_latch_wedges_forever")
    test_meta_fetch_is_bounded()
    print("PASS test_meta_fetch_is_bounded")
    print("ALL GREEN")
