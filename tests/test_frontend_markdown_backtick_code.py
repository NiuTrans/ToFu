"""Regression test for the Markdown backtick → code invariant.

A backtick span is always code in Markdown — it must NEVER be re-routed to
the math (KaTeX) pipeline, even when its content *looks* like LaTeX (a
`\\letter` command, a `_subscript`, a `^{...}`).  An earlier override
(`_LATEX_IN_BACKTICK_RE` in static/js/core/markdown.js) reached into
backticks and pushed "math-shaped" spans to KaTeX; it corrupted ordinary
code/regex such as ``r'\\d+ : \\d+'`` and ``_RG_MATCH_LINE`` into garbled
subscripts.  The override was removed; this test locks that in.

We run the REAL renderMarkdown() (via Node + the shipped marked.min.js) so
the test tracks the production implementation, not a re-implementation.
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


def _node_available() -> bool:
    return shutil.which('node') is not None


# Load escape_html.js + the real marked UMD, stub the few globals
# renderMarkdown touches (BASE_PATH, _ensureKatex), then eval markdown.js
# and exercise renderMarkdown directly.  DOMPurify / katex / projectState
# are all guarded by `typeof … !== 'undefined'` in the source, so leaving
# them undefined exercises the no-KaTeX fallback path — exactly what we
# want to distinguish "rendered as code" from "routed to math".
_HARNESS = r"""
const fs = require('fs');
function load(p){ return fs.readFileSync(p,'utf8'); }

global.marked = require(process.argv[4]);   // marked.min.js (UMD)
var BASE_PATH = '';
var _ensureKatex = function(){};            // no-op: katex stays unloaded

eval(load(process.argv[2]));   // escape_html.js  → escapeHtml
eval(load(process.argv[3]));   // core/markdown.js → renderMarkdown

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// 1. A regex inside backticks must be a <code> span, never math.
let h = renderMarkdown("`_RG_MATCH_LINE = r'\\d+ : \\d+ :'`");
check('regex_is_code', /<code>[^<]*_RG_MATCH_LINE/.test(h));
check('regex_not_mathpending', !/math-pending|math-error/.test(h));

// 2. A bare \letter command in backticks is also code now (accepted
//    tradeoff: models must use $…$ for typeset math).
h = renderMarkdown("`\\hat K = \\text{LN}(X)`");
check('latexish_backtick_is_code', /<code>/.test(h) && !/math-pending/.test(h));

// 3. The $…$ math pipeline still works — with katex unloaded it must fall
//    back to a math-pending span (i.e. it WAS recognised as math).
h = renderMarkdown("inline $E = mc^2$ here");
check('dollar_is_math', /math-pending/.test(h));

// 4. CJK text mixed with an inline-code regex: code stays code.
h = renderMarkdown("正则 `\\d+` 匹配数字");
check('cjk_with_code', /<code>[^<]*\\d\+/.test(h) && !/math-pending/.test(h));

console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_backtick_content_renders_as_code_not_math():
    harness = os.path.join(HERE, '_md_backtick_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'core', 'escape_html.js'),
             os.path.join(JS_DIR, 'core', 'markdown.js'),
             os.path.join(ROOT, 'static', 'vendor', 'marked.min.js')],
            capture_output=True, text=True, timeout=30,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'backtick-code render failures:\n' + output
    assert output.count('PASS') >= 5, f'expected >=5 PASS lines, got:\n{output}'


def test_latex_in_backtick_override_removed():
    """The rogue override must stay gone — guard against re-introduction."""
    with open(os.path.join(JS_DIR, 'core', 'markdown.js'), encoding='utf-8') as f:
        src = f.read()
    assert '_LATEX_IN_BACKTICK_RE' not in src, (
        'The _LATEX_IN_BACKTICK_RE backtick→math override was re-added. '
        'Backtick spans are code; math must come from $…$ / \\(…\\). '
        'See tests/test_frontend_markdown_backtick_code.py.'
    )
