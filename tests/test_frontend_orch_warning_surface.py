"""tests/test_frontend_orch_warning_surface.py — validator-warning SURFACING.

Proves the last hop of the orchestration validator-warning path is not inert:
``validate_definition``'s warnings reach the browser (backend contract tested
in test_orchestrations.py), and the studio must render the warning **text** to
the author — not merely a count. Before this, ``_orchSave`` showed only
``Saved "X" (1 warning)`` so the parallel verdict-channel warning (and every
other validator warning) was effectively swallowed.

Extracts the REAL shipped ``_orchToast`` + ``_orchWarnToast`` from
static/js/orchestration.js (the file is a 2500-line module with many
top-level Api/window refs, so we lift just these two pure DOM helpers rather
than eval the whole file) and runs them under jsdom. Skips when node+jsdom
are absent.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
ORCH_JS = os.path.join(ROOT, 'static', 'js', 'orchestration.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


def _extract(fn_name: str) -> str:
    """Lift a top-level ``function <fn_name>(...) { ... }`` by brace-matching."""
    src = open(ORCH_JS, encoding='utf-8').read()
    m = re.search(r'\nfunction ' + re.escape(fn_name) + r'\s*\(', src)
    assert m, f'{fn_name} not found in orchestration.js'
    i = src.index('{', m.start())
    depth = 0
    for j in range(i, len(src)):
        c = src[j]
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return src[m.start() + 1:j + 1]
    raise AssertionError(f'unbalanced braces extracting {fn_name}')


_HARNESS = r"""
const path = require('path');
const ROOT = process.argv[2];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: 'http://localhost/' });
global.window = dom.window; global.document = dom.window.document;

__FUNCS__

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// A parallel verdict-channel warning (the exact string family the validator
// now emits) must surface as READABLE TEXT, not just a count.
const warns = [
  "parallel 'p' region contains verdict-feeding producer(s) ['w1'] " +
  "(a verifier role or a shared-context producer) — the single-valued " +
  "feedback/directive channel is consumed order-dependently across " +
  "concurrent branches."];
document.querySelectorAll('.orch-toast').forEach(e => e.remove());
_orchWarnToast('Saved "Flow"', warns);
const toast = document.querySelector('.orch-toast');
check('toast_created', !!toast);
const txt = toast ? toast.textContent : '';
check('headline_has_prefix', txt.includes('Saved "Flow"'));
check('headline_has_count', txt.includes('1 warning'));
// The load-bearing assertion: the ACTUAL warning text is in the DOM.
check('detail_has_warning_text', txt.includes('verdict-feeding producer'));
check('detail_has_fix_hint', txt.includes('order-dependent'));
check('detail_node_named', txt.includes("['w1']"));
check('has_warn_class', !!(toast && toast.classList.contains('is-warn')));
check('detail_block_present', !!(toast && toast.querySelector('.orch-toast-detail')));

// No warnings → plain toast, no detail block, no warn styling.
document.querySelectorAll('.orch-toast').forEach(e => e.remove());
_orchWarnToast('Saved "Clean"', []);
const clean = document.querySelector('.orch-toast');
check('clean_no_detail', !!(clean && !clean.querySelector('.orch-toast-detail')));
check('clean_no_warn_class', !!(clean && !clean.classList.contains('is-warn')));
check('clean_headline', !!(clean && clean.textContent.includes('Saved "Clean"')));
check('clean_no_count', !!(clean && !/warning/.test(clean.textContent)));

console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_orch_warning_text_surfaces():
    funcs = _extract('_orchToast') + '\n' + _extract('_orchWarnToast')
    harness_src = _HARNESS.replace('__FUNCS__', funcs)
    harness = os.path.join(HERE, '_orch_warning_surface_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(harness_src)
    try:
        proc = subprocess.run(
            ['node', harness, ROOT],
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
    assert not fails, 'warning-surface failures:\n' + output
    assert output.count('PASS') >= 12, f'expected >=12 PASS, got:\n{output}'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
