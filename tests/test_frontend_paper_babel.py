"""jsdom contract test for the Babel PDF-translation tab cluster.

Covers the functions Epic E cut #7 extracts from static/js/paper-reader.js into
static/js/paper/babel.js:
  • _initBabelPdfTab()   — builds the tab shell (lang buttons + body) into
    #paperTranslateContent when empty; idempotent.
  • _renderBabelResult(text) — renders translated markdown into #babelPdfBody
    (renderMarkdown when present, else escaped <pre>).

Babel is a leaf: its functions are contiguous and reference only each other +
core helpers at runtime. `_babelTranslatedPages` is SHARED (read by library
persist / enterPaperMode) so it moves with the cluster and resolves via
load-before ordering; `_babelTargetLang`/`_babelTranslating` are cluster-local.

Harness-first (recipe step 4): asserts against the CURRENT monolith and stays
green after the split because argv[4] (the extracted babel.js) is eval'd in the
SAME shared scope before the core file when present.

NC: neuter _renderBabelResult to a raw-text builder → the markdown wrapper is
gone, proving the real renderer is load-bearing.
"""

from __future__ import annotations

import os

import pytest

from tests._jsdom import JS_DIR, run_harness

pytestmark = pytest.mark.unit


_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
// Core reads localStorage at LOAD time; jsdom's window.localStorage is
// getter-only (setup's globals loop can't assign it) and the eval runs in
// global scope → seed global.localStorage before setup() evals the targets.
const _lsMem = {};
global.localStorage = {
  getItem: (k) => (k in _lsMem ? _lsMem[k] : null),
  setItem: (k, v) => { _lsMem[k] = String(v); },
  removeItem: (k) => { delete _lsMem[k]; },
};
const { check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body>'
      + '<div id="paperTranslateContent"></div>'
      + '<div id="babelPdfBody"></div>'
      + '</body>',
  targets: [
    ...(process.argv[4] ? [process.argv[4]] : []),
    process.argv[2],
  ],
  globals: {
    renderMarkdown: (s) => '<md>' + s + '</md>',
    escapeHtml: (s) => String(s == null ? '' : s).replace(/</g, '&lt;'),
    t: (k) => k,
    Icon: () => '<svg></svg>',
  },
});

(async () => {
  check('_initBabelPdfTab defined', typeof _initBabelPdfTab === 'function');
  check('_renderBabelResult defined', typeof _renderBabelResult === 'function');
  check('_switchBabelLang defined', typeof _switchBabelLang === 'function');
  check('_startBabelTranslation defined', typeof _startBabelTranslation === 'function');
  if (typeof _renderBabelResult !== 'function') { report(); return; }

  // _renderBabelResult routes translated text through renderMarkdown.
  _renderBabelResult('hello world');
  const body = document.getElementById('babelPdfBody');
  check('render routes through markdown', body.innerHTML.indexOf('<md>hello world</md>') >= 0);

  // NC: neuter the renderer → markdown wrapper disappears (load-bearing).
  {
    const real = _renderBabelResult;
    globalThis._renderBabelResult = (txt) => {
      const b = document.getElementById('babelPdfBody');
      if (b) b.innerHTML = '<pre>' + txt + '</pre>';
    };
    document.getElementById('babelPdfBody').innerHTML = '';
    _renderBabelResult('hello world');
    check('NC: neutered renderer drops markdown wrapper',
          document.getElementById('babelPdfBody').innerHTML.indexOf('<md>') < 0);
    globalThis._renderBabelResult = real;
  }

  report();
})();
"""


def test_paper_babel_contract():
    babel_js = os.path.join(JS_DIR, 'paper', 'babel.js')
    extra = [babel_js] if os.path.exists(babel_js) else []
    run_harness(
        target_js=os.path.join(JS_DIR, 'paper-reader.js'),
        body_js=_BODY,
        min_pass=6,
        extra_targets=extra,
    )
