"""tests/test_frontend_rail_cls.py — Zero-CLS first-paint guard for the rail.

WHY
---
The project rail widens the sidebar (280px → calc(rail + list-min)). If that
width is applied only by the async `_renderFolderTabsInner` AFTER
`loadFolders()` resolves, every ≥1-folder user sees the sidebar jump wider on
each load — a visible layout shift (CLS) that also animates via
`transition:width` and pushes the chat column sideways.

The fix (mirroring the theme no-FOUC script): an inline `<head>` script in
index.html reads a persisted `tofu_has_folders` hint and stamps
`html[data-rail="full"|"collapsed"]` BEFORE first paint; CSS keyed on that
attribute gives the sidebar its correct steady-state width immediately, so the
folders round-trip reconciles CONTENT only, never GEOMETRY. The render path
(`_persistRailHint`) keeps the hint + attribute in sync.

This harness asserts:
  1. PRE-PAINT — running ONLY the extracted index.html inline hint script (no
     app JS, no render) with the hint set stamps html[data-rail], and the
     order is right: the attribute is present with `#folderTabs` still EMPTY
     (i.e. before renderFolderTabs could ever run).
  2. NO-HINT — with the hint absent, no data-rail is stamped (new/zero-folder
     users paint single-column, unchanged).
  3. CSS — styles.css carries an `html[data-rail=...] .sidebar` width rule whose
     computed width equals the `.sidebar.has-rail` rule (byte-identical → the
     later class add causes NO width change → transition never fires).
  4. RUNTIME SYNC — `_persistRailHint(true/false)` from the real
     conversation_list.js writes/removes the localStorage hint AND toggles
     html[data-rail].

NEUTER: strip the `setAttribute('data-rail', …)` call from the extracted inline
script → assertion (1) flips to FAIL, proving the pre-paint stamp is
load-bearing (without it the width is wrong until the async render).

Skips cleanly without node/jsdom.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest
from tests._runtime_sections import orchestration_legacy_test_root as _legacy_test_root

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = _legacy_test_root()
JS_DIR = os.path.join(ROOT, 'static', 'js')
INDEX_HTML = os.path.join(ROOT, 'index.html')
STYLES_CSS = os.path.join(ROOT, 'static', 'styles.css')
_CONV = os.path.join(JS_DIR, 'ui', 'conversation_list.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


def _extract_prepaint_script() -> str:
    """Pull the inline `data-rail` hint script out of index.html.

    It is the IIFE that reads `tofu_has_folders` and setAttribute('data-rail').
    We slice the smallest self-contained `(function(){...})();` that mentions
    the hint key so the test runs the SHIPPED code, not a copy.
    """
    with open(INDEX_HTML, encoding='utf-8') as f:
        html = f.read()
    # Find the rail IIFE specifically (`(function(){try{`) — NOT the theme
    # IIFE just above it (which uses bare `localStorage`). Anchor on the hint
    # key, walk back to the nearest rail-IIFE opener, forward to its close.
    idx = html.index("getItem('tofu_has_folders')")  # the CODE, not the comment
    start = html.rindex('(function(){', 0, idx)
    end = html.index('})();', idx) + len('})();')
    script = html[start:end]
    assert 'tofu_has_folders' in script and 'claude_ui_theme' not in script, \
        'extracted the wrong inline IIFE'
    return script


# ── CSS assertion (no node needed) ──────────────────────────────────────────

def test_prepaint_css_width_rule_exists_and_matches_has_rail():
    with open(STYLES_CSS, encoding='utf-8') as f:
        css = f.read()

    def _width_of(selector):
        # Match `<selector>{...width:<val>...}` — the rules are single-line.
        m = re.search(re.escape(selector) + r'\s*\{([^}]*)\}', css)
        assert m, f'selector not found: {selector}'
        w = re.search(r'(?<![-\w])width:([^;]+)', m.group(1))
        assert w, f'no width in rule: {selector}'
        return w.group(1).strip()

    full_prepaint = _width_of('html[data-rail="full"] .sidebar')
    full_hasrail = _width_of('.sidebar.has-rail')
    collapsed_prepaint = _width_of('html[data-rail="collapsed"] .sidebar')
    collapsed_hasrail = _width_of('.sidebar.has-rail.rail-collapsed')

    # Byte-identical widths → adding .has-rail later changes nothing → no shift.
    assert full_prepaint == full_hasrail, (full_prepaint, full_hasrail)
    assert collapsed_prepaint == collapsed_hasrail, (collapsed_prepaint, collapsed_hasrail)


# ── jsdom: pre-paint stamp order + no-hint + neuter + runtime sync ───────────

_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const CONV = process.argv[4];
const PREPAINT = fs.readFileSync(process.argv[2], 'utf8');  // extracted inline script (maybe neutered)
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ══ Scenario 1: PRE-PAINT with hint set → data-rail stamped, rail still empty ══
{
  const dom = new JSDOM('<!DOCTYPE html><html><body><aside class="sidebar" id="sidebar"><nav class="project-rail" id="folderTabs"></nav></aside></body></html>', { url: 'http://localhost/' });
  const { window } = dom;
  // The pre-paint IIFE uses BARE `localStorage`/`document` (as it does in the
  // real browser). window.eval in this jsdom/node doesn't expose those as bare
  // globals, so bind node globals + indirect-eval (matches how the browser
  // resolves them against the window). A swallowed ReferenceError would make
  // the stamp silently no-op, so this binding is what makes the test real.
  global.window = window; global.document = window.document; global.localStorage = window.localStorage;
  window.localStorage.setItem('tofu_has_folders', '1');
  (0, eval)(PREPAINT);   // run ONLY the extracted inline hint script — no app JS, no render
  const attr = window.document.documentElement.getAttribute('data-rail');
  check('prepaint_stamps_full', attr === 'full');
  // Order proof: the rail container is STILL empty — the width is settled
  // before renderFolderTabs could have run.
  check('prepaint_before_render', window.document.getElementById('folderTabs').children.length === 0);

  // collapsed hint variant
  window.localStorage.setItem('tofu_project_rail_collapsed', '1');
  window.document.documentElement.removeAttribute('data-rail');
  (0, eval)(PREPAINT);
  check('prepaint_stamps_collapsed', window.document.documentElement.getAttribute('data-rail') === 'collapsed');
}

// ══ Scenario 2: NO hint → no data-rail (single-column, unchanged) ══
{
  const dom = new JSDOM('<!DOCTYPE html><html><body></body></html>', { url: 'http://localhost/' });
  const { window } = dom;
  global.window = window; global.document = window.document; global.localStorage = window.localStorage;
  // hint absent (fresh dom → empty localStorage)
  (0, eval)(PREPAINT);
  check('nohint_no_stamp', !window.document.documentElement.hasAttribute('data-rail'));
}

// ══ Scenario 3: RUNTIME SYNC — _persistRailHint from the REAL source ══
{
  const dom = new JSDOM('<!DOCTYPE html><html><body></body></html>', { url: 'http://localhost/' });
  const { window } = dom;
  global.window = window; global.document = window.document; global.localStorage = window.localStorage;
  // _persistRailHint reads _readRailCollapsed which reads localStorage — fine.
  // Eval only the helper region by evaluating the whole file with harmless stubs.
  global.t = (k)=>k; global.escapeHtml = (s)=>String(s==null?'':s);
  global.getFolders = ()=>[]; global.getActiveFolderId = ()=>null; global.areFoldersLoaded = ()=>true;
  global.setActiveFolderId = ()=>{}; global.getFolderById = ()=>null;
  global.formatRelativeTime = ()=>''; global.highlightMatch = (s)=>s; global.sidebarSearchQuery='';
  global.IntersectionObserver = window.IntersectionObserver = class { observe(){} disconnect(){} unobserve(){} };
  global.setTimeout = window.setTimeout = (fn)=>0;
  global.requestAnimationFrame = window.requestAnimationFrame = (fn)=>0;
  (0, eval)(fs.readFileSync(CONV, 'utf8'));
  _persistRailHint(true);
  check('sync_true_sets_key', window.localStorage.getItem('tofu_has_folders') === '1');
  check('sync_true_sets_attr', window.document.documentElement.getAttribute('data-rail') === 'full');
  _persistRailHint(false);
  check('sync_false_clears_key', window.localStorage.getItem('tofu_has_folders') === null);
  check('sync_false_clears_attr', !window.document.documentElement.hasAttribute('data-rail'));
}

console.log(out.join('\n'));
"""


def _run(prepaint_script: str):
    """Write the (possibly neutered) extracted inline script + harness, run node."""
    pp_path = os.path.join(HERE, '_rail_cls_prepaint.js')
    harness = os.path.join(HERE, '_rail_cls_harness.js')
    with open(pp_path, 'w', encoding='utf-8') as f:
        f.write(prepaint_script)
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, pp_path, ROOT, _CONV],
            capture_output=True, text=True, timeout=60)
    finally:
        for p in (pp_path, harness):
            try:
                os.remove(p)
            except OSError:
                pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


_EXPECTED = (
    'prepaint_stamps_full', 'prepaint_before_render', 'prepaint_stamps_collapsed',
    'nohint_no_stamp',
    'sync_true_sets_key', 'sync_true_sets_attr',
    'sync_false_clears_key', 'sync_false_clears_attr',
)


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_rail_cls_prepaint_and_sync():
    output = _run(_extract_prepaint_script())
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'rail-CLS failures:\n' + output
    for must in _EXPECTED:
        assert ('PASS ' + must) in output, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_prepaint_stamp_is_load_bearing():
    """Neuter the pre-paint stamp: strip the setAttribute('data-rail', …) call
    from the extracted inline script → the pre-paint stamp checks FAIL (the
    width would be wrong until the async render), while the no-hint control
    still PASSes."""
    script = _extract_prepaint_script()
    neutered = re.sub(r"document\.documentElement\.setAttribute\('data-rail'[^;]*\);", '', script)
    assert neutered != script, 'NC replacement was a no-op'
    output = _run(neutered)
    assert ('FAIL prepaint_stamps_full') in output, output
    assert ('FAIL prepaint_stamps_collapsed') in output, output
    # Surgical: absent-hint behavior and runtime sync (independent code) still pass.
    assert ('PASS nohint_no_stamp') in output, output
    assert ('PASS sync_true_sets_attr') in output, output


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
