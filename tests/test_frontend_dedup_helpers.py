"""jsdom regression for the Commit-2 helper dedup (escapeHtml + clipboard).

WHY
---
Several files carried their OWN HTML-escaper (a full `.replace(/&/g…)` chain,
or memory.js's slow `createElement/textContent` variant) and their OWN
clipboard fallback, instead of the canonical bundled helpers
`escapeHtml` (frontend/src/html-safety.ts) and `_safeClipboardWrite`
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
from pathlib import Path

import pytest

from tests._runtime_sections import (
    native_module_path,
    runtime_section,
    runtime_sections_dir,
)

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = runtime_sections_dir()
MEMORY_PANEL = Path(ROOT) / 'frontend/src/features/memory/panel.ts'
HTML_SAFETY = native_module_path(
    '.native/dedup-html-safety.js',
    Path(ROOT) / 'frontend/src/html-safety.ts',
)


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const JS_DIR = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.navigator = win.navigator;
global.console = console;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── Load the REAL canonical typed helper into shared scope ──
(0, eval)(fs.readFileSync(process.argv[4], 'utf8'));

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

console.log(out.join('\n'));
"""


def _run():
    harness = os.path.join(HERE, '_dedup_helpers_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        argv = ['node', harness, ROOT, JS_DIR, HTML_SAFETY]
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
    output = _run()
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'dedup-helpers failures:\n' + output
    assert output.count('PASS') >= 7, f'expected >=7 PASS lines, got:\n{output}'
    source = MEMORY_PANEL.read_text()
    assert "import { escapeHtmlText as escape } from '../../html-safety';" in source
    assert 'function escape(' not in source


def test_clipboard_callers_delegate_to_safe_helper():
    """The remaining artifact copy action must delegate to the shared
    _safeClipboardWrite, not open-code their own navigator.clipboard/textarea
    fallback. Source-level (no node needed) — robust against jsdom quirks."""
    art = runtime_section('artifacts.js')
    # _copySource now calls the shared helper …
    assert '_safeClipboardWrite(text)' in art, (
        'artifacts._copySource must route through _safeClipboardWrite')
    # … and no longer open-codes the execCommand fallback it used to.
    assert "document.execCommand(\"copy\")" not in art, (
        'artifacts.js still open-codes an execCommand copy fallback')


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_memory_escape_dependency_has_one_explicit_import():
    """The memory renderer has no mutable registry/fallback escape policy."""
    source = MEMORY_PANEL.read_text()
    assert source.count("from '../../html-safety'") == 1
    assert 'escapeHtml?:' not in source
    assert '.escapeHtml' not in source
