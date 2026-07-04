"""jsdom regression for the per-conversation Project Brain Influence lens.

WHY
---
The global Project Brain shows PROJECT-WIDE state (Charter / Board / Activity).
The Influence lens answers the DIFFERENT, conversation-scoped question the
human asks looking at one chat: "how is THIS conversation affected by the
brain?" — the charter it's bound by, the board epics it OWNS (a live claim),
the epics it must AVOID (a sibling holds the lease), the open ones it could
claim, and the decisions awaiting a human.

The split (mine vs. avoid vs. open) is computed BACKEND-side
(build_conv_influence) reusing the SAME render blocks the prompt injects; the
frontend is a pure renderer of that verdict. This harness loads the REAL
shipped project-brain.js into the REAL index.html #projectBrainInfluence
fragment, stubs Api.project.brainInfluence, opens the panel via the real
openProjectBrain() path, and asserts the lens renders each group correctly and
that a peer-owned epic lands under AVOID (not MINE).

Frontend NEGATIVE CONTROLs (the project's load-bearing-logic bar):
  1. patch a COPY so the mine/avoid classifier always files epics as "mine" →
     the peer-owned epic wrongly appears under MINE → the avoid assertion
     FAILS.
  2. patch a COPY so the empty-hide gate never hides → a no-influence verdict
     leaves the banner visible → the hidden assertion FAILS.
Shipped file asserted byte-identical afterward.

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')
_BRAIN_SRC = os.path.join(JS_DIR, 'project-brain.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


def _extract_panel_fragment() -> str:
    """Pull the REAL #projectBrainOverlay markup (incl. the influence banner)
    out of the shipped index.html — same bounds the sibling test uses."""
    with open(os.path.join(ROOT, 'index.html'), encoding='utf-8') as f:
        html = f.read()
    start = html.find('<div class="project-brain-overlay"')
    assert start != -1, 'project-brain-overlay not found in index.html'
    end = html.find('<div class="chat-container"', start)
    assert end != -1, 'could not bound the overlay fragment'
    return html[start:end].strip()


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const SRC = process.argv[2];
const ROOT = process.argv[3];
const FRAG = process.argv[4];
const fragment = fs.readFileSync(FRAG, 'utf8');
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body>' + fragment + '</body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;

win.Icon = global.Icon = (name) => '<svg data-icon="' + name + '"></svg>';
win.t = global.t = (k) => k;
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);
win.loadConversation = global.loadConversation = () => {};
// This conversation is convA on /proj/real.
win.activeConvId = global.activeConvId = 'convA';
win.getActiveConv = global.getActiveConv = () => ({ id: 'convA', title: 'Parser work', projectPath: '/proj/real' });
win._getConvProjectPath = global._getConvProjectPath = (c) => (c && c.projectPath) || '';
win.pushSubscribe = global.pushSubscribe = () => {};
win.pushUnsubscribe = global.pushUnsubscribe = () => {};

// The backend influence verdict for convA: bound by a charter with one
// decision; owns "Refactor parser"; must AVOID "Rewrite docs" (owned by peer
// convB); "Add tests" is open; one pending proposal raised by convB.
const INFLUENCE = {
  projectPath: '/proj/real', convId: 'convA',
  charter: { exists: true, injected: true, version: 3,
             content: 'NORTH STAR TEXT', decisions: ['Use PostgreSQL'] },
  board: { exists: true, injected: true,
    mine: [{ id: 'pt_a', title: 'Refactor parser', owner: 'convA', dispatched: false, dependsOn: [] }],
    avoid: [{ id: 'pt_b', title: 'Rewrite docs', owner: 'convB', dispatched: false, dependsOn: [] }],
    open: [{ id: 'pt_c', title: 'Add tests', owner: '', dispatched: false, dependsOn: [] }] },
  pendingDecisions: [
    { proposalId: 'prop_1', summary: 'Adopt trunk-based dev', convId: 'convB', title: 'Conv B', ts: 1, mine: false },
  ],
};

win.Api = global.Api = { project: {
  // Feed/charter/board stubs keep openProjectBrain's other columns quiet.
  feed: (p) => Promise.resolve({ maxSeq: 0, events: [] }),
  charter: (p) => Promise.resolve({ exists: true, version: 3, content: 'NORTH STAR TEXT', decisions: [{ text: 'Use PostgreSQL' }] }),
  charterPending: (p) => Promise.resolve({ pending: [] }),
  board: (p) => Promise.resolve({ open: 1, claimed: 2, done: 0, tasks: [] }),
  brainInfluence: (p, cid) => Promise.resolve(INFLUENCE),
} };

eval(fs.readFileSync(SRC, 'utf8'));  // project-brain.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// The shipped influence elements MUST exist (renders-into-null guard).
check('influence_banner_exists', !!win.document.getElementById('projectBrainInfluence'));
check('influence_body_exists', !!win.document.getElementById('projectBrainInfluenceBody'));
check('renderInfluence_exposed', !!(win.ProjectBrain && typeof win.ProjectBrain.renderInfluence === 'function'));

win.openProjectBrain();

// influence loads on a microtask (brainInfluence Promise). Drain a few.
Promise.resolve().then(()=>{}).then(()=>{}).then(()=>{}).then(() => {
  const banner = win.document.getElementById('projectBrainInfluence');
  const body = win.document.getElementById('projectBrainInfluenceBody');
  check('banner_visible', banner && banner.hidden === false);
  const html = body.innerHTML;

  // Charter binding rendered.
  check('charter_northstar', html.indexOf('NORTH STAR TEXT') !== -1);
  check('charter_decision', html.indexOf('Use PostgreSQL') !== -1);

  // MINE group has "Refactor parser" (owned by this conv).
  const mineEl = body.querySelector('.pb-inf-group-mine');
  check('mine_group_present', !!mineEl);
  check('mine_has_refactor', mineEl && mineEl.innerHTML.indexOf('Refactor parser') !== -1);

  // AVOID group has "Rewrite docs" (owned by peer convB) — NOT under mine.
  const avoidEl = body.querySelector('.pb-inf-group-avoid');
  check('avoid_group_present', !!avoidEl);
  check('avoid_has_docs', avoidEl && avoidEl.innerHTML.indexOf('Rewrite docs') !== -1);
  // The decisive split assertion: the peer epic is in AVOID, never in MINE.
  check('docs_not_in_mine', !mineEl || mineEl.innerHTML.indexOf('Rewrite docs') === -1);
  check('avoid_owner_chip_convB', avoidEl && avoidEl.innerHTML.indexOf('convB') !== -1);

  // OPEN group has "Add tests".
  const openEl = body.querySelector('.pb-inf-group-open');
  check('open_has_addtests', openEl && openEl.innerHTML.indexOf('Add tests') !== -1);

  // Pending decision surfaced (awaiting a human).
  check('pending_rendered', html.indexOf('awaiting') !== -1 || body.querySelector('.pb-inf-chip-pending'));

  // Head chips: mine + avoid + charter + pending chips present.
  check('chip_mine', !!body.querySelector('.pb-inf-chip-mine'));
  check('chip_avoid', !!body.querySelector('.pb-inf-chip-avoid'));
  check('chip_charter', !!body.querySelector('.pb-inf-chip-charter'));

  console.log(out.join('\n'));
});
"""


_HARNESS_EMPTY = r"""
const fs = require('fs');
const path = require('path');
const SRC = process.argv[2];
const ROOT = process.argv[3];
const FRAG = process.argv[4];
const fragment = fs.readFileSync(FRAG, 'utf8');
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body>' + fragment + '</body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;

win.Icon = global.Icon = (name) => '<svg data-icon="' + name + '"></svg>';
win.t = global.t = (k) => k;
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);
win.loadConversation = global.loadConversation = () => {};
win.activeConvId = global.activeConvId = 'convSolo';
win.getActiveConv = global.getActiveConv = () => ({ id: 'convSolo', title: 'Solo', projectPath: '/proj/solo' });
win._getConvProjectPath = global._getConvProjectPath = (c) => (c && c.projectPath) || '';
win.pushSubscribe = global.pushSubscribe = () => {};
win.pushUnsubscribe = global.pushUnsubscribe = () => {};

// A no-influence verdict: no charter injected, empty board, no pending.
const EMPTY_INF = {
  projectPath: '/proj/solo', convId: 'convSolo',
  charter: { exists: false, injected: false, version: 0, content: '', decisions: [] },
  board: { exists: false, injected: false, mine: [], avoid: [], open: [] },
  pendingDecisions: [],
};
win.Api = global.Api = { project: {
  feed: (p) => Promise.resolve({ maxSeq: 0, events: [] }),
  charter: (p) => Promise.resolve({ exists: false, version: 0, content: '', decisions: [] }),
  charterPending: (p) => Promise.resolve({ pending: [] }),
  board: (p) => Promise.resolve({ open: 0, claimed: 0, done: 0, tasks: [] }),
  brainInfluence: (p, cid) => Promise.resolve(EMPTY_INF),
} };

eval(fs.readFileSync(SRC, 'utf8'));

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

win.openProjectBrain();
Promise.resolve().then(()=>{}).then(()=>{}).then(()=>{}).then(() => {
  const banner = win.document.getElementById('projectBrainInfluence');
  // A no-influence verdict must HIDE the banner (no visual noise on a solo
  // project) — this is the empty-hide gate under test.
  check('banner_hidden_when_no_influence', banner && banner.hidden === true);
  console.log(out.join('\n'));
});
"""


def _run(harness_src: str, brain_src: str) -> str:
    frag = _extract_panel_fragment()
    frag_file = os.path.join(HERE, '_pb_infl_fragment.html')
    harness = os.path.join(HERE, '_pb_infl_harness.js')
    with open(frag_file, 'w', encoding='utf-8') as f:
        f.write(frag)
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(harness_src)
    try:
        proc = subprocess.run(
            ['node', harness, brain_src, ROOT, frag_file],
            capture_output=True, text=True, timeout=60)
    finally:
        for p in (frag_file, harness):
            try:
                os.remove(p)
            except OSError:
                pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_influence_lens_renders_split_into_real_fragment():
    """The Influence lens populates the REAL #projectBrainInfluenceBody from
    the backend verdict: charter binding + mine/avoid/open groups, with the
    peer-owned epic under AVOID (never MINE)."""
    output = _run(_HARNESS, _BRAIN_SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'influence-lens failures:\n' + output
    for must in ('PASS influence_banner_exists', 'PASS banner_visible',
                 'PASS charter_northstar', 'PASS charter_decision',
                 'PASS mine_has_refactor', 'PASS avoid_has_docs',
                 'PASS docs_not_in_mine', 'PASS avoid_owner_chip_convB',
                 'PASS open_has_addtests', 'PASS chip_mine',
                 'PASS chip_avoid', 'PASS chip_charter'):
        assert must in output, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_influence_hidden_when_no_influence():
    """A no-influence verdict (no charter, empty board, no pending) HIDES the
    banner — a solo/empty project adds no visual noise."""
    output = _run(_HARNESS_EMPTY, _BRAIN_SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'influence-empty failures:\n' + output
    assert 'PASS banner_hidden_when_no_influence' in output, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_mine_avoid_split_is_load_bearing():
    """Frontend NC: neuter the mine/avoid classifier in renderInfluence so
    EVERY claimed epic files under `mine` → the peer-owned "Rewrite docs"
    wrongly appears under MINE → docs_not_in_mine FAILS. Byte-identical
    restore. (Proves the per-conversation ownership split is real logic, not
    incidental grouping.)"""
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        original = f.read()
    # The split lives in build_conv_influence backend-side, but the RENDER
    # trusts board.mine / board.avoid. Simulate a bug where the renderer reads
    # avoid rows into the mine group too.
    anchor = "    var avoid = board.avoid || [];"
    assert anchor in original, 'avoid-var anchor not found'
    patched = original.replace(
        anchor,
        "    var avoid = board.avoid || []; mine = mine.concat(avoid);  // NC leak",
        1)
    copy_path = os.path.join(HERE, '_pb_infl_nc_copy.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run(_HARNESS, copy_path)
        assert 'FAIL docs_not_in_mine' in output, \
            ('NC: filing the peer epic under MINE must make docs_not_in_mine '
             'FAIL:\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain.js must be byte-identical'


# ════════════════════════════════════════════════════════════════════
#  Always-visible per-conversation Influence BAR (docked on the chat view).
#  The lens above lives inside the closed-by-default overlay; the BAR is the
#  always-on entry point on the conversation view itself. It MUST re-pull with
#  the NEW convId on a conversation switch and re-render (or hide). Drives the
#  REAL shipped refreshConvInfluenceBar() against distinct per-conv verdicts.
# ════════════════════════════════════════════════════════════════════

# The bar element sits OUTSIDE the overlay fragment (before .chat-wrapper), so
# the harness mounts it explicitly alongside the overlay fragment.
_HARNESS_BAR = r"""
const fs = require('fs');
const path = require('path');
const SRC = process.argv[2];
const ROOT = process.argv[3];
const FRAG = process.argv[4];
const fragment = fs.readFileSync(FRAG, 'utf8');
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
// The always-visible bar lives on the chat view, not the overlay — mount the
// real element id the shipped index.html uses.
const dom = new JSDOM('<!DOCTYPE html><body>' +
  '<button class="conv-influence-bar" id="convInfluenceBar" hidden></button>' +
  fragment + '</body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;

win.Icon = global.Icon = (name) => '<svg data-icon="' + name + '"></svg>';
win.t = global.t = (k) => k;
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);
win.loadConversation = global.loadConversation = () => {};
win.openProjectBrain = global.openProjectBrain = () => {};
win.pushSubscribe = global.pushSubscribe = () => {};
win.pushUnsubscribe = global.pushUnsubscribe = () => {};

// Start on convA (project /proj/real). getActiveConv/_getConvProjectPath +
// activeConvId are re-pointed between switches below.
win.activeConvId = global.activeConvId = 'convA';
win.getActiveConv = global.getActiveConv = () => ({ id: win.activeConvId, title: win.activeConvId, projectPath: '/proj/real' });
win._getConvProjectPath = global._getConvProjectPath = (c) => (c && c.projectPath) || '';

// Per-conv verdicts: convA owns "Refactor parser" (chip-mine); convB is bound
// only by the charter (chip-charter, NO mine); convSolo has NO influence.
const VERDICTS = {
  convA: { projectPath: '/proj/real', convId: 'convA',
    charter: { exists: false, injected: false, content: '', decisions: [] },
    board: { injected: true,
      mine: [{ id: 'a', title: 'Refactor parser', owner: 'convA' }],
      avoid: [], open: [] }, pendingDecisions: [] },
  convB: { projectPath: '/proj/real', convId: 'convB',
    charter: { exists: true, injected: true, content: 'NS', decisions: ['D1'] },
    board: { injected: true, mine: [], avoid: [], open: [] }, pendingDecisions: [] },
  convSolo: { projectPath: '/proj/real', convId: 'convSolo',
    charter: { exists: false, injected: false, content: '', decisions: [] },
    board: { injected: false, mine: [], avoid: [], open: [] }, pendingDecisions: [] },
};
// The stub echoes the verdict for the convId it was ASKED for — this is what
// proves the re-pull carries the NEW convId (not a stale one).
let _lastAskedConv = null;
win.Api = global.Api = { project: {
  brainInfluence: (p, cid) => { _lastAskedConv = cid; return Promise.resolve(VERDICTS[cid] || VERDICTS.convSolo); },
} };

eval(fs.readFileSync(SRC, 'utf8'));  // project-brain.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const bar = win.document.getElementById('convInfluenceBar');
check('bar_element_exists', !!bar);
check('refresh_exposed', typeof win.convInfluenceRefresh === 'function');

// ── convA: refresh → bar visible with the MINE chip. ──
win.convInfluenceRefresh();
Promise.resolve().then(()=>{}).then(()=>{}).then(() => {
  check('convA_asked', _lastAskedConv === 'convA');
  check('convA_bar_visible', bar.hidden === false);
  check('convA_has_mine_chip', bar.innerHTML.indexOf('pb-inf-chip-mine') !== -1);
  check('convA_no_charter_chip', bar.innerHTML.indexOf('pb-inf-chip-charter') === -1);

  // ── SWITCH to convB: re-point active conv + re-pull. Bar must re-render
  //    with convB's DISTINCT shape (charter chip, NO mine chip). ──
  win.activeConvId = global.activeConvId = 'convB';
  win.convInfluenceRefresh();
  Promise.resolve().then(()=>{}).then(()=>{}).then(() => {
    check('convB_asked_new_id', _lastAskedConv === 'convB');
    check('convB_bar_visible', bar.hidden === false);
    check('convB_has_charter_chip', bar.innerHTML.indexOf('pb-inf-chip-charter') !== -1);
    // Decisive: the bar no longer shows convA's mine chip after the switch.
    check('convB_dropped_mine_chip', bar.innerHTML.indexOf('pb-inf-chip-mine') === -1);

    // ── SWITCH to convSolo (no influence): bar must HIDE. ──
    win.activeConvId = global.activeConvId = 'convSolo';
    win.convInfluenceRefresh();
    Promise.resolve().then(()=>{}).then(()=>{}).then(() => {
      check('solo_asked', _lastAskedConv === 'convSolo');
      check('solo_bar_hidden', bar.hidden === true);
      console.log(out.join('\n'));
    });
  });
});
"""


def _run_bar(brain_src: str) -> str:
    frag = _extract_panel_fragment()
    frag_file = os.path.join(HERE, '_pb_bar_fragment.html')
    harness = os.path.join(HERE, '_pb_bar_harness.js')
    with open(frag_file, 'w', encoding='utf-8') as f:
        f.write(frag)
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS_BAR)
    try:
        proc = subprocess.run(
            ['node', harness, brain_src, ROOT, frag_file],
            capture_output=True, text=True, timeout=60)
    finally:
        for p in (frag_file, harness):
            try:
                os.remove(p)
            except OSError:
                pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_conv_influence_bar_repulls_on_switch_and_hides_when_empty():
    """The always-visible bar re-pulls with the NEW convId on a conversation
    switch and re-renders its distinct shape (convA MINE → convB CHARTER), and
    hides on a no-influence conversation (convSolo)."""
    output = _run_bar(_BRAIN_SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'conv-influence-bar failures:\n' + output
    for must in ('PASS bar_element_exists', 'PASS refresh_exposed',
                 'PASS convA_asked', 'PASS convA_has_mine_chip',
                 'PASS convB_asked_new_id', 'PASS convB_has_charter_chip',
                 'PASS convB_dropped_mine_chip', 'PASS solo_bar_hidden'):
        assert must in output, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_bar_repull_uses_new_convid_is_load_bearing():
    """Frontend NC: pin the bar's convId resolution to a STALE value so a
    switch never re-pulls with the new convId → after switching to convB the
    bar still shows convA's MINE chip → convB_dropped_mine_chip +
    convB_asked_new_id FAIL. Byte-identical restore. (Proves the per-conv
    re-pull is real logic, not incidental.)"""
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = ("    var convId = (typeof activeConvId !== 'undefined' && activeConvId)\n"
              "      ? activeConvId : '';\n"
              "    if (!api || typeof api.brainInfluence !== 'function' || !path || !convId) {")
    assert anchor in original, 'bar-convid anchor not found'
    patched = original.replace(
        anchor,
        ("    var convId = 'convA';  // NC (pinned stale convId)\n"
         "    if (!api || typeof api.brainInfluence !== 'function' || !path || !convId) {"),
        1)
    copy_path = os.path.join(HERE, '_pb_bar_nc_copy.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run_bar(copy_path)
        assert ('FAIL convB_dropped_mine_chip' in output
                or 'FAIL convB_asked_new_id' in output), \
            ('NC: pinning a stale convId must make the switch keep convA\'s '
             'shape (bar not re-pulled for convB):\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain.js must be byte-identical'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_empty_hide_gate_is_load_bearing():
    """Frontend NC: neuter the empty-hide gate so a no-influence verdict never
    hides the banner → banner_hidden_when_no_influence FAILS. Byte-identical
    restore."""
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = "      banner.hidden = true;\n      body.innerHTML = '';\n      if (convEl) convEl.textContent = '';\n      return;"
    assert anchor in original, 'empty-hide-gate anchor not found'
    patched = original.replace(
        anchor,
        "      void 0;  // NC (empty-hide gate disabled)",
        1)
    copy_path = os.path.join(HERE, '_pb_infl_hide_nc_copy.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run(_HARNESS_EMPTY, copy_path)
        assert 'FAIL banner_hidden_when_no_influence' in output, \
            ('NC: disabling the empty-hide gate must leave the banner visible '
             'on a no-influence verdict:\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain.js must be byte-identical'
