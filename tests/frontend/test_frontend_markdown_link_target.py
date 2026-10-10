"""Behavior spec for the Markdown link-target invariant (docs/RENDER_CONTRACT.md).

Every anchor the Markdown renderer emits without an explicit `target` is
rewritten to `target="_blank" rel="noopener noreferrer"` so navigation never
replaces the single-page app. In-page `#anchor` links keep same-tab scrolling
and `mailto:` keeps its default handler; anchors that already declare a
target (raw-HTML passthrough) are left alone. The rewrite is part of the
cached render output, so repeat renders of the same text agree.

Runs the REAL renderMarkdown() from the retained markdown section (Node +
the marked build the Vite graph bundles), same harness pattern as
tests/frontend/test_frontend_markdown_backtick_code.py.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import native_module_path, runtime_sections_dir

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..', '..'))
JS_DIR = runtime_sections_dir()
HTML_SAFETY = native_module_path(
    '.native/markdown-html-safety.js',
    os.path.join(ROOT, 'frontend', 'src', 'html-safety.ts'),
)

_HARNESS = r"""
const fs = require('fs');
function load(p){ return fs.readFileSync(p,'utf8'); }

global.marked = require(process.argv[4]);
var BASE_PATH = '';
var _ensureKatex = function(){};

(0, eval)(load(process.argv[2])); // typed HTML-safety owner → escapeHtml
eval(load(process.argv[3]));      // core/markdown.js → renderMarkdown

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// 1. An ordinary https link is rewritten to open in a new tab, with the
//    noopener pair that target=_blank requires.
let h = renderMarkdown('[site](https://example.com/x)');
check('external_gets_blank', /target="_blank"/.test(h));
check('external_gets_noopener', /rel="noopener noreferrer"/.test(h));
check('external_keeps_href', /href="https:\/\/example\.com\/x"/.test(h));

// 2. In-page anchors keep same-tab scrolling (no target attribute added).
h = renderMarkdown('[jump](#details)');
check('inpage_anchor_untouched',
      /<a href="#details">/.test(h) && !/target=/.test(h));

// 3. mailto: keeps its default handler (a new tab would flash blank).
h = renderMarkdown('[mail](mailto:a@b.c)');
check('mailto_untouched',
      /<a href="mailto:a@b\.c">/.test(h) && !/target=/.test(h));

// 4. Raw-HTML anchors declaring their own target keep it (passthrough).
h = renderMarkdown('<a href="https://example.com" target="_self">x</a>');
check('explicit_target_kept',
      /target="_self"/.test(h) && !/target="_blank"/.test(h));

// 5. The rewrite is part of the cached output: a second render of the same
//    text returns the identical string (cache identity, not re-processing).
const first = renderMarkdown('[site](https://example.com/x)');
const second = renderMarkdown('[site](https://example.com/x)');
check('rewrite_is_cached', first === second && /target="_blank"/.test(second));

console.log(out.join('\n'));
"""


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_markdown_link_target_contract():
    marked_cjs = os.path.join(ROOT, 'node_modules', 'marked', 'lib', 'marked.cjs')
    if not os.path.isfile(marked_cjs):
        pytest.skip('marked dev-dep not installed (run `npm install`)')
    harness = os.path.join(HERE, '_md_link_target_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        result = subprocess.run(
            ['node', harness, HTML_SAFETY,
             os.path.join(JS_DIR, 'core', 'markdown.js'), marked_cjs],
            cwd=ROOT, capture_output=True, text=True, timeout=30)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    assert result.returncode == 0, result.stdout + result.stderr
    fails = [ln for ln in result.stdout.splitlines() if ln.startswith('FAIL ')]
    passed = [ln for ln in result.stdout.splitlines() if ln.startswith('PASS ')]
    assert not fails, '\n'.join(fails + [result.stderr])
    assert len(passed) >= 7, result.stdout
