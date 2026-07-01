"""jsdom regression for the Debug panel's Project-Brain injection badge
(static/js/core/debug_panel.js).

WHY
---
"How do I observe whether the project brain actually reached the model this
task?" The AUTHORITATIVE answer is the exact marker string the model saw in
the wire-form ``messages`` snapshot the debug panel renders: ``[PROJECT
CHARTER]`` / ``[PROJECT BOARD]``. ``showMessagesInDebug`` sniffs ONLY those
markers (no separate frontend heuristic, no state reverse-engineering) and:

  • adds a per-message ``.debug-brain-badge`` (+ ``.debug-msg-brain`` class) to
    the SYSTEM message that carries them, naming which blocks (charter/board);
  • adds a ``🧠``-glyph (SVG, §3.4) summary counter to the panel header.

This harness loads the REAL shipped ``debug_panel.js`` under jsdom via indirect
eval (the file attaches nothing to ``window`` — function declarations leak to
global scope in sloppy-mode indirect eval), feeds it a wire-form snapshot whose
system message content is an ARRAY of text blocks (as the real assembler emits)
carrying both markers, and asserts the badge + header counter appear — and that
a system message WITHOUT the markers gets NO badge (proving the marker string,
not the role, is the judge).

Frontend NEGATIVE CONTROL: patch a COPY of debug_panel.js so ``_debugBrainInfo``
always returns null → the badge + header counter vanish → the assertions FAIL.
Shipped file asserted byte-identical afterwards.

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
_DEBUG_SRC = os.path.join(JS_DIR, 'core', 'debug_panel.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const SRC = process.argv[2];
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body>' +
  '<div id="debugPanelWrap"><div id="debugTitle"></div><div id="debugContent"></div></div>' +
  '</body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;

// Stubs debug_panel.js touches.
win.Icon = global.Icon = (name, sz) => '<svg data-icon="' + name + '"></svg>';
// key-echo t(): lets us assert on the i18n KEY (debug.brainCharter/Board).
win.t = global.t = (k) => k;
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);
win.debugVisible = global.debugVisible = true;
// activeConvId undefined → showMessagesInDebug renders unconditionally.

// Indirect eval so debug_panel.js's top-level `function` declarations leak to
// global scope (sloppy mode) — the file attaches nothing to window itself.
const ie = eval;
ie(fs.readFileSync(SRC, 'utf8'));

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const fn = global.showMessagesInDebug || win.showMessagesInDebug;
if (typeof fn !== 'function') {
  console.log('FAIL fn_exposed showMessagesInDebug missing'); console.log(out.join('\n'));
  process.exit(0);
}
check('fn_exposed', true);

// Wire-form snapshot the MODEL saw:
//   #0 system, content = ARRAY of text blocks (as the real assembler emits),
//      one block carrying BOTH brain markers.
//   #1 system, content = STRING with NO markers (base instructions).
//   #2 user.
const messages = [
  { role: 'system', content: [
    { type: 'text', text: 'base instructions here' },
    { type: 'text', text: '<system-reminder>\n[PROJECT CHARTER] — north star ...\n</system-reminder>' },
    { type: 'text', text: '<system-reminder>\n[PROJECT BOARD] — coordination ...\n</system-reminder>' },
  ] },
  { role: 'system', content: 'plain system with no brain markers at all' },
  { role: 'user', content: 'hello' },
];

fn(messages, 'test', false, null, null, false);

const title = win.document.getElementById('debugTitle').innerHTML;
// Header counter: names BOTH charter + board (key-echo) + a brain SVG glyph.
check('header_has_brain_glyph', title.indexOf('data-icon="brain"') !== -1);
check('header_names_charter', title.indexOf('debug.brainCharter') !== -1);
check('header_names_board', title.indexOf('debug.brainBoard') !== -1);

const blocks = win.document.querySelectorAll('#debugContent .debug-msg-block');
check('blocks_rendered', blocks.length === 3);
// Block #0 (system WITH markers) → brain badge + class.
const b0 = blocks[0];
check('sys0_has_brain_class', !!b0 && b0.classList.contains('debug-msg-brain'));
const badge0 = b0 ? b0.querySelector('.debug-brain-badge') : null;
check('sys0_has_brain_badge', !!badge0);
check('sys0_badge_svg', !!badge0 && !!badge0.querySelector('svg'));
check('sys0_badge_names_both',
  !!badge0 && badge0.innerHTML.indexOf('debug.brainCharter') !== -1 &&
  badge0.innerHTML.indexOf('debug.brainBoard') !== -1);
// Block #1 (system WITHOUT markers) → NO badge (marker-gated, not role-gated).
const b1 = blocks[1];
check('sys1_no_brain_badge', !!b1 && !b1.querySelector('.debug-brain-badge') &&
  !b1.classList.contains('debug-msg-brain'));
// Block #2 (user) → NO badge.
const b2 = blocks[2];
check('user_no_brain_badge', !!b2 && !b2.querySelector('.debug-brain-badge'));

console.log(out.join('\n'));
"""


def _run_harness(debug_src):
    harness = os.path.join(HERE, '_debug_brain_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, debug_src, ROOT],
            capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_debug_panel_brain_badge_and_header_counter():
    """A wire-form snapshot whose system message carries [PROJECT CHARTER] /
    [PROJECT BOARD] gets a per-message brain badge + a header counter naming
    both — sniffed strictly from the authoritative marker strings. A system
    message WITHOUT the markers gets no badge."""
    output = _run_harness(_DEBUG_SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'debug-brain-badge failures:\n' + output
    for must in ('PASS header_has_brain_glyph', 'PASS header_names_charter',
                 'PASS header_names_board', 'PASS sys0_has_brain_class',
                 'PASS sys0_has_brain_badge', 'PASS sys0_badge_svg',
                 'PASS sys0_badge_names_both', 'PASS sys1_no_brain_badge',
                 'PASS user_no_brain_badge'):
        assert must in output, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_brain_sniff_is_load_bearing():
    """Frontend NC: neuter _debugBrainInfo so it always returns null → the
    per-message badge AND the header counter both vanish → the badge/counter
    assertions FAIL. Shipped file byte-identical afterwards."""
    with open(_DEBUG_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = "function _debugBrainInfo(msg) {"
    assert anchor in original, 'brain-sniff anchor not found'
    patched = original.replace(
        anchor, "function _debugBrainInfo(msg) {\n  return null;  // NC (brain sniff disabled)", 1)
    copy_path = os.path.join(HERE, '_debug_brain_nc_copy.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run_harness(copy_path)
        assert 'FAIL sys0_has_brain_badge' in output, \
            ('NC: disabling _debugBrainInfo must make sys0_has_brain_badge '
             'FAIL:\n' + output)
        assert 'FAIL header_names_charter' in output, \
            ('NC: disabling _debugBrainInfo must also drop the header '
             'counter:\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_DEBUG_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped debug_panel.js must be byte-identical'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_marker_string_is_the_judge():
    """Frontend NC: change the authoritative marker the sniff looks for
    ([PROJECT CHARTER] → a bogus string) in a COPY → the real snapshot's
    charter block no longer matches → sys0_badge_names_both + header_names_charter
    FAIL. Proves the EXACT marker string is the judge (per the owner's hard
    constraint), not some other heuristic. Byte-identical restore."""
    with open(_DEBUG_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = 'const charter = text.indexOf("[PROJECT CHARTER]") !== -1;'
    assert anchor in original, 'charter-marker anchor not found'
    patched = original.replace(
        anchor, 'const charter = text.indexOf("[NOPE NOT THE MARKER]") !== -1;', 1)
    copy_path = os.path.join(HERE, '_debug_marker_nc_copy.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run_harness(copy_path)
        assert 'FAIL sys0_badge_names_both' in output, \
            ('NC: changing the [PROJECT CHARTER] marker must break the '
             'charter half of the badge:\n' + output)
        assert 'FAIL header_names_charter' in output, output
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_DEBUG_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped debug_panel.js must be byte-identical'
