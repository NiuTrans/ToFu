"""Public contracts for the typed HTML-safety owner and interpolation lint.

Two concerns:

1. **Behavior** — the typed owner must escape interpolations by default, pass
   `raw()` through verbatim, join arrays, and compose nested `safeHtml`
   results without double-escaping. We exercise its browser bundle via Node.

2. **Lint** — once a render function adopts `safeHtml`, future edits must
   not silently reintroduce a bare template-string sink for user/model
   content. The lint rule flags `insertAdjacentHTML(...,  `...${x}...` )`
   and `.outerHTML = `...${x}...`` style raw-template sinks in the
   chat-render hotspot files, steering devs to `safeHtml`.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

from tests._runtime_sections import (
    native_module_path,
    runtime_section_names,
    runtime_sections_dir,
)

ROOT = Path(__file__).resolve().parents[1]
OWNER = ROOT / 'frontend/src/html-safety.ts'
OWNER_JS = Path(native_module_path('.native/html-safety.js', OWNER))
JS_DIR = runtime_sections_dir()


def _node_available() -> bool:
    return shutil.which('node') is not None


# ── 1. Behavior of safeHtml (run the real JS in Node) ──

_HARNESS = r"""
const fs = require('fs');
(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));

const out = [];
function check(name, got, want) {
  out.push((String(got) === String(want) ? 'PASS ' : 'FAIL ') + name +
           (String(got) === String(want) ? '' : ` got=${JSON.stringify(String(got))} want=${JSON.stringify(String(want))}`));
}

// escapes by default
check('escape_default', safeHtml`<b>${'<script>'}</b>`, '<b>&lt;script&gt;</b>');
check('direct_escape_full_set', escapeHtml(`&<>"'`), '&amp;&lt;&gt;&quot;&#39;');
check('direct_falsy_compat', escapeHtml(0) === '' && escapeHtml(false) === '', true);
check('typed_text_preserves_false_and_zero',
  escapeHtmlText(false) === 'false' && escapeHtmlText(0) === '0', true);
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
    proc = subprocess.run(
        ['node', '-e', _HARNESS, str(OWNER_JS)],
        capture_output=True, text=True, timeout=30,
    )
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'safeHtml behavior failures:\n' + '\n'.join(fails)
    # Sanity: we actually ran the checks.
    assert output.count('PASS') == 12, f'expected 12 PASS lines, got:\n{output}'


# ── 2. The typed owner must be wired once at the composition boundary ──

def test_html_safety_owner_replaces_ordered_classic_sections():
    names = runtime_section_names()
    assert 'core/escape_html.js' not in names
    assert 'core/safe_html.js' not in names


def test_typed_features_have_no_registry_or_fallback_escape_policy():
    offenders = []
    for path in (ROOT / 'frontend/src/features').rglob('*.ts'):
        source = path.read_text(encoding='utf-8')
        if ('escapeHtml?:' in source
                or '.escapeHtml' in source
                or 'function escapeHtml(' in source
                or 'function escape(' in source):
            offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


def test_safe_html_is_not_a_raw_index_script():
    html = (ROOT / 'index.html').read_text(encoding='utf-8')
    assert 'static/js/core/safe_html.js' not in html
    assert '<!-- TOFU_APP_ASSETS -->' in html


# ── 3. Chat-render lint: no bare template-string HTML sinks ──

# Files that have adopted (or are the target for) safeHtml. New raw-template
# sinks of dynamic content here must go through safeHtml instead.
_GUARDED_FILES = [
    'ui/streaming_render.js',
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
