"""jsdom regression: the describe-to-recommend RESEARCH phase renders through
the SAME inline tool timeline chatInner uses — not a bespoke one-line counter.

WHY
The recommend interpretation agent runs the shared tofu tool loop
(``run_agent_loop`` + ``_execute_report_tool``) and emits chat-compatible
``tool_start`` / ``tool_done`` events, exactly like the report / review tabs.
The report tab already accumulates those into ``s.toolRounds`` and renders them
with ``renderToolRoundsHTML`` (the unified chatInner tool timeline). The
recommend tab used to THROW THEM AWAY into a ``researchCount`` counter, so the
user only saw "Researching current literature (search 5)…" with no idea what
was searched or found — the exact "mini-branch" rendering the owner flagged.

This suite drives the REAL shipped reconciler under jsdom and proves that:
  1. ``_applyRecommendEvent`` accumulates ``tool_start`` / ``tool_done`` into
     ``s.toolRounds`` with the chat-compatible round shape.
  2. ``_paintRecommendNow`` feeds those rounds to ``renderToolRoundsHTML`` (the
     SAME renderer the report tab uses) and mounts the output in the
     ``[data-rec-tools]`` container.

``renderToolRoundsHTML`` is STUBBED to a recognizable marker that echoes the
rounds it was handed — so the assertion is specifically "the recommend flow
routes its research through the unified renderer", independent of the chat
renderer's (heavy) internals.

NEUTER (in a COPY; shipped file byte-identical after): drop the
``s.toolRounds.push(...)`` in the ``tool_start`` branch → the timeline stays
empty even though research events arrived → the assertion fails.

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_READER_SRC = os.path.join(
    ROOT, 'frontend', 'src', 'features', 'paper', 'recommend.ts')
ESBUILD = os.path.join(ROOT, 'node_modules', '.bin', 'esbuild')


def _node_deps_available():
    if not shutil.which('node'):
        return False
    return (os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))
            and os.path.isfile(ESBUILD))


_DOM = r'''<!DOCTYPE html><body>
<div id="paperPdfViewer"></div>
</body>'''


def _harness():
    return r'''
const fs = require('fs');
const path = require('path');
const READER = process.argv[1];
const ROOT = process.argv[2];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(DOM_PLACEHOLDER, { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;
global.localStorage = win.localStorage;
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => { fn(Date.now()); return 1; };
win.matchMedia = win.matchMedia || ((q) => ({ matches: false, media: q,
  addEventListener(){}, removeEventListener(){}, addListener(){}, removeListener(){} }));
global.setTimeout = (fn) => { if (typeof fn === 'function') fn(); return 0; };
global.clearTimeout = () => {};
global.setInterval = () => 0; global.clearInterval = () => {};
win.t = global.t = (k, f) => {
  const M = {
    'paper.recommendTitle': 'Recommended papers',
    'paper.recommendHint': 'Each is verified on arXiv — click to load',
    'paper.recommendInterpreting': 'Interpreting your description…',
    'paper.recommendResearching': 'Researching current literature (search {n})…',
    'paper.recommendGrounding': 'Verifying against arXiv ({n}/{total})…',
    'paper.recommendNoResults': 'Could not verify a matching paper on arXiv.',
    'paper.searchBack': 'Back',
  };
  return M[k] || f || k;
};
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
win.debugLog = global.debugLog = () => {};
win.Api = global.Api = { paper: {} };
win._openRecommendResult = global._openRecommendResult = () => {};
win._openRecommendCorrection = global._openRecommendCorrection = () => {};
win._showPaperLanding = global._showPaperLanding = () => {};

// STUB the unified renderer with a recognizable marker that echoes the rounds
// it was handed. This proves the recommend flow routes its research through
// renderToolRoundsHTML (the SAME seam the report tab uses), not a bespoke
// counter — without pulling in the chat renderer's heavy internals.
let _rtrCalls = [];
win.renderToolRoundsHTML = global.renderToolRoundsHTML = (rounds, streaming) => {
  _rtrCalls.push({ n: rounds ? rounds.length : 0, streaming: !!streaming,
                   queries: (rounds || []).map(r => r.query),
                   statuses: (rounds || []).map(r => r.status) });
  return '<div class="ptool-panel" data-marker="unified">' +
    (rounds || []).map(r =>
      '<div class="ptool-row" data-tool="' + win.escapeHtml(r.toolName) + '"' +
      ' data-status="' + win.escapeHtml(r.status) + '">' +
      win.escapeHtml(String(r.query)) + '</div>').join('') +
    '</div>';
};

eval(fs.readFileSync(READER, 'utf8'));

(function main() {
const applyEv = win._applyRecommendEvent;
const paint = win._paintRecommendNow;
const newStream = win._newRecStream;
if (typeof applyEv !== 'function' || typeof paint !== 'function' || typeof newStream !== 'function') {
  console.log('__RESULT__' + JSON.stringify({ _missing: {
    applyEv: typeof applyEv, paint: typeof paint, newStream: typeof newStream } }));
  return;
}

const viewer = win.document.getElementById('paperPdfViewer');
const out = {};

const s = newStream('anthropic global workspace language models');
win._recStream = s;
s.status = 'running';

// ── Research round 1 STARTS: tool_start(web_search) ──
applyEv(s, { type: 'tool_start', roundNum: 1, toolName: 'web_search',
             query: 'arXiv "global workspace" language models',
             toolCallId: 'tc1', toolArgs: '{"queries":[{"query":"..."}]}' });
paint();
out.roundsAfterStart = s.toolRounds.length;
out.round1Searching = s.toolRounds.length === 1 && !!s.toolRounds[0] && s.toolRounds[0].status === 'searching';
let panel = viewer.querySelector('[data-rec-tools] .ptool-panel[data-marker="unified"]');
out.timelineMountedWhileSearching = !!panel;
out.timelineShowsQuery = !!panel &&
  panel.textContent.indexOf('global workspace') !== -1;
out.streamingFlagWhileRunning = _rtrCalls.length > 0 &&
  _rtrCalls[_rtrCalls.length - 1].streaming === true;

// ── Research round 1 DONE: tool_done carries results + elapsed ──
applyEv(s, { type: 'tool_done', roundNum: 1, toolName: 'web_search',
             elapsed: 3.4, results: [{ url: 'https://arxiv.org/abs/2103.00001', title: 'X' }],
             toolContent: 'found 5 results' });
paint();
out.round1Done = s.toolRounds.length === 1 && !!s.toolRounds[0] && s.toolRounds[0].status === 'done';
out.round1Elapsed = s.toolRounds[0] ? s.toolRounds[0]._elapsed : null;
out.round1HasResults = !!s.toolRounds[0] && Array.isArray(s.toolRounds[0].results) &&
  s.toolRounds[0].results.length === 1;
panel = viewer.querySelector('[data-rec-tools] .ptool-panel[data-marker="unified"]');
out.doneRowRendered = !!panel &&
  !!panel.querySelector('.ptool-row[data-status="done"]');

// ── Second research round, then interpret_done (research complete) ──
applyEv(s, { type: 'tool_start', roundNum: 2, toolName: 'fetch_url',
             query: 'https://arxiv.org/abs/2103.00001', toolCallId: 'tc2' });
applyEv(s, { type: 'tool_done', roundNum: 2, toolName: 'fetch_url', elapsed: 1.1 });
applyEv(s, { type: 'interpret_done', query: s.description, candidateCount: 2, correctionPending: false });
paint();
out.twoRoundsRendered = s.toolRounds.length === 2;
panel = viewer.querySelector('[data-rec-tools] .ptool-panel[data-marker="unified"]');
out.timelinePersistsAfterInterpret = !!panel &&
  panel.querySelectorAll('.ptool-row').length === 2;
// The renderer must have been handed BOTH rounds on the last call.
const last = _rtrCalls[_rtrCalls.length - 1];
out.lastCallHadTwoRounds = !!last && last.n === 2;
out.lastCallQueries = last ? last.queries : [];

console.log('__RESULT__' + JSON.stringify(out));
})();
'''.replace('DOM_PLACEHOLDER', json.dumps(_DOM))


def _run(reader=_READER_SRC):
    with tempfile.TemporaryDirectory(prefix='tofu-recommend-timeline-') as temp_dir:
        built = os.path.join(temp_dir, 'recommend.js')
        with open(reader, encoding='utf-8') as source_file:
            source = source_file.read()
        dependency = os.path.join(
            os.path.dirname(_READER_SRC), 'push-transport.ts').replace('\\', '/')
        source = source.replace(
            "from './push-transport';", f'from {json.dumps(dependency)};')
        compiled = subprocess.run(
            [ESBUILD, '--bundle', '--format=iife', '--platform=browser',
             '--loader=ts', '--sourcefile=recommend.ts',
             f'--outfile={built}'], input=source, capture_output=True,
            text=True, timeout=60)
        if compiled.returncode != 0:
            raise AssertionError(f'esbuild failed: {compiled.stderr or compiled.stdout}')
        proc = subprocess.run(
            ['node', '-e', _harness(), built, ROOT],
            capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(f'harness failed: {proc.stderr or proc.stdout}')
    for line in proc.stdout.splitlines():
        if line.startswith('__RESULT__'):
            res = json.loads(line[len('__RESULT__'):])
            if res.get('_missing'):
                raise AssertionError(f'reconciler seam not exposed: {res["_missing"]}')
            return res
    raise AssertionError(f'no result line: {proc.stdout}\n{proc.stderr}')


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_recommend_research_renders_through_unified_tool_timeline():
    out = _run()
    # tool_start accumulates a chat-compatible round and paints it immediately.
    assert out['roundsAfterStart'] == 1, f'tool_start must push a round: {out}'
    assert out['round1Searching'] is True, f'round must start data-status=searching: {out}'
    assert out['timelineMountedWhileSearching'] is True, \
        f'the unified tool timeline must mount WHILE researching (not only at done): {out}'
    assert out['timelineShowsQuery'] is True, \
        f'the timeline must show the actual research query, not just a counter: {out}'
    assert out['streamingFlagWhileRunning'] is True, \
        f'renderToolRoundsHTML must be told isStreaming=true while the task runs: {out}'
    # tool_done flips the round + carries results/elapsed (same shape as report).
    assert out['round1Done'] is True, f'tool_done must flip the round to done: {out}'
    assert out['round1Elapsed'] == '3.4s', f'elapsed must be recorded: {out}'
    assert out['round1HasResults'] is True, f'tool_done results must attach to the round: {out}'
    assert out['doneRowRendered'] is True, f'the done round must re-render in the timeline: {out}'
    # Multiple rounds + timeline persists past interpret_done.
    assert out['twoRoundsRendered'] is True, out
    assert out['timelinePersistsAfterInterpret'] is True, \
        f'the research timeline must stay visible after interpretation completes: {out}'
    assert out['lastCallHadTwoRounds'] is True, \
        f'renderToolRoundsHTML must receive ALL accumulated rounds: {out}'
    assert 'https://arxiv.org/abs/2103.00001' in out['lastCallQueries'], \
        f'the fetch_url round query must reach the renderer: {out}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_tool_round_accumulation_is_load_bearing(tmp_path):
    """NC: drop the ``s.toolRounds.push(...)`` in the ``tool_start`` branch →
    research events arrive but nothing is accumulated → the timeline is empty.
    Proves the accumulation is what drives the unified render. Shipped file
    byte-identical after."""
    with open(_READER_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = (
        "      stream.researchCount += 1;\n"
        "      stream.researchLabel = typeof event.query === 'string'\n"
        "        ? event.query.slice(0, 80) : '';\n"
        "      stream.toolRounds.push({")
    assert anchor in original, 'tool_start accumulation anchor not found — update the neuter target'
    patched = original.replace(
        anchor,
        ("      stream.researchCount += 1;\n"
         "      stream.researchLabel = typeof event.query === 'string'\n"
         "        ? event.query.slice(0, 80) : '';\n"
         "      if (false) stream.toolRounds.push({"),
        1)
    assert patched != original, 'NC patch did not apply'
    src = os.path.join(tmp_path, 'paper-recommend-nc.ts')
    with open(src, 'w', encoding='utf-8') as f:
        f.write(patched)
    out = _run(reader=src)
    assert out['roundsAfterStart'] == 0, \
        f'NC: without the push, tool_start must NOT accumulate a round: {out}'
    assert out['timelineMountedWhileSearching'] is False, \
        f'NC: with no accumulated rounds the unified timeline must NOT mount: {out}'
    with open(_READER_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped paper-reader.js must be byte-identical after NC'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
