"""Compiled Paper recommendation owner: replay, rendering and teardown."""

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
    ROOT, 'frontend', 'src', 'features', 'paper', 'recommend.ts')
ESBUILD = os.path.join(ROOT, 'node_modules', '.bin', 'esbuild')


_HARNESS = r"""
const fs = require('fs'), path = require('path');
const ROOT = process.argv[1], BUILT = process.argv[2];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><textarea id="paperDescribeInput"></textarea>' +
  '<div id="paperPdfViewer"></div></body>', { url: 'http://localhost/' });
global.window = global;
global.document = dom.window.document;
global.requestAnimationFrame = (callback) => { callback(Date.now()); return 1; };
global.escapeHtml = (value) => String(value == null ? '' : value)
  .replace(/[&<>"']/g, (char) => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;',
    '"':'&quot;', "'":'&#39;' }[char]));
global.t = (key) => ({
  'paper.recommendTitle':'Recommended', 'paper.recommendHint':'Verified',
  'paper.recommendInterpreting':'Interpreting',
  'paper.recommendResearching':'Research {n}',
  'paper.recommendGrounding':'Grounding {n}/{total}',
  'paper.recommendNoResults':'No results', 'paper.searchBack':'Back',
  'paper.correctionActual':'Actual', 'paper.correctionTitle':'Correction',
}[key] || key);
global._escWithInlineMath = (value) => global.escapeHtml(value);
global.debugLog = () => {};
global.renderToolRoundsHTML = (rounds) => '<div data-tools>' + rounds.length + '</div>';

const cards = [
  { arxiv_id:'2501.00001', title:'Paper A', why:'match A' },
  { arxiv_id:'2501.00002', title:'Paper B', why:'match B' },
];
const subscriptions = [];
global.pushSubscribe = (channel, taskId, handler) => {
  subscriptions.push({ channel, taskId, handler });
};
global.pushUnsubscribe = (channel, taskId, handler) => {
  const index = subscriptions.findIndex((row) => row.channel === channel &&
    row.taskId === taskId && row.handler === handler);
  if (index >= 0) subscriptions.splice(index, 1);
};
const persisted = new Set(), aborts = [], fetches = [];
global._persistRecommendedCard = (card) => {
  if (card && card.arxiv_id) persisted.add(card.arxiv_id);
};
global._findLibraryEntryByArxiv = (id) => ({ id:'saved-' + id });
global._fetchArxivPaper = (id, reuseId) => { fetches.push([id, reuseId]); };
global.Api = { paper: {
  recommendStart: async () => ({ ok:true, task_id:'rec-1' }),
  recommendAbort: async (taskId) => { aborts.push(taskId); },
  recommendPoll: async (_taskId, cursor) => {
    subscriptions[0].handler({ seq:0, type:'candidate', index:0, card:cards[0] });
    return { ok:true, status:200, json:async () => ({ ok:true, status:'done',
      events:[
        { seq:0, type:'candidate', index:0, card:cards[0] },
        { seq:1, type:'candidate', index:1, card:cards[1] },
      ], next_cursor:2, results:cards, correction:null }) };
  },
}};

(0, eval)(fs.readFileSync(BUILT, 'utf8'));
const viewer = document.getElementById('paperPdfViewer');

(async () => {
  // Incremental DOM: two skeletons → one grounded, with subtree identity kept.
  const visual = _newRecStream('visual');
  visual.status = 'running'; __setFeatureService('_recStream', visual);
  _applyRecommendEvent(visual, { type:'interpret_done', candidateCount:2 });
  _paintRecommendNow();
  const skeletons = viewer.querySelectorAll('[data-status="searching"]').length;
  _applyRecommendEvent(visual, { type:'candidate', index:0, card:cards[0] });
  _paintRecommendNow();
  const node = viewer.querySelector('[data-status="grounded"]');
  const child = node && node.firstChild;
  _paintRecommendNow();
  const identityKept = !!node && node.firstChild === child;
  const oneGrounded = viewer.querySelectorAll('[data-status="grounded"]').length === 1 &&
    viewer.querySelectorAll('[data-status="searching"]').length === 1;

  await _recommendPapers('long context');
  const stream = global._recStream;
  _paintRecommendNow();
  _openRecommendResult(1);
  const pipeline = { status:stream.status, cursor:stream.cursor,
    results:stream.results.length, subscriptions:subscriptions.length,
    persisted:[...persisted].sort(), grounded:
      viewer.querySelectorAll('[data-status="grounded"]').length };

  const running = _newRecStream('stop'); running.taskId='rec-stop';
  running.status='running'; __setFeatureService('_recStream', running);
  _destroyPaperRecommend();
  await new Promise((resolve) => setTimeout(resolve, 0));

  console.log(JSON.stringify({ skeletons, oneGrounded, identityKept, pipeline,
    fetches, aborts, destroyedAborted:running.aborted }));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""


@pytest.mark.skipif(
    not shutil.which('node') or not os.path.isfile(ESBUILD)
    or not os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom')),
    reason='node/jsdom/esbuild dev dependencies required',
)
def test_compiled_recommend_owner_folds_push_poll_and_reconciles_dom(tmp_path):
    built = tmp_path / 'paper-recommend.js'
    compiled = compile_feature_owner(ESBUILD, SOURCE, built, tmp_path)
    assert compiled.returncode == 0, compiled.stderr
    process = subprocess.run(
        ['node', '-e', _HARNESS, ROOT, str(built)], capture_output=True,
        text=True, timeout=60)
    assert process.returncode == 0, process.stderr + process.stdout
    result = json.loads(process.stdout.strip().splitlines()[-1])
    assert result['skeletons'] == 2
    assert result['oneGrounded'] and result['identityKept']
    pipeline = result['pipeline']
    assert pipeline == {
        'status': 'done', 'cursor': 2, 'results': 2, 'subscriptions': 0,
        'persisted': ['2501.00001', '2501.00002'], 'grounded': 2,
    }
    assert result['fetches'] == [['2501.00002', 'saved-2501.00002']]
    assert result['aborts'] == ['rec-stop']
    assert result['destroyedAborted'] is True
