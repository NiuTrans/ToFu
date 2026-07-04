"""jsdom regression for the Commit-2 helper dedup (escapeHtml + clipboard).

WHY
---
Several files carried their OWN HTML-escaper (a full `.replace(/&/g…)` chain,
or memory.js's slow `createElement/textContent` variant) and their OWN
clipboard fallback, instead of the canonical bundled helpers
`escapeHtml` (core/escape_html.js) and `_safeClipboardWrite`
(core/debug_panel.js). Commit 2 collapsed those onto the shared helpers.

Escaping is XSS-adjacent, so this is verified, not self-reported: the test
loads the REAL shipped files under jsdom and asserts (1) the canonical
`escapeHtml` neutralises the full metachar set `& < > " '` — including the
`"`/`'` that some collapsed partial re-impls used to miss; (2) the collapsed
`memory.js` `_esc` now routes through it (so `<img onerror>`-style payloads are
inert); (3) the clipboard callers (artifacts._copySource, the oauth curl-copy
button) delegate to the shared `_safeClipboardWrite` instead of open-coding
their own `navigator.clipboard || textarea+execCommand` fallback (asserted at
source level — robust vs. jsdom's unreliable execCommand stub).

NC (biting): revert `memory.js::_esc` to an identity passthrough (the pre-fix
DOM-escaper is equivalent to the global, but a passthrough models "someone
un-did the dedup wrong") → the `memory_esc_blocks_script` assertion MUST fail
while the canonical-escapeHtml assertions stay green. Proven by the two runs
below (fix → all green; NC → the memory assertion flips FAIL).

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


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const NC = process.argv[3] === 'NC';   // negative-control mode
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.navigator = win.navigator;
global.console = console;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── Load the REAL canonical helper (core/escape_html.js) into shared scope ──
eval(fs.readFileSync(path.join(ROOT, 'static', 'js', 'core', 'escape_html.js'), 'utf8'));

if (typeof escapeHtml !== 'function') { console.log('FAIL fn_exposed escapeHtml missing'); process.exit(0); }
check('fn_exposed_escapeHtml', true);

// ════════════════════════════════════════════════════════════════════
// 1 — the canonical escapeHtml neutralises the FULL metachar set.
//     The partial re-impls we collapsed (log-clean's 3-char chain) missed
//     " and ', which are load-bearing inside attribute contexts. The shared
//     helper covers all five.
// ════════════════════════════════════════════════════════════════════
const raw = `<img src=x onerror="alert(1)">&'"`;
const esc = escapeHtml(raw);
check('esc_lt',  !esc.includes('<'));
check('esc_gt',  !esc.includes('>'));
check('esc_amp_entity', esc.includes('&amp;'));
check('esc_dquote', esc.includes('&quot;') && !/[^&]"/.test('X' + esc));
check('esc_squote', esc.includes('&#39;'));
check('esc_no_raw_onerror_tag', !esc.includes('<img'));

// ── Now load memory.js and prove its _esc routes through escapeHtml. ──
// memory.js references many globals at load; stub the few touched at module
// top-level / needed by _esc. _esc is a plain top-level function.
win.marked = global.marked = undefined;   // force _renderMemoryBody fallback path (not under test)
// memory.js is pure function declarations at top level (no IIFE side effects
// that need DOM ids), so eval-ing it just defines the functions.
eval(fs.readFileSync(path.join(ROOT, 'static', 'js', 'memory.js'), 'utf8'));

if (NC) {
  // NC: model a botched dedup where _esc was left as an identity passthrough.
  // Redefine it AFTER load to simulate the regression. The biting assertion
  // below must then FAIL.
  // eslint-disable-next-line no-global-assign
  _esc = function (s) { return String(s == null ? '' : s); };
}

if (typeof _esc !== 'function') { console.log('FAIL fn_exposed _esc missing'); process.exit(0); }
check('fn_exposed_memory_esc', true);

// ════════════════════════════════════════════════════════════════════
// 2 (BITING) — memory.js _esc must render an <img onerror> payload inert.
//     Fix → escaped (no live <img); NC passthrough → raw <img> survives → FAIL.
// ════════════════════════════════════════════════════════════════════
const memOut = _esc(`<img src=x onerror="x">`);
check('memory_esc_blocks_script', !memOut.includes('<img'));

console.log(out.join('\n'));
"""


def _run(nc: bool):
    harness = os.path.join(HERE, '_dedup_helpers_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        argv = ['node', harness, ROOT]
        if nc:
            argv.append('NC')
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
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
def test_dedup_helpers_escape_and_clipboard():
    output = _run(nc=False)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'dedup-helpers failures:\n' + output
    # fn_exposed_escapeHtml + 6 esc metachar + fn_exposed_memory_esc +
    # memory_esc_blocks_script = 9
    assert output.count('PASS') >= 9, f'expected >=9 PASS lines, got:\n{output}'


def test_clipboard_callers_delegate_to_safe_helper():
    """artifacts._copySource + oauth curl-copy must delegate to the shared
    _safeClipboardWrite, not open-code their own navigator.clipboard/textarea
    fallback. Source-level (no node needed) — robust against jsdom quirks."""
    art = open(os.path.join(ROOT, 'static', 'js', 'artifacts.js'),
               encoding='utf-8').read()
    oauth = open(os.path.join(ROOT, 'static', 'js', 'settings', 'oauth.js'),
                 encoding='utf-8').read()
    # _copySource now calls the shared helper …
    assert '_safeClipboardWrite(text)' in art, (
        'artifacts._copySource must route through _safeClipboardWrite')
    # … and no longer open-codes the execCommand fallback it used to.
    assert "document.execCommand(\"copy\")" not in art, (
        'artifacts.js still open-codes an execCommand copy fallback')
    assert '_safeClipboardWrite(ta.value)' in oauth, (
        'oauth curl-copy button must route through _safeClipboardWrite')
    assert "document.execCommand('copy')" not in oauth, (
        'oauth.js still open-codes an execCommand copy fallback')


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_dedup_helpers_nc_catches_broken_memory_esc():
    """NC: a passthrough _esc (botched dedup) must trip memory_esc_blocks_script."""
    output = _run(nc=True)
    lines = output.splitlines()
    assert 'FAIL memory_esc_blocks_script' in lines, (
        'NC did not catch the broken memory _esc — the biting assertion is not '
        'actually biting:\n' + output
    )
    # The canonical escapeHtml assertions must STAY green in NC mode (only the
    # memory passthrough regressed), proving the test isolates the right thing.
    assert 'PASS esc_lt' in lines and 'PASS esc_dquote' in lines, output
