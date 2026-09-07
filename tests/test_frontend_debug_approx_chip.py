"""jsdom test for the debug-panel "reconstructed approximation" chip.

Loads the REAL shipped static/js/core/debug_panel.js under jsdom and drives
``showMessagesInDebug`` directly, asserting the rendered DOM:

  • COLD path (approx=true, what the /debug-messages endpoint sends) → the
    amber chip renders WITH both disclosure strings (memory/date first-round;
    transport-layer transforms not expanded).
  • LIVE path (approx=false/undefined, the real wire-form SSE snapshot) →
    NO chip (negative control — proves the chip is gated strictly on the
    endpoint flag, not on the panel in general).
  • Toggling approx back off removes a previously-shown chip.

Mirrors the harness style of tests/test_frontend_presence.py. Skips cleanly
when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_sections_dir

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = runtime_sections_dir()


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body><div id="debugPanel">' +
  '<div id="debugTitle"></div>' +
  '<div id="debugContent"></div>' +
  '</div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;

win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
// Icon(): return a deterministic stub <svg> so the chip's glyph is present
// but we don't depend on the real path data.
win.Icon = global.Icon = (name, size) => `<svg data-icon="${name}" width="${size||14}"></svg>`;
// t(): echo the en value so the disclosure strings are readable + assertable.
const _I18N = {
  'debug.approxTitle': 'Reconstructed approximation (not a specific turn)',
  'debug.approxMemDate': 'Memory <relevant_memories> and date are reconstructed as a hypothetical first-round, not a specific historical turn.',
  'debug.approxTransport': 'Transport-layer transforms (image resolve/downscale, provider body reshape) are not expanded here.',
};
win.t = global.t = (k) => _I18N[k] || k;

win.activeConvId = global.activeConvId = 'conv-1';
win.conversations = global.conversations = [{ id: 'conv-1' }];
// debugVisible used by toggleDebug (not exercised here) — define to be safe.
win.debugVisible = global.debugVisible = true;

eval(fs.readFileSync(process.argv[2], 'utf8'));  // core/debug_panel.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof win.showMessagesInDebug !== 'function'
    && typeof showMessagesInDebug !== 'function') {
  console.log('FAIL fn_present showMessagesInDebug not defined');
  console.log(out.join('\n'));
  process.exit(0);
}
check('fn_present', true);

const panel = document.getElementById('debugPanel');
const msgs = [
  { role: 'system', content: 'SYS' },
  { role: 'user', content: 'hi' },
];

// ── COLD path: approx=true → chip present with BOTH disclosures ──
showMessagesInDebug(msgs, '2 msgs (server)', false, 'conv-1', undefined, true);
const chip = panel.querySelector('.debug-approx-chip');
check('chip_present_when_approx', !!chip);
check('chip_title', chip && chip.innerHTML.indexOf('Reconstructed approximation') !== -1);
check('chip_discloses_mem_date',
  chip && chip.innerHTML.indexOf('hypothetical first-round') !== -1);
check('chip_discloses_transport',
  chip && chip.innerHTML.indexOf('Transport-layer transforms') !== -1);
// SVG glyph only — no emoji (§3.4). The chip head must carry an <svg> icon.
check('chip_uses_svg_glyph',
  chip && chip.querySelector('svg[data-icon="alertTriangle"]') !== null);
// The <relevant_memories> token inside the disclosure must be ESCAPED, not
// interpreted as a tag (it would otherwise vanish from the DOM text).
check('chip_text_escaped',
  chip && chip.innerHTML.indexOf('&lt;relevant_memories&gt;') !== -1);

// ── LIVE path (negative control): approx omitted → NO chip ──
// This is the real wire-form SSE snapshot shape; it must never show the chip.
showMessagesInDebug(msgs, 'Round 1 · 2条', true, 'conv-1', undefined);
check('no_chip_on_live_snapshot',
  panel.querySelector('.debug-approx-chip') === null);

// ── Toggle back ON then OFF removes the chip again ──
showMessagesInDebug(msgs, 'server', false, 'conv-1', undefined, true);
check('chip_back_on', !!panel.querySelector('.debug-approx-chip'));
showMessagesInDebug(msgs, 'server', false, 'conv-1', undefined, false);
check('chip_removed_when_approx_false',
  panel.querySelector('.debug-approx-chip') === null);

console.log(out.join('\n'));
"""


def _run():
    harness = os.path.join(HERE, '_debug_approx_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'core', 'debug_panel.js'),   # argv[2]
             ROOT,                                              # argv[3]
             ],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'debug-approx-chip render failures:\n' + output
    assert output.count('PASS') >= 10, f'expected >=10 PASS lines, got:\n{output}'
    print(output)


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_debug_approx_chip_renders():
    _run()


if __name__ == '__main__':
    _run()
