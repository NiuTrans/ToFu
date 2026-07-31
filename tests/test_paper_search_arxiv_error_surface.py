#!/usr/bin/env python3
"""arXiv title-search error transparency (lib/paper/arxiv.py +
routes/paper.py + static/js/paper/arxiv.js).

Regression for the 2026-07-28 live incident: the running server held a stale
``tofu_search.search.vertical.arxiv`` module (pre-``search_by_query``), every
``/api/v1/paper/search-arxiv`` call raised AttributeError → uncaught 500, and
the frontend (``onError:'null'`` → null → ``results = []``) rendered each one
as "no papers found". Three independent collapses made a loud outage look
like an empty result set:

  1. lib: ``search_arxiv`` returned ``[]`` for BOTH "query ran, matched
     nothing" and "every attempt failed" — indistinguishable downstream.
  2. route: no exception handling — any lib error became an uncaught 500.
  3. frontend: ``Api.paper.searchArxiv`` swallowed HTTP errors to ``null``,
     and ``_searchArxivPapers`` rendered ``null`` / falsy-ok as the empty
     result list.

Proven here (all network faked):

  * lib ``search_arxiv_explained`` — hits / no_matches / request_failed /
    unusable_query each produce the right ``(results, error)`` pair;
    ``search_arxiv`` keeps its list-only back-compat contract.
  * route — failure surfaces as a non-200 ``{ok:false, error}`` payload
    (502 upstream-failure / 400 built-syntax / 502 unexpected, logged), and
    a legit no-match still returns ``ok:true, results:[]`` (the complement —
    without it "everything is an error" would also pass).
  * frontend (jsdom, real shipped module) — a thrown ApiError-shaped
    failure and an ``ok:false`` payload both render the REAL reason text,
    never the no-results copy; a real empty result still renders the
    no-results copy (complement); a hit renders a card.
  * NEUTER×2 — amputate the ok-gate / the detail line from a COPY of the
    module and the corresponding probes flip to FAIL (the guards are
    load-bearing, not incidental).
  * static ratchet — ``Api.paper.searchArxiv`` must not regress to
    ``onError:'null'``.

Under pytest:  pytest tests/test_paper_search_arxiv_error_surface.py -m unit
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
ARXIV_JS = os.path.join(ROOT, 'static', 'js', 'paper', 'arxiv.js')

def _patch_ts_search(monkeypatch, fake):
    """Patch the shared arXiv vertical's search_by_query BY OBJECT — the
    dotted-string form breaks here: ``tofu_search.search`` resolves to a
    top-level FUNCTION, not the ``search`` subpackage, so pytest's attribute
    walk dies on ``.vertical``."""
    from tofu_search.search.vertical import arxiv as ts_arxiv
    monkeypatch.setattr(ts_arxiv, 'search_by_query', fake)


def _envelope(outcome, papers=(), error=''):
    return {'ok': outcome in ('hits', 'no_matches'),
            'query': 'all:q', 'mode': 'terms',
            'papers': list(papers), 'outcome': outcome, 'error': error}


def _paper(i):
    return {'arxiv_id': f'2301.0000{i}', 'title': f'Real Paper {i}',
            'authors': ['A. Author'], 'summary': 's', 'published': '2023-01-01',
            'primary_category': 'cs.CL', 'pdf_url': '', 'abs_url': ''}


# ══════════════════════════════════════════════════════════
#  lib: search_arxiv_explained keeps the failed-vs-empty distinction
# ══════════════════════════════════════════════════════════

def test_lib_hits_return_results_and_empty_error(monkeypatch):
    import lib.paper.arxiv as ax
    _patch_ts_search(monkeypatch,
                     lambda *a, **kw: _envelope('hits', [_paper(1), _paper(2)]))
    results, error = ax.search_arxiv_explained('real paper', max_results=5)
    assert error == ''
    assert [r['arxiv_id'] for r in results] == ['2301.00001', '2301.00002']


def test_lib_no_matches_is_empty_results_AND_empty_error(monkeypatch):
    """A query that ran clean and matched nothing is NOT an error — this is
    the complement case: without it "report everything as an error" passes."""
    import lib.paper.arxiv as ax
    _patch_ts_search(monkeypatch,
                     lambda *a, **kw: _envelope('no_matches'))
    results, error = ax.search_arxiv_explained('zzz-no-such-paper', max_results=5)
    assert results == []
    assert error == ''


def test_lib_request_failed_exhausts_to_error_not_silent_empty(monkeypatch):
    import lib.paper.arxiv as ax
    monkeypatch.setattr('time.sleep', lambda s: None)  # skip backoff
    _patch_ts_search(monkeypatch,
                     lambda *a, **kw: _envelope('request_failed', error='HTTP 429'))
    results, error = ax.search_arxiv_explained('q', max_results=5)
    assert results == []
    assert 'HTTP 429' in error


def test_lib_unusable_query_is_an_error_not_an_empty_result(monkeypatch):
    """Every term sanitized away → the request never ran. That is a failure
    to ASK, and must not render as "no papers matched"."""
    import lib.paper.arxiv as ax
    _patch_ts_search(monkeypatch,
                     lambda *a, **kw: _envelope('unusable_query'))
    results, error = ax.search_arxiv_explained('!!!', max_results=5)
    assert results == []
    assert error != ''


def test_lib_wrapper_back_compat(monkeypatch):
    """search_arxiv stays list-only for internal callers: failure → [] with
    no raise, hits → the bare list, built syntax still raises."""
    import lib.paper.arxiv as ax
    monkeypatch.setattr('time.sleep', lambda s: None)
    _patch_ts_search(monkeypatch,
                     lambda *a, **kw: _envelope('request_failed', error='HTTP 500'))
    assert ax.search_arxiv('q', max_results=5) == []
    _patch_ts_search(monkeypatch,
                     lambda *a, **kw: _envelope('hits', [_paper(1)]))
    assert len(ax.search_arxiv('real paper', max_results=5)) == 1
    with pytest.raises(ax.ArxivQuerySyntaxError):
        ax.search_arxiv('ti:attention AND all:"kv cache"')


# ══════════════════════════════════════════════════════════
#  route: failures surface as errors; a clean empty stays ok:true
# ══════════════════════════════════════════════════════════

def _patch_explained(monkeypatch, fn):
    import routes.paper as paper_routes
    monkeypatch.setattr(paper_routes, 'search_arxiv_explained', fn)


def test_route_success_returns_results(flask_client, monkeypatch):
    _patch_explained(monkeypatch, lambda q, n: ([_paper(1)], ''))
    r = flask_client.post('/api/v1/paper/search-arxiv', json={'query': 'real paper'})
    assert r.status_code == 200
    body = r.get_json()
    assert body['ok'] is True
    assert len(body['results']) == 1


def test_route_upstream_failure_is_a_502_with_the_reason(flask_client, monkeypatch):
    _patch_explained(monkeypatch, lambda q, n: ([], 'HTTP 429'))
    r = flask_client.post('/api/v1/paper/search-arxiv', json={'query': 'q'})
    assert r.status_code == 502
    body = r.get_json()
    assert body['ok'] is False
    assert 'HTTP 429' in body['error']


def test_route_clean_no_match_stays_ok_with_empty_results(flask_client, monkeypatch):
    """Complement: a legitimate zero must NOT be promoted to an error —
    otherwise "make everything a 502" would satisfy the failure probes."""
    _patch_explained(monkeypatch, lambda q, n: ([], ''))
    r = flask_client.post('/api/v1/paper/search-arxiv', json={'query': 'zzz'})
    assert r.status_code == 200
    body = r.get_json()
    assert body['ok'] is True
    assert body['results'] == []


def test_route_built_syntax_is_a_400_not_an_uncaught_500(flask_client, monkeypatch):
    import routes.paper as paper_routes

    def _raise(q, n):
        raise paper_routes.ArxivQuerySyntaxError('built syntax rejected')
    _patch_explained(monkeypatch, _raise)
    r = flask_client.post('/api/v1/paper/search-arxiv', json={'query': 'ti:x AND all:"y"'})
    assert r.status_code == 400
    assert r.get_json()['ok'] is False


def test_route_unexpected_exception_is_a_json_502_not_uncaught_500(flask_client, monkeypatch):
    """THE incident regression: a lib-side AttributeError (stale tofu_search)
    previously escaped as an uncaught 500 — and the frontend showed 'no
    papers found'. It must now be a JSON error carrying the real reason."""

    def _raise(q, n):
        raise AttributeError("module 'tofu_search.search.vertical.arxiv' has "
                             "no attribute 'search_by_query'")
    _patch_explained(monkeypatch, _raise)
    r = flask_client.post('/api/v1/paper/search-arxiv', json={'query': 'q'})
    assert r.status_code == 502
    body = r.get_json()
    assert body['ok'] is False
    assert 'search_by_query' in body['error']


# ══════════════════════════════════════════════════════════
#  frontend: the shipped module renders the real reason, never a fake empty
# ══════════════════════════════════════════════════════════

def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body><div id="paperPdfViewer"></div>' +
  '<input id="paperArxivUrl" value="attention is all you need"></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
win.debugLog = global.debugLog = () => {};
const T_MAP = {
  'paper.searching': 'SEARCHING_TEXT',
  'paper.searchNoResults': 'NO_RESULTS_TEXT',
  'paper.searchFailed': 'SEARCH_FAILED_TEXT',
  'paper.searchBack': 'BACK_TEXT',
  'paper.searchResultsTitle': 'RESULTS_TITLE_TEXT',
  'paper.searchResultsHint': 'RESULTS_HINT_TEXT',
};
win.t = global.t = (k) => T_MAP[k] || k;
// Shared state owned by paper-reader.js in the real bundle (read at runtime).
global._paperSearchResults = [];
global._lastArxivSearchQuery = '';

const CARD = { arxiv_id: '1706.03762', title: 'Attention Is All You Need',
  authors: ['A. Vaswani'], summary: 'The dominant sequence transduction models',
  published: '2017-06-12', primary_category: 'cs.CL', pdf_url: '', abs_url: '' };
const apiState = { mode: 'throw_string' };
global.Api = win.Api = { paper: {
  searchArxiv: async (q, n) => {
    if (apiState.mode === 'throw_string') {
      // Mimics api.js ApiError for a server string-error body: e.code IS the
      // server's message; e.message is the generic 'HTTP 502 on POST ...'.
      const e = new Error('HTTP 502 on POST /api/v1/paper/search-arxiv');
      e.code = 'arXiv search failed: HTTP 429';
      throw e;
    }
    if (apiState.mode === 'network') {
      const e = new Error('network error');
      e.code = 'network';
      throw e;
    }
    if (apiState.mode === 'ok_false') {
      return { ok: false, error: 'arXiv search failed: ReadTimeout' };
    }
    if (apiState.mode === 'ok_empty') return { ok: true, query: q, results: [] };
    if (apiState.mode === 'ok_hits') return { ok: true, query: q, results: [CARD] };
    throw new Error('bad mode: ' + apiState.mode);
  },
}};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // paper/arxiv.js (real, shipped)

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
function viewerHtml() { return document.getElementById('paperPdfViewer').innerHTML; }

(async () => {
  // ── Case A: thrown ApiError with the server string on .code ──
  apiState.mode = 'throw_string';
  await _searchArxivPapers('attention is all you need');
  check('thrown_error_shows_title', viewerHtml().includes('SEARCH_FAILED_TEXT'));
  check('thrown_error_shows_detail', viewerHtml().includes('arXiv search failed: HTTP 429'));
  check('thrown_error_NOT_no_results', !viewerHtml().includes('NO_RESULTS_TEXT'));

  // ── Case B: reachable server, explicit ok:false string error ──
  apiState.mode = 'ok_false';
  await _searchArxivPapers('attention is all you need');
  check('ok_false_shows_detail', viewerHtml().includes('arXiv search failed: ReadTimeout'));
  check('ok_false_NOT_no_results', !viewerHtml().includes('NO_RESULTS_TEXT'));

  // ── Case C: network failure — e.message is shown (code is just 'network') ──
  apiState.mode = 'network';
  await _searchArxivPapers('attention is all you need');
  check('network_error_shows_message', viewerHtml().includes('network error'));
  check('network_error_NOT_no_results', !viewerHtml().includes('NO_RESULTS_TEXT'));

  // ── Case D (complement): a REAL empty result still says "no results" ──
  apiState.mode = 'ok_empty';
  await _searchArxivPapers('zzz-no-such-paper');
  check('clean_empty_shows_no_results', viewerHtml().includes('NO_RESULTS_TEXT'));
  check('clean_empty_NOT_failure', !viewerHtml().includes('SEARCH_FAILED_TEXT'));

  // ── Case E: a hit renders a card ──
  apiState.mode = 'ok_hits';
  await _searchArxivPapers('attention is all you need');
  check('hit_renders_card', !!document.querySelector('.paper-result-card'));
  check('hit_renders_title', viewerHtml().includes('Attention Is All You Need'));

  console.log(out.join('\n'));
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""


def _run_harness(arxiv_js_path: str) -> subprocess.CompletedProcess:
    harness = os.path.join(HERE, '_paper_arxiv_search_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        return subprocess.run(
            ['node', harness, arxiv_js_path, ROOT],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_frontend_failure_never_renders_as_no_results():
    proc = _run_harness(ARXIV_JS)
    out = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{out}'
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'arxiv search error-surface failures:\n' + out
    assert out.count('PASS') >= 11, f'expected >=11 PASS lines, got:\n{out}'


def _neuter_copy(marker: str, replacement: str, tag: str) -> str:
    """Write a copy of the shipped module with ONE edit amputated. Fails hard
    (not silently green) when the marker no longer matches the source."""
    src = open(ARXIV_JS, encoding='utf-8').read()
    assert marker in src, \
        f'NEUTER marker not found in shipped arxiv.js — the guard is stale: {marker[:80]!r}'
    broken = src.replace(marker, replacement, 1)
    assert broken != src
    tmp = os.path.join(HERE, f'_paper_arxiv_neuter_{tag}.js')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(broken)
    return tmp


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NEUTER_ok_gate_is_loadbearing():
    """Amputate the ``!data.ok`` guard → an ok:false payload falls through to
    the empty-list renderer → the ok_false detail probe MUST flip to FAIL.
    Without the gate, a reachable-but-failed server prints "no papers
    found" — exactly the incident lie."""
    tmp = _neuter_copy('if (!data || !data.ok) {', 'if (false) {', 'gate')
    try:
        chk = subprocess.run(['node', '--check', tmp], capture_output=True,
                             text=True, timeout=30)
        assert chk.returncode == 0, f'patched JS invalid: {chk.stderr}'
        proc = _run_harness(tmp)
        out = proc.stdout.strip()
        assert proc.returncode == 0, f'node crashed: {proc.stderr}\n{out}'
        assert 'FAIL ok_false_shows_detail' in out, \
            'amputating the ok-gate did NOT flip the probe — gate non-load-bearing:\n' + out
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NEUTER_error_detail_line_is_loadbearing():
    """Amputate the detail line → a thrown failure shows only the generic
    title, never the real reason — the detail probe MUST flip to FAIL."""
    tmp = _neuter_copy(
        "(detail ? '<div class=\"paper-error-detail\">' + escapeHtml(detail) + '</div>' : '') +",
        "'' +", 'detail')
    try:
        chk = subprocess.run(['node', '--check', tmp], capture_output=True,
                             text=True, timeout=30)
        assert chk.returncode == 0, f'patched JS invalid: {chk.stderr}'
        proc = _run_harness(tmp)
        out = proc.stdout.strip()
        assert proc.returncode == 0, f'node crashed: {proc.stderr}\n{out}'
        assert 'FAIL thrown_error_shows_detail' in out, \
            'amputating the detail line did NOT flip the probe — line non-load-bearing:\n' + out
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# ══════════════════════════════════════════════════════════
#  static ratchets (no node required)
# ══════════════════════════════════════════════════════════

def test_static_api_searchArxiv_does_not_swallow_errors():
    """Ratchet: Api.paper.searchArxiv must not regress to onError:'null' —
    the swallow is what turned every HTTP failure into a fake empty list."""
    src = open(os.path.join(ROOT, 'static', 'js', 'api.js'), encoding='utf-8').read()
    m = re.search(r"searchArxiv:[\s\S]{0,300}?post\(([^)]*)\)", src)
    assert m, 'Api.paper.searchArxiv entry not found in api.js — guard is stale'
    assert "'null'" not in m.group(0) and 'onError' not in m.group(0), \
        'searchArxiv regressed to a swallowing onError mode — failures would ' \
        'render as "no papers found" again'


def test_static_js_syntax():
    if not shutil.which('node'):
        pytest.skip('node not installed')
    for path in (ARXIV_JS, os.path.join(ROOT, 'static', 'js', 'api.js')):
        proc = subprocess.run(['node', '--check', path],
                              capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, f'{os.path.basename(path)} syntax: {proc.stderr}'
