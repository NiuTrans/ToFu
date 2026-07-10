"""jsdom test for inline-math rendering in the arXiv candidate list.

arXiv titles and abstracts routinely contain LaTeX, e.g.
``Observation of the $\\Lambda_b^0 \\to J/\\psi\\Xi^- K^+$ ... decays``. The
Paper Reading-Mode candidate cards used to render the title with a bare
``escapeHtml()`` so the raw ``$…$`` TeX showed literally (the reported bug).

The fix is the shared helper ``_escWithInlineMath(text)``: it escapes prose but
typesets each ``$…$`` / ``\\(…\\)`` span with KaTeX, falling back to a
``math-pending`` code span when KaTeX has not loaded yet.

This loads the REAL shipped ``static/js/paper-reader.js`` under jsdom, stubs a
minimal ``katex.renderToString`` (mirrors the real .katex wrapper), and checks:
  • a title with inline math produces a ``.katex`` element (rendered, not raw);
  • the raw ``$`` delimiters and the TeX macro source do NOT appear as text;
  • surrounding prose is still escaped (``<`` → ``&lt;``, no HTML injection);
  • a plain title with no ``$`` is left as plain escaped text (no KaTeX call);
  • when KaTeX is absent the helper emits a ``math-pending`` fallback (so the
    katex:loaded repaint can upgrade it later).

Negative control (in-harness): re-rendering the title with the OLD behaviour
(plain escapeHtml) leaves the literal ``$`` in the output — proving the new
helper is load-bearing.

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


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
global.escapeHtml = win.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

// Minimal KaTeX stub: emit a .katex wrapper carrying the rendered TeX in a
// data attribute (mirrors the real .katex element the app looks for).
let _katexCalls = 0;
global.katex = win.katex = {
  renderToString: function(tex, opts) {
    _katexCalls++;
    return '<span class="katex" data-tex="' + escapeHtml(tex) + '">' +
             '<span class="katex-html">' + escapeHtml(tex) + '</span></span>';
  }
};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // paper-reader.js (real, shipped)

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

check('helper_exists', typeof _escWithInlineMath === 'function');

// ── Title with inline math (the reported case) ──
const title = 'Observation of the $\\Lambda_b^0 \\to J/\\psi\\Xi^- K^+$ decays';
const html = _escWithInlineMath(title);
const holder = document.createElement('div');
holder.innerHTML = html;
check('title_renders_katex', !!holder.querySelector('.katex'));
check('title_no_raw_dollar', holder.textContent.indexOf('$') === -1);
// The visible text must not carry the raw macro backslash source.
check('title_prose_escaped', html.indexOf('Observation of the ') !== -1);
check('katex_was_called', _katexCalls >= 1);

// ── Prose escaping / no HTML injection ──
const evil = 'A <script>x</script> title with $x^2$';
const evilHtml = _escWithInlineMath(evil);
check('prose_lt_escaped', evilHtml.indexOf('&lt;script&gt;') !== -1);
check('prose_no_live_script', evilHtml.indexOf('<script>') === -1);
check('evil_math_rendered', /class="katex"/.test(evilHtml));

// ── Plain title (no math) is left alone, no KaTeX call ──
const before = _katexCalls;
const plain = _escWithInlineMath('Measurement of the branching fraction');
check('plain_no_katex_call', _katexCalls === before);
check('plain_is_escaped_text', plain === 'Measurement of the branching fraction');

// ── Negative control: OLD behaviour (plain escapeHtml) leaks the $ ──
const naive = escapeHtml(title);
check('NC_plain_escape_leaks_dollar', naive.indexOf('$') !== -1 && !/katex/.test(naive));

// ── Fallback when KaTeX absent → math-pending span ──
const savedKatex = global.katex;
global.katex = win.katex = undefined;
// stub _ensureKatex so the helper's lazy-load call is a harmless no-op
global._ensureKatex = win._ensureKatex = function(){ return { then: function(){} }; };
const pendingHtml = _escWithInlineMath(title);
check('fallback_math_pending', pendingHtml.indexOf('math-pending') !== -1);
check('fallback_no_raw_dollar_delim',
      // the fallback wraps the TeX in a code span; the $ delimiters are gone
      (function(){ const d = document.createElement('div'); d.innerHTML = pendingHtml;
                   return d.querySelectorAll('.math-pending').length >= 1; })());
global.katex = win.katex = savedKatex;

console.log(out.join('\n'));
process.exit(0);
"""


# ── Harness 2: the katex:loaded repaint must not clobber a plain-search list
# when a STALE `_recStream` (from an earlier "describe" flow) is still around.
_CLOBBER_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="paperPdfViewer"></div></body>',
                      { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
global.requestAnimationFrame = win.requestAnimationFrame = function(fn){ return fn(); };
global.escapeHtml = win.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
global.t = win.t = (k) => k;
// KaTeX absent at first so the plain-search list paints math-pending fallback.
global.katex = win.katex = undefined;
global._ensureKatex = win._ensureKatex = function(){};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // paper-reader.js (real, shipped)

// Isolate the branch under test: neuter the unrelated report/QA repaint paths.
_renderPaperQA = win._renderPaperQA = function(){};
_paintReportFromState = win._paintReportFromState = function(){};

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

paperMode = true;

// (1) Screen currently shows a PLAIN arXiv search list (user switched back to
// title search after having used "describe").
const results = [{ title: 'Observation of $\\Lambda_b^0$ decays', arxiv_id: '2501.1',
                   authors: ['LHCb'], summary: 'A $J/\\psi$ study.' }];
_paperSearchResults = results;
_renderArxivSearchResults('lhcb decays', results);

const viewer = document.getElementById('paperPdfViewer');
check('pre_search_list_present', !!viewer.querySelector('.paper-search .paper-result-list'));
check('pre_no_rec_shell', !viewer.querySelector('[data-rec-shell]'));

// (2) A STALE recommend stream is still hanging around from an earlier flow.
const stale = _newRecStream('some old description');
stale.interpreted = true;
stale.candidateCount = 3;
stale.status = 'done';
_recStream = stale;

// (3) KaTeX finishes lazy-loading — fire the real listener.
global.katex = win.katex = {
  renderToString: (tex) => '<span class="katex" data-tex="' + escapeHtml(tex) + '"></span>',
};
win.dispatchEvent(new win.Event('katex:loaded'));

// (4) The search list must SURVIVE — no recommend shell may replace it.
check('post_search_list_survives', !!viewer.querySelector('.paper-search .paper-result-list'));
check('post_no_rec_shell_clobber', !viewer.querySelector('[data-rec-shell]'));
// And the plain-search repaint upgraded the math-pending spans to real KaTeX.
check('post_search_math_upgraded', !!viewer.querySelector('.paper-result-title .katex'));
check('post_search_no_math_pending', !viewer.querySelector('.math-pending'));

// (5) Negative control — the OLD buggy branch (keyed on `_recStream` alone,
// no DOM gate) called _paintRecommendFromState directly. Prove that WOULD
// clobber, so the DOM gate is load-bearing.
_paintRecommendFromState();
check('NC_ungated_recommend_clobbers',
      !!viewer.querySelector('[data-rec-shell]') &&
      !viewer.querySelector('.paper-search:not([data-rec-shell]) .paper-result-list'));

console.log(out.join('\n'));
process.exit(0);
"""


def _run_harness(src, min_pass, label, tmp_name):
    harness = os.path.join(HERE, tmp_name)
    with open(harness, 'w') as f:
        f.write(src)
    try:
        proc = subprocess.run(
            ['node', harness, os.path.join(JS_DIR, 'paper-reader.js'), ROOT],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed ({label}): {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, f'{label} failures:\n' + output
    assert output.count('PASS') >= min_pass, \
        f'{label}: expected >={min_pass} PASS lines, got:\n{output}'


def _run():
    _run_harness(_HARNESS, 13, 'arXiv title math', '_paper_arxiv_title_math_harness.js')


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_arxiv_title_inline_math_renders():
    _run()


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_katex_loaded_repaint_does_not_clobber_search_list():
    _run_harness(_CLOBBER_HARNESS, 7, 'katex:loaded clobber',
                 '_paper_arxiv_clobber_harness.js')


if __name__ == '__main__':
    if not _node_deps_available():
        print('SKIP: node + jsdom not available')
    else:
        _run()
        _run_harness(_CLOBBER_HARNESS, 7, 'katex:loaded clobber',
                     '_paper_arxiv_clobber_harness.js')
        print('PASS: arXiv title inline math renders + no clobber on katex:loaded')
