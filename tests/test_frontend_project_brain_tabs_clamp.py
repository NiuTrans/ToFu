"""jsdom regression for the Project Brain panel UX overhaul.

WHY
The panel used a 4-column grid (`grid-template-columns:1fr 1fr 1.15fr 1fr`) with
no min-width floor, so on a narrow / port-forwarded viewport each column
collapsed to an unreadable sliver (one character per line). And a committed
charter decision can be 2000+ chars, rendered in full → one wall of text made
the whole surface unusable. Two fixes, both load-bearing:

  • TABS: the four surfaces (Charter / Board / Activity / Team) are shown
    ONE-AT-A-TIME. `_selectTab(name)` marks exactly one `.pb-tab` active and
    shows exactly one `.pb-tab-panel` (`.pb-tab-panel-active`), giving it the
    full width.
  • CLAMP: long text (> threshold) is wrapped in a collapsed `.pb-clamp` with a
    Show more/less toggle; clicking toggles `.pb-clamp-open`. Short text is NOT
    wrapped (no needless chrome).

Loads the REAL shipped project-brain.js under jsdom over the REAL index.html
tab + panel fragment. NEGATIVE CONTROL: neuter `_selectTab`'s panel-hiding in a
COPY of the source and assert two panels are visible at once → proving the
one-at-a-time switch is load-bearing. Shipped file byte-identical after.

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import json
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
_BRAIN_SRC = os.path.join(JS_DIR, 'project-brain.js')


def _node_deps_available():
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


# A realistic slice of the shipped index.html: the tab bar + the four panels,
# with a long charter decision so the clamp path is exercised.
_LONG = 'X' * 900
_DOM = r'''<!DOCTYPE html><body>
<div class="project-brain-tabs" id="projectBrainTabs" role="tablist">
  <button type="button" class="pb-tab pb-tab-active" data-pb-tab="charter" role="tab"><span>Charter</span><span class="pb-tab-count" id="pbTabCountCharter" hidden></span></button>
  <button type="button" class="pb-tab" data-pb-tab="board" role="tab"><span>Board</span><span class="pb-tab-count" id="pbTabCountBoard" hidden></span></button>
  <button type="button" class="pb-tab" data-pb-tab="activity" role="tab"><span>Activity</span></button>
  <button type="button" class="pb-tab" data-pb-tab="peers" role="tab"><span>Team</span><span class="pb-tab-count" id="pbTabCountPeers" hidden></span></button>
</div>
<div class="project-brain-columns">
  <div class="project-brain-col pb-tab-panel pb-tab-panel-active" data-pb-panel="charter"><div class="project-brain-col-body" id="projectBrainCharterBody"></div></div>
  <div class="project-brain-col pb-tab-panel" data-pb-panel="board"><div class="project-brain-col-body" id="projectBrainBoardBody"></div></div>
  <div class="project-brain-col pb-tab-panel" data-pb-panel="activity"><div class="project-brain-col-body"><div id="projectBrainActivityList"></div></div></div>
  <div class="project-brain-col pb-tab-panel" data-pb-panel="peers"><div class="project-brain-col-body" id="projectBrainPeersBody"></div></div>
</div>
</body>'''


def _harness(dom_js):
    return r'''
const fs = require('fs');
const path = require('path');
const SRC = process.argv[1];
const ROOT = process.argv[2];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(DOM_PLACEHOLDER, { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;
win.t = global.t = (k) => k;
win.Icon = global.Icon = () => '<svg></svg>';
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);
win.Api = global.Api = { project: {} };   // no network in this test
win.activeConvId = global.activeConvId = '';

eval(fs.readFileSync(SRC, 'utf8'));
const PB = win.ProjectBrain;
const out = {};

// ── Render a charter with a LONG decision + a SHORT one → clamp behaviour. ──
PB._renderCharterForTest
  ? PB._renderCharterForTest()
  : PB.renderCharter({ version: 1, content: '', decisions: [
      { text: 'LONGTEXT_PLACEHOLDER' }, { text: 'short one' }] }, []);

const body = win.document.getElementById('projectBrainCharterBody');
const clamps = body.querySelectorAll('.pb-clamp');
const toggles = body.querySelectorAll('.pb-clamp-toggle');
out.clampCount = clamps.length;            // exactly 1 (only the long decision)
out.toggleCount = toggles.length;

// Toggle expands.
let openBefore = clamps.length ? clamps[0].classList.contains('pb-clamp-open') : null;
if (toggles.length) toggles[0].dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
let openAfter = clamps.length ? clamps[0].classList.contains('pb-clamp-open') : null;
out.openBefore = openBefore;
out.openAfter = openAfter;

// ── Tabs: switch to board → exactly one panel visible. ──
PB._selectTabForTest ? PB._selectTabForTest('board') : (win.ProjectBrain._selectTab && win.ProjectBrain._selectTab('board'));
function visiblePanels() {
  return Array.prototype.filter.call(
    win.document.querySelectorAll('.pb-tab-panel'),
    (p) => p.classList.contains('pb-tab-panel-active')).map((p) => p.getAttribute('data-pb-panel'));
}
out.afterBoardSwitch = visiblePanels();
const activeTabs = Array.prototype.filter.call(
  win.document.querySelectorAll('.pb-tab'),
  (b) => b.classList.contains('pb-tab-active')).map((b) => b.getAttribute('data-pb-tab'));
out.activeTabs = activeTabs;

// Click the peers tab via the wired bar.
PB._initTabsForTest && PB._initTabsForTest();
const peersTab = win.document.querySelector('.pb-tab[data-pb-tab="peers"]');
peersTab.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
out.afterPeersClick = visiblePanels();

console.log('__RESULT__' + JSON.stringify(out));
'''.replace('DOM_PLACEHOLDER', json.dumps(dom_js)).replace('LONGTEXT_PLACEHOLDER', _LONG)


def _run(src, dom_js):
    proc = subprocess.run(
        ['node', '-e', _harness(dom_js), src, ROOT],
        capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(f'harness failed: {proc.stderr or proc.stdout}')
    for line in proc.stdout.splitlines():
        if line.startswith('__RESULT__'):
            return json.loads(line[len('__RESULT__'):])
    raise AssertionError(f'no result line: {proc.stdout}\n{proc.stderr}')


def _ensure_test_hooks(src_text):
    """The module exposes window.ProjectBrain; ensure the fns we drive are on
    it. If the shipped surface doesn't expose _selectTab/_initTabs/renderCharter
    we add thin *ForTest shims by patching the window export — but only in a
    COPY, never the shipped file."""
    return src_text


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_tabs_and_clamp(tmp_path):
    # The shipped module must expose the driving fns on window.ProjectBrain.
    # Verify the exports exist; if the public object uses different names the
    # test fails loudly (rather than silently passing).
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        txt = f.read()
    assert 'window.ProjectBrain' in txt
    # Add test hooks in a COPY: expose the internal fns under *ForTest.
    hook = (
        "\n;window.ProjectBrain._selectTabForTest = function(n){return _selectTab(n);};"
        "\nwindow.ProjectBrain._initTabsForTest = function(){return _initTabs();};"
        "\nwindow.ProjectBrain._renderCharterForTest = function(){return renderCharter("
        "{version:1,content:'',decisions:[{text:'" + _LONG + "'},{text:'short one'}]},[]);};"
    )
    patched = txt.replace('})();', hook + '\n})();', 1) if txt.rstrip().endswith('})();') \
        else txt + hook
    # Insert the hooks just before the IIFE close so they run inside the closure.
    m = re.search(r'\n\}\)\(\);\s*$', txt)
    assert m, 'could not find IIFE close in project-brain.js'
    patched = txt[:m.start()] + hook + txt[m.start():]
    src = os.path.join(tmp_path, 'project-brain-hooked.js')
    with open(src, 'w', encoding='utf-8') as f:
        f.write(patched)

    out = _run(src, _DOM)
    # Clamp: exactly ONE clamp (the 900-char decision), the short one is plain.
    assert out['clampCount'] == 1, out
    assert out['toggleCount'] == 1, out
    assert out['openBefore'] is False and out['openAfter'] is True, \
        f'the toggle must expand the clamp: {out}'
    # Tabs: after switching to board, exactly the board panel is visible.
    assert out['afterBoardSwitch'] == ['board'], out
    assert out['activeTabs'] == ['board'], out
    # Clicking the wired peers tab shows exactly the peers panel.
    assert out['afterPeersClick'] == ['peers'], out


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_select_tab_hiding_is_load_bearing(tmp_path):
    """NC: neuter the panel-hiding half of _selectTab (make it a no-op on the
    panels) → switching tabs no longer hides the previously-active panel, so
    TWO panels are visible at once. Proves the one-at-a-time switch is what
    keeps the layout single-view. Shipped file byte-identical."""
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        original = f.read()
    # The panel loop toggles 'pb-tab-panel-active' by matching data-pb-panel.
    anchor = ("      panels[j].classList.toggle('pb-tab-panel-active',\n"
              "        panels[j].getAttribute('data-pb-panel') === name);")
    assert anchor in original, 'panel-toggle anchor not found'
    # Neuter: always add active (never remove) → old panel stays visible.
    patched = original.replace(
        anchor,
        "      panels[j].classList.add('pb-tab-panel-active');  // NC: never hide",
        1)
    # add the same test hooks so we can drive _selectTab.
    m = re.search(r'\n\}\)\(\);\s*$', patched)
    hook = ("\n;window.ProjectBrain._selectTabForTest = function(n){return _selectTab(n);};"
            "\nwindow.ProjectBrain._initTabsForTest = function(){return _initTabs();};"
            "\nwindow.ProjectBrain._renderCharterForTest = function(){return renderCharter("
            "{version:1,content:'',decisions:[{text:'z'}]},[]);};")
    patched = patched[:m.start()] + hook + patched[m.start():]
    src = os.path.join(tmp_path, 'project-brain-nc.js')
    with open(src, 'w', encoding='utf-8') as f:
        f.write(patched)

    out = _run(src, _DOM)
    # With hiding neutered, switching to board leaves charter ALSO visible.
    assert set(out['afterBoardSwitch']) >= {'charter', 'board'}, \
        f'NC: without panel-hiding multiple panels stay visible: {out}'

    with open(_BRAIN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain.js must be byte-identical'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
