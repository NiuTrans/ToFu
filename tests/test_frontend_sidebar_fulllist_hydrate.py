"""Frontend full-list sidebar hydrate — cold-boot paints the ENTIRE sidebar.

Epic ①: the IndexedDB `ConvCache` gained a lightweight full-list mirror
(`putSidebarList` / `getSidebarList`, schema v3) DISTINCT from the locally
observed `conv_meta` store. On a cold boot
`hydrateConversationCatalogFromCache()` paints EVERY conversation the server
last reported (not just the rows observed on this
device), so the sidebar is "打开即在" before the server ?meta=1 round-trip.

Runs the REAL shipped `hydrateConversationCatalogFromCache` body under node with a minimal
global harness (no bundler, no DOM). Proves:
  • the full-list mirror is PREFERRED over getAllMeta (paints all N convs);
  • it FALLS BACK to getAllMeta when the mirror is empty (first run / v2→v3);
  • the mirror's `rev` is adopted as `_serverRev` (CAS base / anti-resurrect);
  • NEUTER: reverting to `return 0` paints nothing (logic is load-bearing).
"""
import json
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests._runtime_sections import runtime_section_path


pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parent.parent
HYDRATE_JS = Path(runtime_section_path("core/conversation_catalog.js"))


def _extract_fn(src: str, name: str) -> str:
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


# The mirror (getSidebarList) reports THREE convs — the full server list — while
# getAllMeta reports only ONE locally observed row. A correct
# hydrate paints all three from the mirror; a fallback would paint only the one.
_HARNESS = r"""
'use strict';
let conversations = [];
function debugLog() {}
let _renderCount = 0;
function renderConversationList() { _renderCount++; }
function _convSorter(a, b) { return (b.updatedAt || 0) - (a.updatedAt || 0); }
function _applySettingsToConv(conv, settings) {
  if (!settings) return;
  if (settings.model) conv.model = settings.model;
}
function _catalogTurnCount(sc) {
  if (!sc) return 0;
  const v = sc.messageCount != null ? sc.messageCount
    : (sc.msgCount != null ? sc.msgCount : sc.msg_count);
  return v || 0;
}
function _startPendingSyncPolling() {}
function _flushPendingSyncs() {}

const ConvCache = {
  isAvailable: () => true,
  // Full-list mirror: the ENTIRE server list (3 convs), each with a rev.
  getSidebarList: async () => (__FULL_LIST__),
  // Locally observed metadata: only ONE row, no rev.
  getAllMeta: async () => ([
    { id: 'c-opened', title: 'Opened', updatedAt: 50, cachedAt: 50, settings: {}, msgCount: 2 },
  ]),
};

__FN__

(async () => {
  const added = await hydrateConversationCatalogFromCache();
  const ids = conversations.map(c => c.id).sort();
  const byId = {};
  conversations.forEach(c => { byId[c.id] = c; });
  console.log(JSON.stringify({
    added,
    count: conversations.length,
    ids,
    renderCount: _renderCount,
    firstIsNewest: conversations[0] && conversations[0].id === 'c-a',  // updatedAt 300
    aRev: byId['c-a'] ? byId['c-a']._serverRev : 'MISSING',
    aNeedsLoad: byId['c-a'] ? byId['c-a']._turnSnapshotRequired === true : 'MISSING',
    aFromCache: byId['c-a'] ? byId['c-a']._fromCache === true : 'MISSING',
    emptyNeedsLoad: byId['c-empty'] ? byId['c-empty']._turnSnapshotRequired : 'MISSING',
    openedRev: byId['c-opened'] ? (byId['c-opened']._serverRev === undefined ? 'UNDEF' : byId['c-opened']._serverRev) : 'MISSING',
  }));
})();
"""

_FULL_LIST = json.dumps([
    {"id": "c-a", "title": "Alpha", "createdAt": 10, "updatedAt": 300, "rev": 7, "msgCount": 5, "settings": {"model": "x"}},
    {"id": "c-b", "title": "Beta", "createdAt": 20, "updatedAt": 200, "rev": 3, "msgCount": 1, "settings": {}},
    {"id": "c-empty", "title": "Empty", "createdAt": 30, "updatedAt": 100, "rev": 1, "msgCount": 0, "settings": {}},
])


def _run(fn_src: str, full_list_js: str = _FULL_LIST) -> dict:
    script = (_HARNESS
              .replace("__FN__", fn_src)
              .replace("__FULL_LIST__", full_list_js))
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, cwd=str(REPO))
    assert out.returncode == 0, f"node failed: {out.stderr}\n---\n{out.stdout}"
    last = [ln for ln in out.stdout.strip().splitlines() if ln.strip().startswith("{")][-1]
    return json.loads(last)


def _hydrate_source_bundle(source: str) -> str:
    return "\n".join(_extract_fn(source, name) for name in (
        "_catalogRevision", "_newCatalogShell",
        "hydrateConversationCatalogFromCache",
    ))


def test_prefers_full_list_mirror():
    """REAL fn: paints ALL THREE convs from the full-list mirror (not the one
    from getAllMeta), sorted newest-first, with rev adopted as _serverRev."""
    src = HYDRATE_JS.read_text()
    fn = _hydrate_source_bundle(src)
    r = _run(fn)
    assert r["added"] == 3, r
    assert r["count"] == 3, r
    assert r["ids"] == ["c-a", "c-b", "c-empty"], r
    assert "c-opened" not in r["ids"], "fell back to getAllMeta instead of the full-list mirror"
    assert r["firstIsNewest"], "sidebar not sorted newest-first via _convSorter"
    # rev from the mirror row is adopted as the CAS base.
    assert r["aRev"] == 7, r
    assert r["aNeedsLoad"] is True, "msgCount>0 → _turnSnapshotRequired shell"
    assert r["aFromCache"] is True, "shell must carry _fromCache marker"
    assert r["emptyNeedsLoad"] is False, "msgCount==0 → NOT _turnSnapshotRequired"


def test_falls_back_to_getallmeta_when_mirror_empty():
    """Empty mirror (first run / v2→v3 upgrade) → fall back to getAllMeta so the
    locally observed fallback is preserved, never a blank sidebar."""
    src = HYDRATE_JS.read_text()
    fn = _hydrate_source_bundle(src)
    r = _run(fn, full_list_js="[]")
    assert r["added"] == 1, r
    assert r["ids"] == ["c-opened"], r
    # getAllMeta rows carry no rev → shell has no _serverRev (undefined).
    assert r["openedRev"] == "UNDEF", r


def test_neuter_paints_nothing():
    """NEUTER: revert body to `return 0` → sidebar paints ZERO. Biting control
    proving the full-list hydrate is load-bearing (the cold-boot blank bug)."""
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


if __name__ == "__main__":
    test_prefers_full_list_mirror()
    print("PASS test_prefers_full_list_mirror")
    test_falls_back_to_getallmeta_when_mirror_empty()
    print("PASS test_falls_back_to_getallmeta_when_mirror_empty")
    test_neuter_paints_nothing()
    print("PASS test_neuter_paints_nothing")
    print("ALL GREEN")
