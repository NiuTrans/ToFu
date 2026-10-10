"""Behavior spec for the escapeRawHtml contract (docs/RENDER_CONTRACT.md).

Human-authored turns are plain text, not HTML: the renderer passes
`escapeRawHtml` to the markdown port, which escapes every `<` so CommonMark
raw-HTML passthrough cannot swallow a typed `<tag>` as an unknown element.
Code spans/fences run as placeholders at that point, so code keeps its
literal `<` (escaped once by the parser inside `<code>`), and `>` is never
escaped so blockquote syntax survives. Assistant output (no option) keeps
the raw-HTML contract.

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

// 1. With escapeRawHtml, a typed <tag> is escaped: no element is created and
//    the literal text stays visible as &lt;.
let h = renderMarkdown('look: <tag> x', {escapeRawHtml: true});
check('typed_tag_escaped', !/<tag>/.test(h));
check('typed_tag_visible', /&lt;tag&gt;/.test(h));

// 2. Code spans keep their literal '<' (the parser escapes it once inside
//    <code>); the escape pass must not double-escape the entity.
h = renderMarkdown('`a < b`', {escapeRawHtml: true});
check('code_keeps_lt', /<code>a &lt; b<\/code>/.test(h));
check('code_not_double_escaped', !/&amp;lt;/.test(h));

// 3. Fenced code keeps literal '<' as well.
h = renderMarkdown('```\nif (a < b) {}\n```', {escapeRawHtml: true});
check('fence_keeps_lt', /a &lt; b/.test(h) && !/&amp;lt;/.test(h));

// 4. '>' is never escaped: blockquote syntax survives the option.
h = renderMarkdown('> quoted', {escapeRawHtml: true});
check('blockquote_survives', /<blockquote>/.test(h) && /quoted/.test(h));

// 5. Without the option (assistant output) raw HTML keeps its contract.
h = renderMarkdown('plain <b>bold</b>');
check('assistant_raw_html_kept', /<b>bold<\/b>/.test(h));

// 6. The option is part of the cache identity: the same text rendered with
//    and without the option returns different, individually-correct output.
const esc = renderMarkdown('a <tag> b', {escapeRawHtml: true});
const raw = renderMarkdown('a <tag> b');
const esc2 = renderMarkdown('a <tag> b', {escapeRawHtml: true});
check('option_in_cache_key', esc !== raw && esc === esc2);

console.log(out.join('\n'));
"""


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_markdown_escape_raw_html_contract():
    marked_cjs = os.path.join(ROOT, 'node_modules', 'marked', 'lib', 'marked.cjs')
    if not os.path.isfile(marked_cjs):
        pytest.skip('marked dev-dep not installed (run `npm install`)')
    harness = os.path.join(HERE, '_md_escape_raw_harness.js')
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
    assert len(passed) >= 8, result.stdout
