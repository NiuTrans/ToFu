"""Compiled native arXiv title search preserves routing, math and errors."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from tests._esm_feature_harness import compile_feature_owner


pytestmark = pytest.mark.unit
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..'))
SOURCE = os.path.join(
    ROOT, 'frontend', 'src', 'features', 'paper', 'arxiv-search.ts')
ESBUILD = os.path.join(ROOT, 'scripts', 'vite_test_bundle.mjs')


_HARNESS = r"""
const fs = require('fs'), path = require('path');
const ROOT = process.argv[1], BUILT = process.argv[2];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><input id="paperArxivUrl">' +
  '<div id="paperPdfViewer"></div></body>', { url: 'http://localhost/' });
global.window = global;
global.document = dom.window.document;
global.escapeHtml = (value) => String(value == null ? '' : value)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;');
global.t = (key) => ({
  'paper.searching': 'SEARCHING', 'paper.searchFailed': 'FAILED',
  'paper.searchNoResults': 'EMPTY', 'paper.searchBack': 'BACK',
  'paper.searchResultsTitle': 'RESULTS', 'paper.searchResultsHint': 'HINT',
}[key] || key);
global.debugLog = () => {};
const calls = { search: [], fetch: [] };
let mode = 'hits';
const card = { arxiv_id: '1706.03762', title: 'Attention $x^2$',
  authors: ['A'], summary: 'Summary $y$', primary_category: 'cs.CL' };
global.Api = { paper: { searchArxiv: async (query, limit) => {
  calls.search.push([query, limit]);
  if (mode === 'throw') { const error = new Error('HTTP 502');
    error.code = 'arXiv upstream HTTP 429'; throw error; }
  if (mode === 'failed') return { ok: false, error: 'ReadTimeout' };
  if (mode === 'empty') return { ok: true, results: [] };
  return { ok: true, results: [card] };
}}};
global._fetchArxivPaper = (reference) => { calls.fetch.push(reference); };
global.katex = { renderToString: (tex) => '<span class="katex">' + tex + '</span>' };

(0, eval)(fs.readFileSync(BUILT, 'utf8'));
const viewer = document.getElementById('paperPdfViewer');

(async () => {
  const input = document.getElementById('paperArxivUrl');
  input.value = '2301.12345v2';
  _submitArxivQuery();
  input.value = 'attention architecture';
  _submitArxivQuery();
  // A direct PDF link (e.g. a Hugging Face resolve URL) must route to the
  // SAME server fetch stream as an arXiv ref — never to title search.
  input.value = 'https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash/resolve/main/DeepSeek_V41_Tech_Report.pdf';
  _submitArxivQuery();
  await new Promise((resolve) => setTimeout(resolve, 0));
  // Snapshot BEFORE the harness's own direct _searchArxivPapers calls below:
  // three submit queries (arxiv ref, title, PDF link) must yield exactly ONE
  // title search — the PDF link routes to fetch, never to search.
  const searchesAfterSubmits = calls.search.length;
  const hitHtml = viewer.innerHTML;

  _openArxivResult(0);

  mode = 'throw'; await _searchArxivPapers('broken');
  const thrownHtml = viewer.innerHTML;
  mode = 'failed'; await _searchArxivPapers('failed');
  const failedHtml = viewer.innerHTML;
  mode = 'empty'; await _searchArxivPapers('empty');
  const emptyHtml = viewer.innerHTML;
  global.katex = undefined;
  const pending = _escWithInlineMath('A $z$ title');

  console.log(JSON.stringify({ calls, searchesAfterSubmits, direct: _looksLikeArxivRef('hep-th/0601001v2'),
    queryNotDirect: !_looksLikeArxivRef('attention architecture'),
    pdfDirect: _looksLikePdfUrl('https://example.org/x/paper.pdf'),
    pdfQueryDirect: _looksLikePdfUrl('https://example.org/a.pdf?v=2#frag'),
    pdfNotDirect: !_looksLikePdfUrl('https://example.org/abs/123') &&
      !_looksLikePdfUrl('attention architecture'),
    hitCard: hitHtml.includes('paper-result-card'),
    hitMath: hitHtml.includes('class="katex"'),
    openUsesStoredResult: calls.fetch.includes('1706.03762'),
    thrownReason: thrownHtml.includes('arXiv upstream HTTP 429') &&
      !thrownHtml.includes('EMPTY'),
    envelopeReason: failedHtml.includes('ReadTimeout') && !failedHtml.includes('EMPTY'),
    cleanEmpty: emptyHtml.includes('EMPTY') && !emptyHtml.includes('FAILED'),
    pending: pending.includes('math-pending') }));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""


@pytest.mark.skipif(
    not shutil.which('node') or not os.path.isfile(ESBUILD)
    or not os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom')),
    reason='node/jsdom/esbuild dev dependencies required',
)
def test_compiled_arxiv_search_owner_preserves_browser_contract(tmp_path):
    built = tmp_path / 'arxiv-search.js'
    compiled = compile_feature_owner(ESBUILD, SOURCE, built, tmp_path)
    assert compiled.returncode == 0, compiled.stderr
    process = subprocess.run(
        ['node', '-e', _HARNESS, ROOT, str(built)], capture_output=True,
        text=True, timeout=60)
    assert process.returncode == 0, process.stderr + process.stdout
    result = json.loads(process.stdout.strip().splitlines()[-1])
    assert result['direct'] and result['queryNotDirect']
    assert result['calls']['fetch'][0] == '2301.12345v2'
    assert result['calls']['search'][0] == ['attention architecture', 12]
    # Direct PDF link routed to the fetch stream, not to title search.
    assert result['pdfDirect'] and result['pdfQueryDirect'] and result['pdfNotDirect']
    assert result['calls']['fetch'][1].endswith('DeepSeek_V41_Tech_Report.pdf')
    assert result['searchesAfterSubmits'] == 1, result['calls']['search']
    assert result['hitCard'] and result['hitMath']
    assert result['openUsesStoredResult']
    assert result['thrownReason'] and result['envelopeReason']
    assert result['cleanEmpty'] and result['pending']
