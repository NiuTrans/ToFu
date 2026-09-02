"""Frontend boot cache-hydration resilience — double-neuter test.

Verifies `hydrateConversationCatalogFromCache()` paints the
sidebar from the IndexedDB `ConvCache` when the server load is unavailable — the
fix for the "blank/Loading… forever on a flaky tunnel" symptom.

Runs the REAL shipped function body under node with a minimal global harness
(no bundler, no DOM), so a regression in the actual source is what the assertion
catches. The biting NEUTER reverts the function to an early `return 0` and proves
the sidebar then paints ZERO conversations from cache.
"""
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
from _runtime_sections import runtime_section_path  # noqa: E402

HYDRATE_JS = Path(runtime_section_path("core/conversation_catalog.js"))


def _extract_fn(src: str, name: str) -> str:
    """Extract a top-level `async function <name>(...) { ... }` by brace matching."""
    m = re.search(r"(?:async\s+)?function %s\s*\(" % re.escape(name), src)
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


_HARNESS = r"""
'use strict';
// ── Minimal globals the real catalog-cache hydrate references ──
let conversations = [];
function debugLog() {}
let _renderCount = 0;
function renderConversationList() { _renderCount++; }
// _convSorter: mirror the real newest-first ordering closely enough for the test.
function _convSorter(a, b) { return (b.updatedAt || 0) - (a.updatedAt || 0); }
// _applySettingsToConv: the real one copies whitelisted settings; here we only
// need it to not throw and to apply a couple of fields.
function _applySettingsToConv(conv, settings) {
  if (!settings) return;
  if (settings.model) conv.model = settings.model;
}
// Shared catalog count normalization.
function _catalogTurnCount(sc) {
  if (!sc) return 0;
  const v = sc.messageCount != null ? sc.messageCount
    : (sc.msgCount != null ? sc.msgCount : sc.msg_count);
  return v || 0;
}
// Pending-sync poller hooks -- only fire when a cached meta carries
// _pendingSyncAt (the test metas do not), so stub them to avoid ReferenceError.
function _startPendingSyncPolling() {}
function _flushPendingSyncs() {}
// Fake ConvCache holding two locally observed catalog rows.
const ConvCache = {
  isAvailable: () => true,
  getAllMeta: async () => ([
    { id: 'c-alpha', title: 'Alpha', updatedAt: 200, cachedAt: 200, settings: { model: 'x' }, msgCount: 3 },
    { id: 'c-beta',  title: 'Beta',  updatedAt: 100, cachedAt: 100, settings: {}, msgCount: 0 },
  ]),
};

__FN__

(async () => {
  const added = await hydrateConversationCatalogFromCache();
  const ids = conversations.map(c => c.id);
  // Emit a machine-parseable result line.
  console.log(JSON.stringify({
    added,
    count: conversations.length,
    ids,
    firstIsNewest: ids[0] === 'c-alpha',          // _convSorter applied
    alphaNeedsLoad: (conversations.find(c => c.id === 'c-alpha') || {})._turnSnapshotRequired === true,
    betaNeedsLoad:  (conversations.find(c => c.id === 'c-beta')  || {})._turnSnapshotRequired === true,
    alphaFromCache: (conversations.find(c => c.id === 'c-alpha') || {})._fromCache === true,
  }));
})();
"""


def _run(fn_src: str) -> dict:
    import json
    script = _HARNESS.replace("__FN__", fn_src)
    out = subprocess.run(
        ["node", "-e", script],
        capture_output=True, text=True, cwd=str(REPO),
    )
    assert out.returncode == 0, f"node failed: {out.stderr}\n---\n{out.stdout}"
    last = [ln for ln in out.stdout.strip().splitlines() if ln.strip().startswith("{")][-1]
    return json.loads(last)


def _hydrate_source_bundle(source: str) -> str:
    return "\n".join(_extract_fn(source, name) for name in (
        "_catalogRevision", "_newCatalogShell",
        "hydrateConversationCatalogFromCache",
    ))


def test_hydrate_paints_cached_convs():
    """REAL function: cache-only convs are painted as _fromCache shells, sorted."""
    src = HYDRATE_JS.read_text()
    fn = _hydrate_source_bundle(src)
    r = _run(fn)
    assert r["added"] == 2, r
    assert r["count"] == 2, r
    assert set(r["ids"]) == {"c-alpha", "c-beta"}, r
    assert r["firstIsNewest"], "sidebar not sorted newest-first via _convSorter"
    assert r["alphaNeedsLoad"], "msgCount>0 conv should be _turnSnapshotRequired shell"
    assert not r["betaNeedsLoad"], "msgCount==0 conv should NOT be _turnSnapshotRequired"
    assert r["alphaFromCache"], "shell must carry _fromCache marker for prune-on-confirm"


def test_neuter_hydrate_paints_nothing():
    """NEUTER: revert the body to `return 0` → sidebar paints ZERO cached convs.

    This is the biting negative control: it proves the cache-hydration logic is
    load-bearing. Without it, a failed boot fetch leaves the sidebar empty (the
    original 'Loading… forever' bug)."""
    neutered = textwrap.dedent(
        """
        async function hydrateConversationCatalogFromCache() {
          return 0;  // NEUTER
        }
        """
    ).strip()
    r = _run(neutered)
    assert r["added"] == 0, r
    assert r["count"] == 0, r
    assert r["ids"] == [], r


MAIN_JS = Path(runtime_section_path("main.js"))


def _extract_plain_fn(src: str, name: str) -> str:
    """Extract a top-level `function <name>(...) {...}` OR `async function ...`."""
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


# ── Reconnect-trigger harness: drives the REAL _bootReconnectWithBackoff /
#    banner fns against a resolved-but-failed load (the swallowed-error tunnel
#    path) and a legitimate-success load. ──
_RECON_HARNESS = r"""
'use strict';
// Fake minimal DOM for the banner helpers.
const _nodes = {};
const document = {
  body: { prepend(n){ _nodes[n.id] = n; }, },
  getElementById(id){ return _nodes[id] || null; },
  createElement(){ return { id:'', style:{cssText:''}, innerHTML:'', remove(){ delete _nodes[this.id]; } }; },
};
const window = {};
const runtimeScope = window;
function debugLog(){}
function renderConversationList(){}

// Controls: how many times loadConversationCatalog is called, and what
// serverLoadOk() returns on each call (index-based).
let _loadCalls = 0;
let _okSequence = __OK_SEQUENCE__;   // e.g. [false,false,true] or [false,false,false,false,false]
async function loadConversationCatalog(){
  _loadCalls++;
  // Simulate the REAL contract: swallows the tunnel error and RESOLVES.
  return undefined;
}
function serverLoadOk(){
  // serverLoadOk reflects the OUTCOME of the most recent load call.
  const idx = Math.min(_loadCalls - 1, _okSequence.length - 1);
  return _okSequence[idx] === true;
}

__FNS__

(async () => {
  if (__CONCURRENT__) {
    // Fire two overlapping calls — the second must be a no-op (idempotent).
    const p1 = _bootReconnectWithBackoff();
    const p2 = _bootReconnectWithBackoff();
    await Promise.all([p1, p2]);
  } else {
    await _bootReconnectWithBackoff();
  }
  console.log(JSON.stringify({
    loadCalls: _loadCalls,
    bannerShownAtLeastOnce: _bannerEverShown,
    bannerShowCount: _bannerShowCount,
    bannerClearedAtEnd: !document.getElementById('boot-reconnect-banner'),
  }));
})();
"""


def _run_recon(ok_sequence, neuter_trigger=False, concurrent=False, neuter_idempotency=False):
    import json
    src = MAIN_JS.read_text()
    # Speed up the backoff delays for the test.
    show = _extract_plain_fn(src, "_showBootReconnectBanner")
    clear = _extract_plain_fn(src, "_clearBootReconnectBanner")
    backoff = _extract_plain_fn(src, "_bootReconnectWithBackoff")
    backoff = re.sub(r"\[2000, 4000, 8000, 15000, 30000\]", "[5, 5, 5, 5, 5]", backoff)
    # Instrument the banner-show to record it was ever shown.
    show = show.replace("document.body.prepend(banner);",
                        "_bannerEverShown = true; _bannerShowCount++; document.body.prepend(banner);", 1)
    if neuter_idempotency:
        # NEUTER: remove the runtime-scope idempotency early exit.
        # early exit so a 2nd concurrent call runs a 2nd loop (double banner).
        before = backoff
        backoff = re.sub(
            r"if \(runtimeScope\._bootReconnectStarted\) \{.*?return;.*?\}",
            "/* guard neutered */",
            backoff, count=1, flags=re.DOTALL,
        )
        assert backoff != before and "if (runtimeScope._bootReconnectStarted)" not in backoff, \
            "neuter did not strip the guard early-return"
    if neuter_trigger:
        # NEUTER: the backoff never actually retries / shows the banner.
        backoff = "async function _bootReconnectWithBackoff() { return; }"
    fns = "let _bannerEverShown=false; let _bannerShowCount=0;\n" + show + "\n" + clear + "\n" + backoff
    script = (_RECON_HARNESS
              .replace("__FNS__", fns)
              .replace("__OK_SEQUENCE__", json.dumps(ok_sequence))
              .replace("__CONCURRENT__", "true" if concurrent else "false"))
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, cwd=str(REPO))
    assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
    last = [ln for ln in out.stdout.strip().splitlines() if ln.strip().startswith("{")][-1]
    return json.loads(last)


def test_reconnect_fires_on_resolved_but_failed_load():
    """The realistic tunnel path: load RESOLVES (error swallowed) but
    serverLoadOk() is false → banner shown + retries until it recovers."""
    # Two failures then a success on the 3rd attempt.
    r = _run_recon([False, False, True])
    assert r["bannerShownAtLeastOnce"], "reconnect banner must appear on a swallowed-error load"
    assert r["loadCalls"] >= 3, r
    assert r["bannerClearedAtEnd"], "banner must clear once serverLoadOk() flips true"


def test_reconnect_recovers_on_304_success():
    """A legitimate 304 (serverLoadOk true on first retry) recovers + clears."""
    r = _run_recon([True])
    assert r["loadCalls"] == 1, r
    assert r["bannerClearedAtEnd"], "304 success must clear the banner"


def test_reconnect_is_idempotent_under_concurrent_calls():
    """Both boot paths (.then !ok AND .catch) can call _bootReconnectWithBackoff.
    Two overlapping invocations must run ONE loop: one banner, not a double
    retry storm. Uses a never-recovers sequence so both would loop if unguarded.

    With [False]*N the loop runs to exhaustion; the guard makes the 2nd call a
    no-op, so the banner is shown exactly once."""
    # The `_showBootReconnectBanner` DOM check already dedupes the banner
    # ELEMENT, so the load-call STORM is what the _bootReconnectStarted guard
    # actually prevents. With the guard, the 2nd concurrent call is a no-op →
    # ONE loop → at most len(_delays)=5 load attempts.
    r = _run_recon([False, False, False, False, False], concurrent=True)
    assert r["loadCalls"] <= 5, f"expected one loop (<=5 load calls), got {r['loadCalls']}: {r}"
    assert r["bannerShowCount"] == 1, f"expected 1 banner, got {r['bannerShowCount']}: {r}"
    # Biting control: strip the guard → BOTH concurrent calls run their own loop
    # → the load-call count roughly doubles (retry storm).
    r2 = _run_recon([False, False, False, False, False], concurrent=True, neuter_idempotency=True)
    assert r2["loadCalls"] > 5, f"neutered guard should double the retry loop, got {r2['loadCalls']}: {r2}"


def test_neuter_trigger_does_not_reconnect():
    """NEUTER the trigger fn → no retries, banner never shown. Biting control:
    proves the reconnect DECISION (not just hydration) is load-bearing."""
    r = _run_recon([False, False, False], neuter_trigger=True)
    assert r["loadCalls"] == 0, r
    assert not r["bannerShownAtLeastOnce"], r


def test_all_symbols_land_in_served_bundle_together():
    """Bundle-integration ratchet: the harness tests eval SOURCE files directly,
    never the served bundle — so a _BUNDLE_FILES reorder/drop or a minifier
    regression could ship a broken artifact while every harness stays green
    (the §3.2.1 'file exists but silently absent from bundle' class).

    This closes the loop: build the real bundle and assert all four symbols from
    the three edited files survived into the SAME served bundle-*.js."""
    import glob
    import sys
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    try:
        from lib.js_bundler import build_bundle
    except Exception as e:  # bundler import failure shouldn't hard-fail the file
        import pytest
        pytest.skip(f"js_bundler unavailable: {e}")
    built_name = build_bundle()
    assert built_name, "build_bundle() did not produce a core bundle"
    # Read the artifact returned by THIS build. Another xdist worker/process
    # may publish a different valid content hash immediately afterwards, so
    # choosing the globally-newest mtime can inspect somebody else's fixture.
    bundle = str(REPO / "static" / "js" / built_name)
    assert Path(bundle).is_file(), f"built bundle disappeared: {built_name}"
    text = Path(bundle).read_text()
    required = [
        "getAllMeta",              # idb-cache.js — the list primitive
        "hydrateConversationCatalogFromCache", # metadata-only cache-first paint
        "serverLoadOk",            # conversations.js — observable-outcome flag
        "_bootReconnectStarted",   # main.js — idempotency latch
    ]
    missing = [s for s in required if s not in text]
    assert not missing, (
        f"symbols missing from served bundle {Path(bundle).name}: {missing} "
        f"— a _BUNDLE_FILES reorder/drop or minifier regression dropped one of "
        f"the three edited files. See CLAUDE.md §3.2.1."
    )
    print(f"PASS bundle-integration ({Path(bundle).name}: all 4 symbols present)")


if __name__ == "__main__":
    test_hydrate_paints_cached_convs()
    print("PASS test_hydrate_paints_cached_convs")
    test_neuter_hydrate_paints_nothing()
    print("PASS test_neuter_hydrate_paints_nothing")
    test_reconnect_fires_on_resolved_but_failed_load()
    print("PASS test_reconnect_fires_on_resolved_but_failed_load")
    test_reconnect_recovers_on_304_success()
    print("PASS test_reconnect_recovers_on_304_success")
    test_reconnect_is_idempotent_under_concurrent_calls()
    print("PASS test_reconnect_is_idempotent_under_concurrent_calls")
    test_neuter_trigger_does_not_reconnect()
    print("PASS test_neuter_trigger_does_not_reconnect")
    test_all_symbols_land_in_served_bundle_together()
    print("ALL GREEN")
