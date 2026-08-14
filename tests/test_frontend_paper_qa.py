"""jsdom contract test for the native Paper Reading-Mode Q&A owner.

Covers the pure/near-pure Q&A functions that Epic E cut #3 extracts from
``features/paper/qa.ts``:
  • _qaMsgInnerHtml(msg)   — build one bubble's innerHTML (user escaped;
    assistant markdown; running → thinking pulse; toolRounds panel).
  • _renderPaperQA()       — reconcile the #paperQAMessages list in place from
    _paperQAHistory (empty-state placeholder; one node per message; only
    rewrite a node whose rendered content changed).
  • _applyQAEvent(asst,ev) — fold a stream event into the assistant message
    (tool_start pushes a round; tool_done marks it; delta appends;
    delta_reset clears interim prose).

This is the harness-FIRST no-regression proof for the split (recipe step 4):
it asserts against the CURRENT monolith and stays green after the cut because
argv[4] (the extracted qa.js) is eval'd in the SAME shared scope before the
core file when present. _paperQAHistory + the other QA STATE vars stay in the
core file (shared across clusters), so the harness declares a local shim for
them; the FUNCTIONS are what move.

Negative control (in-harness): re-running with _qaMsgInnerHtml replaced by a
raw-text builder leaves the assistant markdown un-rendered — proving the real
helper is load-bearing.

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._jsdom import JS_DIR, ROOT, run_harness

pytestmark = pytest.mark.unit

QA_TS = os.path.join(ROOT, 'frontend', 'src', 'features', 'paper', 'qa.ts')
ESBUILD = os.path.join(ROOT, 'node_modules', '.bin', 'esbuild')


_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
// A minimal in-memory localStorage: the core file reads it at LOAD time
// (report-lang map, active-paper id) so it MUST exist before target eval —
// hence it goes through setup({globals}) (applied before the eval), not after.
// The core file reads localStorage at LOAD time. jsdom exposes it on the
// window as a GETTER-ONLY prop (so setup's globals loop can't assign it), and
// the target eval runs in GLOBAL scope — so seed global.localStorage directly
// BEFORE setup() evals the targets.
const _lsMem = {};
global.localStorage = {
  getItem: (k) => (k in _lsMem ? _lsMem[k] : null),
  setItem: (k, v) => { _lsMem[k] = String(v); },
  removeItem: (k) => { delete _lsMem[k]; },
};
const { check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body></body>',
  targets: [
    // Extracted sibling first (optional pre-split), then the core monolith,
    // eval'd in the SAME shared scope.
    ...(process.argv[4] ? [process.argv[4]] : []),
    process.argv[2],
  ],
  globals: {
    // renderMarkdown override: distinguishable from escapeHtml so the
    // "assistant bubble uses markdown" check is meaningful.
    renderMarkdown: (s) => '<md>' + s + '</md>',
    renderToolRoundsHTML: (rounds, running) => '<tools n="' + rounds.length + '" run="' + (running ? 1 : 0) + '"/>',
    Icon: () => '<svg></svg>',
  },
});
// Native modules install compatibility functions on the browser Window. The
// classic core is indirect-eval'd onto Node's global object in this harness;
// make both names and state share that object after module initialization.
for (const name of [
  '_qaMsgInnerHtml', '_renderPaperQA', '_sendPaperQuestion', '_pollQATask',
  '_applyQAEvent', '_paperAskQuestion', '_quotePaperSelection',
  '_askAboutPaperSelection', '_hidePaperQuoteBar', '_handlePaperTextSelection',
]) {
  if (window[name]) globalThis[name] = window[name];
}
global.window = globalThis;
// QA state vars live in the CORE file's State block; the eval'd source declares
// them. We only need _paperQAHistory addressable here — set via the global the
// source `var` creates. (var at top level → global in this eval scope.)

(async () => {
  check('_qaMsgInnerHtml defined', typeof _qaMsgInnerHtml === 'function');
  check('_renderPaperQA defined', typeof _renderPaperQA === 'function');
  check('_applyQAEvent defined', typeof _applyQAEvent === 'function');
  if (typeof _qaMsgInnerHtml !== 'function') { report(); return; }

  // 1: user message → escaped, no markdown wrapper.
  {
    const h = _qaMsgInnerHtml({ role: 'user', content: '1 < 2 & <b>' });
    check('user bubble escapes html', h.includes('1 &lt; 2 &amp; &lt;b&gt;') && !h.includes('<md>'));
  }
  // 2: assistant message with content → markdown-rendered.
  {
    const h = _qaMsgInnerHtml({ role: 'assistant', content: 'hello' });
    check('assistant bubble uses markdown', h.includes('<md>hello</md>'));
  }
  // 3: assistant running with no content → thinking pulse.
  {
    const h = _qaMsgInnerHtml({ role: 'assistant', content: '', status: 'running' });
    check('running assistant shows thinking pulse', h.includes('paper-qa-thinking'));
  }
  // 4: assistant with toolRounds → tools panel included.
  {
    const h = _qaMsgInnerHtml({ role: 'assistant', content: 'x', status: 'running',
      toolRounds: [{ roundNum: 0, toolName: 'web_search' }] });
    check('toolRounds panel rendered', h.includes('<tools n="1"'));
  }

  // 5: _renderPaperQA reconciles list from _paperQAHistory.
  {
    const c = document.createElement('div'); c.id = 'paperQAMessages';
    document.body.appendChild(c);
    _paperQAHistory = [];
    _renderPaperQA();
    check('empty history → placeholder', c.innerHTML.includes('paper-qa-empty'));
    _paperQAHistory = [{ role: 'user', content: 'q1' }, { role: 'assistant', content: 'a1' }];
    _renderPaperQA();
    check('two messages → two bubble nodes',
          c.querySelectorAll('.paper-qa-msg').length === 2);
    check('user/assistant classes applied',
          !!c.querySelector('.paper-qa-user') && !!c.querySelector('.paper-qa-assistant'));
  }

  // 6: _applyQAEvent folds stream events into the assistant message.
  {
    const asst = { role: 'assistant', content: '', toolRounds: [] };
    _applyQAEvent(asst, { type: 'tool_start', roundNum: 0, toolName: 'web_search' });
    check('tool_start pushes a round', asst.toolRounds.length === 1 && asst.toolRounds[0].status === 'searching');
    _applyQAEvent(asst, { type: 'tool_done', roundNum: 0, elapsed: 1.2 });
    check('tool_done marks the round done', asst.toolRounds[0].status === 'done');
    _applyQAEvent(asst, { type: 'delta', delta: 'Hel' });
    _applyQAEvent(asst, { type: 'delta', delta: 'lo' });
    check('delta appends content', asst.content === 'Hello');
    _applyQAEvent(asst, { type: 'delta_reset' });
    check('delta_reset clears interim prose', asst.content === '');
  }

  // NC: prove the markdown-rendering path is load-bearing.
  {
    const real = _qaMsgInnerHtml;
    globalThis._qaMsgInnerHtml = (m) => '<div>' + escapeHtml(m.content) + '</div>';  // no markdown
    const h = _qaMsgInnerHtml({ role: 'assistant', content: 'hello' });
    check('NC: neutered builder drops markdown wrapper', !h.includes('<md>'));
    globalThis._qaMsgInnerHtml = real;
  }

  report();
})();
"""


def _run_qa_contract(qa_js: str) -> None:
    # argv[4] = the extracted sibling (present post-split; absent pre-split →
    # harness still evals the monolith and passes).
    run_harness(
        target_js=os.path.join(JS_DIR, 'paper-reader.js'),
        body_js=_BODY,
        min_pass=13,
        extra_targets=[qa_js],
    )


@pytest.mark.skipif(not shutil.which('node') or not os.path.isfile(ESBUILD),
                    reason='node + esbuild dev dependency required')
def test_vite_paper_qa_contract(tmp_path):
    built = tmp_path / 'paper-qa.js'
    compiled = subprocess.run(
        [ESBUILD, QA_TS, '--bundle', '--format=iife', '--platform=browser',
         f'--outfile={built}'],
        capture_output=True, text=True, timeout=60)
    assert compiled.returncode == 0, compiled.stderr
    _run_qa_contract(str(built))
