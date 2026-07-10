"""jsdom guard: a stored paper PDF URL is re-based onto the CURRENT proxy
base path before pdf.js loads it.

Regression (2026-07): under a cloud-IDE proxy the app is served from a base
path such as ``/proxy/15000/`` and every backend call goes through
``apiUrl()`` which prepends that ``BASE_PATH``. A paper library row persists
``pdfUrl`` with two different shapes depending on the writer: the server ingest
stores a ROOT-RELATIVE ``/api/paper/pdf/<f>`` (the backend can't know the proxy
prefix), while a client PUT stores an ``apiUrl()``-prefixed value baked with a
PARTICULAR session's ``BASE_PATH``.

The library RE-OPEN path used to assign ``_paperPdfUrl = entry.pdfUrl``
verbatim, so pdf.js fetched ``https://host/api/paper/pdf/...`` with NO
``/proxy/15000`` prefix → the request hit the gateway root, 404'd, and pdf.js
raised ``Missing PDF`` ("Failed to load PDF"). A baked prefix from an old
session is equally wrong if the port/prefix changes.

``_resolvePaperPdfUrl(url)`` fixes this at the single point of use inside
``_loadPaperPdf``: strip back to the canonical ``/api/...`` segment and
re-apply the live ``BASE_PATH`` via ``apiUrl()``. This guard loads the REAL
shipped ``static/js/paper-reader.js`` under jsdom with a stubbed
``apiUrl`` that mimics a ``/proxy/15000`` base path and asserts:
  • a root-relative stored URL gains the prefix;
  • an already-prefixed (baked) URL is not double-prefixed;
  • a ``blob:``/``data:`` URL is left untouched.

Negative-control (source-level): a COPY of paper-reader.js has
``_resolvePaperPdfUrl`` neutered to the identity function; the harness must
then FAIL the root-relative re-base check, proving the helper is load-bearing.
The shipped file is never modified.

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
PAPER_JS = os.path.join(JS_DIR, 'paper-reader.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: 'http://localhost/proxy/15000/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.localStorage = win.localStorage;
global.console = console;

// Mimic core.js under a /proxy/15000 base path.
const BASE_PATH = '/proxy/15000';
global.BASE_PATH = win.BASE_PATH = BASE_PATH;
global.apiUrl = win.apiUrl = (p) => BASE_PATH + p;

win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);
win.t = global.t = (k) => k;
win.Icon = global.Icon = () => '<svg></svg>';
win.debugLog = global.debugLog = () => {};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // paper-reader.js (real, shipped)

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Root-relative (server-ingest shape) → prefix restored.
check('root_relative_gets_prefix',
      _resolvePaperPdfUrl('/api/paper/pdf/arxiv_2606.30534v2.pdf')
        === '/proxy/15000/api/paper/pdf/arxiv_2606.30534v2.pdf');

// Already-prefixed (client-PUT shape) → not double-prefixed.
check('prefixed_not_doubled',
      _resolvePaperPdfUrl('/proxy/15000/api/paper/pdf/arxiv_2606.30534v2.pdf')
        === '/proxy/15000/api/paper/pdf/arxiv_2606.30534v2.pdf');

// A STALE baked prefix (different port) is re-based onto the live one.
check('stale_prefix_rebased',
      _resolvePaperPdfUrl('/proxy/9999/api/paper/pdf/x.pdf')
        === '/proxy/15000/api/paper/pdf/x.pdf');

// blob:/data: and empty → untouched.
check('blob_untouched', _resolvePaperPdfUrl('blob:http://x/abc') === 'blob:http://x/abc');
check('empty_untouched', _resolvePaperPdfUrl('') === '');

console.log(out.join('\n'));
process.exit(0);
"""


def _run_harness(paper_js: str) -> subprocess.CompletedProcess:
    harness = os.path.join(HERE, '_paper_pdf_url_rebase_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        return subprocess.run(
            ['node', harness, paper_js, ROOT],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_paper_pdf_url_rebased_onto_live_base_path():
    proc = _run_harness(PAPER_JS)
    out = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{out}'
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'paper PDF url re-base failures:\n' + out
    assert out.count('PASS') >= 5, f'expected >=5 PASS lines, got:\n{out}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_source_level_negative_control_identity_reintroduces_404():
    """Neuter ``_resolvePaperPdfUrl`` to the identity fn; prove the guard FAILS.

    A COPY of paper-reader.js has the helper body replaced with ``return url``
    (the pre-fix behaviour: hand the stored URL to pdf.js verbatim). The
    harness must then FAIL the root-relative re-base check, proving the helper
    is load-bearing. The shipped file is never modified.
    """
    src = open(PAPER_JS, encoding='utf-8').read()

    marker = "function _resolvePaperPdfUrl(url) {"
    assert marker in src, 'helper marker not found — test is stale, update the marker'
    broken = src.replace(
        marker,
        "function _resolvePaperPdfUrl(url) {\n  return url;  // NEUTERED",
        1,
    )
    assert broken != src, 'negative-control patch was a no-op'

    tmp = os.path.join(HERE, '_paper_reader_url_identity.js')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(broken)
    try:
        chk = subprocess.run(['node', '--check', tmp], capture_output=True, text=True, timeout=30)
        assert chk.returncode == 0, f'patched JS invalid: {chk.stderr}'
        proc = _run_harness(tmp)
        out = proc.stdout.strip()
        assert proc.returncode == 0, f'node crashed: {proc.stderr}\n{out}'
        assert 'FAIL root_relative_gets_prefix' in out, \
            'identity helper did NOT drop the prefix — guard is non-load-bearing:\n' + out
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    assert open(PAPER_JS, encoding='utf-8').read() == src, 'shipped file was modified!'


if __name__ == '__main__':
    test_paper_pdf_url_rebased_onto_live_base_path()
    print('positive: PASS')
    test_source_level_negative_control_identity_reintroduces_404()
    print('negative-control: PASS')
    print('ALL PASSED')
