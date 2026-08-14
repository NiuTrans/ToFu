"""Tests for the auto-escaping `safeHtml` tagged-template helper and the
chat-render interpolation lint rule.

Two concerns:

1. **Behavior** — `static/js/core/safe_html.js` must escape interpolations
   by default, pass `raw()` through verbatim, join arrays, and compose
   nested `safeHtml` results without double-escaping. We exercise the real
   JS via Node so the test tracks the shipped implementation.

2. **Lint** — once a render function adopts `safeHtml`, future edits must
   not silently reintroduce a bare template-string sink for user/model
   content. The lint rule flags `insertAdjacentHTML(...,  `...${x}...` )`
   and `.outerHTML = `...${x}...`` style raw-template sinks in the
   chat-render hotspot files, steering devs to `safeHtml`.
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


def _node_available() -> bool:
    return shutil.which('node') is not None


# ── 1. Behavior of safeHtml (run the real JS in Node) ──

_HARNESS = r"""
const fs = require('fs');
function load(p){ return fs.readFileSync(p,'utf8'); }
eval(load(process.argv[2]));   // escape_html.js
eval(load(process.argv[3]));   // safe_html.js

const out = [];
function check(name, got, want) {
  out.push((String(got) === String(want) ? 'PASS ' : 'FAIL ') + name +
           (String(got) === String(want) ? '' : ` got=${JSON.stringify(String(got))} want=${JSON.stringify(String(want))}`));
}

// escapes by default
check('escape_default', safeHtml`<b>${'<script>'}</b>`, '<b>&lt;script&gt;</b>');
// raw passes through
check('raw_passthrough', safeHtml`<x>${raw('<i>ok</i>')}</x>`, '<x><i>ok</i></x>');
// null/undefined → ''
check('null_empty', safeHtml`a${null}b${undefined}c`, 'abc');
// numbers coerce + escape (no special chars here)
check('number', safeHtml`n=${42}`, 'n=42');
// arrays are joined, each escaped
check('array_join', safeHtml`<ul>${['<a>', '&b']}</ul>`, '<ul>&lt;a&gt;&amp;b</ul>');
// nested safeHtml composes without double-escaping
const inner = safeHtml`<li>${'<x>'}</li>`;
check('nested_compose', safeHtml`<ul>${inner}</ul>`, '<ul><li>&lt;x&gt;</li></ul>');
// array of nested safeHtml
const items = ['<a>', '<b>'].map(s => safeHtml`<li>${s}</li>`);
check('array_nested', safeHtml`<ul>${items}</ul>`, '<ul><li>&lt;a&gt;</li><li>&lt;b&gt;</li></ul>');
// quotes escaped (attribute context)
check('attr_quotes', safeHtml`<div title="${'a"b'}">`, '<div title="a&quot;b">');
// a plain object injected as raw-shaped JSON must NOT bypass escaping
check('fake_raw_obj', safeHtml`${ {value:'<x>', __safeHtmlRaw:true} }`,
      escapeHtml(String({value:'<x>', __safeHtmlRaw:true})));

console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_safe_html_behavior():
    harness = os.path.join(HERE, '_safe_html_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'core', 'escape_html.js'),
             os.path.join(JS_DIR, 'core', 'safe_html.js')],
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
    assert not fails, 'safeHtml behavior failures:\n' + '\n'.join(fails)
    # Sanity: we actually ran the checks.
    assert output.count('PASS') >= 9, f'expected >=9 PASS lines, got:\n{output}'


# ── 2. safe_html.js must be wired into the bundler + dev-mode tags ──

def test_safe_html_in_bundler():
    from lib.js_bundler import _BUNDLE_FILES
    assert 'core/safe_html.js' in _BUNDLE_FILES, (
        'core/safe_html.js missing from _BUNDLE_FILES — it would load as a '
        'silent no-op in production (CLAUDE.md §3.2.1).'
    )
    # Must come after escape_html.js (it calls escapeHtml at module scope-ish).
    assert (_BUNDLE_FILES.index('core/safe_html.js')
            > _BUNDLE_FILES.index('core/escape_html.js')), (
        'safe_html.js must be bundled AFTER escape_html.js.'
    )


def test_safe_html_is_not_a_raw_index_script():
    with open(os.path.join(ROOT, 'index.html'), encoding='utf-8') as f:
        html = f.read()
    assert 'static/js/core/safe_html.js' not in html
    assert '<!-- TOFU_APP_ASSETS -->' in html


# ── 3. Chat-render lint: no bare template-string HTML sinks ──

# Files that have adopted (or are the target for) safeHtml. New raw-template
# sinks of dynamic content here must go through safeHtml instead.
_GUARDED_FILES = [
    'ui/streaming_render.js',
    'ui/chat_render.js',
]

# A raw-template sink: insertAdjacentHTML(pos, `...${...}...`) or
# `.outerHTML = `...${...}...`` / `.innerHTML = `...${...}...``  where the
# template literal contains an interpolation. We detect the dangerous shape
# (sink + backtick template with ${) on a single logical line.
import re  # noqa: E402

_SINK_RE = re.compile(
    r"""(insertAdjacentHTML\s*\([^,]+,\s*`[^`]*\$\{   # insertAdjacentHTML(pos, `...${
        | \.(outerHTML|innerHTML)\s*=\s*`[^`]*\$\{)    # .outerHTML = `...${
    """,
    re.VERBOSE,
)

# Allow an explicit opt-out comment for reviewed exceptions.
_ALLOW_MARK = 'safe-html-lint-ok'


def test_no_bare_template_html_sinks_in_guarded_files():
    offenders = []
    for rel in _GUARDED_FILES:
        path = os.path.join(JS_DIR, rel)
        with open(path, encoding='utf-8') as f:
            for lineno, line in enumerate(f, 1):
                if _ALLOW_MARK in line:
                    continue
                if _SINK_RE.search(line):
                    offenders.append(f'{rel}:{lineno}: {line.strip()[:120]}')
    assert not offenders, (
        'Bare template-string HTML sink with interpolation found in a '
        'safeHtml-guarded file. Build the markup with safeHtml`...` (which '
        'auto-escapes) and pass the result to the sink, or add a '
        f'`{_ALLOW_MARK}` comment if the interpolation is provably static.\n'
        + '\n'.join(offenders)
    )
