"""Compiled arXiv ingest owner fences races, errors and session teardown."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from tests._esm_feature_harness import compile_feature_owner


pytestmark = pytest.mark.unit
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
SOURCE = os.path.join(
    ROOT, 'frontend', 'src', 'features', 'paper', 'arxiv-fetch.ts')
ESBUILD = os.path.join(ROOT, 'scripts', 'vite_test_bundle.mjs')


_HARNESS = r"""
const fs = require('fs'), path = require('path');
const ROOT = process.argv[1], BUILT = process.argv[2];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><input id="paperArxivUrl">' +
  '<div id="paperPdfViewer"></div></body>', { url:'http://localhost/' });
global.window = global;
global.document = dom.window.document;
global.escapeHtml = (value) => String(value == null ? '' : value);
global.apiUrl = (url) => '/base' + url;
global._paperLibrary = [{ id:'saved', title:'Saved recommendation' }];
global._activePaperId = '';
let nextId = 0, releaseA, releaseC;
const calls = { created:[], loaded:[], saved:0, rendered:0, active:[], debug:[] };
global._newPaperEntryId = () => 'new-' + (++nextId);
global._createPaperEntry = (title, pdfUrl, parsed, arxivId, id) => {
  calls.created.push({ title, pdfUrl, parsed, arxivId, id });
  const existing = _paperLibrary.find((row) => row.id === id);
  if (existing) Object.assign(existing, { title, pdfUrl, parsed, arxivId });
  else _paperLibrary.push({ id, title, pdfUrl, parsed, arxivId });
  _activePaperId = id;
};
global._updatePaperTitles = () => {};
global._renderPaperLibrary = () => { calls.rendered++; };
global._loadPaperPdf = async (url) => { calls.loaded.push(url); };
global._saveActivePaperState = async () => { calls.saved++; };
global._setActivePaperId = (id) => { _activePaperId = id; calls.active.push(id); };
global._renderArxivFetchProgress = () => {};
global.debugLog = (message, level) => { calls.debug.push([message, level]); };
function streamBody(tag) {
  let gate = Promise.resolve();
  if (tag === 'A') gate = new Promise((resolve) => { releaseA = resolve; });
  if (tag === 'C') gate = new Promise((resolve) => { releaseC = resolve; });
  const bytes = new TextEncoder().encode(
    'data: ' + JSON.stringify(done(tag)) + '\n\n',
  );
  let sent = false;
  return { getReader: () => ({ read: async () => {
    if (sent) return { done:true, value:undefined };
    await gate;
    sent = true;
    return { done:false, value:bytes };
  } }) };
}
global.Api = { paper: { fetchArxivStream: async (reference) => {
  if (reference === 'fail') return { ok:false, status:502, body:null,
    json:async () => ({ error:'upstream timeout' }) };
  return { ok:true, status:200, body:streamBody(reference) };
}}};
function done(tag) {
  return { stage:'done', arxiv_id:tag, title:'Title ' + tag,
    pdf_url:'/api/paper/pdf/' + tag + '.pdf', parsed_text:'text ' + tag,
    total_pages:3, paper_hash:'hash-' + tag, images:[{page:1}], text_length:6 };
}
(0, eval)(fs.readFileSync(BUILT, 'utf8'));

(async () => {
  const first = _fetchArxivPaper('A');
  await new Promise((resolve) => setTimeout(resolve, 0));
  await _fetchArxivPaper('B');
  releaseA(); await first;
  const race = { arxiv:_paperArxivId, title:_paperFileName, hash:_paperHash,
    url:_paperPdfUrl, active:_activePaperId, loaded:[...calls.loaded],
    created:[...calls.created] };

  await _fetchArxivPaper('fail', 'saved');
  const reuseSurvived = _paperLibrary.some((row) => row.id === 'saved');
  const errorHtml = document.getElementById('paperPdfViewer').innerHTML;

  const beforeDestroy = { arxiv:_paperArxivId, created:calls.created.length };
  const third = _fetchArxivPaper('C');
  await new Promise((resolve) => setTimeout(resolve, 0));
  _destroyArxivFetch(); releaseC(); await third;
  console.log(JSON.stringify({ race, reuseSurvived,
    errorReason:errorHtml.includes('upstream timeout'),
    destroyedFenced:_paperArxivId === beforeDestroy.arxiv &&
      calls.created.length === beforeDestroy.created && _paperLoading === false,
    saved:calls.saved }));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""


@pytest.mark.skipif(
    not shutil.which('node') or not os.path.isfile(ESBUILD)
    or not os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom')),
    reason='node/jsdom/esbuild dev dependencies required',
)
def test_compiled_arxiv_fetch_owner_is_latest_wins_and_destroyable(tmp_path):
    built = tmp_path / 'arxiv-fetch.js'
    compiled = compile_feature_owner(ESBUILD, SOURCE, built, tmp_path)
    assert compiled.returncode == 0, compiled.stderr
    process = subprocess.run(
        ['node', '-e', _HARNESS, ROOT, str(built)], capture_output=True,
        text=True, timeout=60)
    assert process.returncode == 0, process.stderr + process.stdout
    result = json.loads(process.stdout.strip().splitlines()[-1])
    race = result['race']
    assert race['arxiv'] == 'B' and race['title'] == 'Title B'
    assert race['hash'] == 'hash-B' and race['active'] == 'new-2'
    assert race['url'] == '/base/api/paper/pdf/B.pdf'
    assert race['loaded'] == ['/base/api/paper/pdf/B.pdf']
    assert [row['arxivId'] for row in race['created']] == ['B']
    assert result['reuseSurvived'] and result['errorReason']
    assert result['destroyedFenced']
    assert result['saved'] == 1
