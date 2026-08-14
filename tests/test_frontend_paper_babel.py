"""jsdom contract test for the native Babel PDF-translation owner.

Covers the browser globals exported by ``features/paper/babel.ts``:
  • _initBabelPdfTab()   — builds the tab shell (lang buttons + body) into
    #paperTranslateContent when empty; idempotent.
  • _renderBabelResult(text) — renders translated markdown into #babelPdfBody
    (renderMarkdown when present, else escaped <pre>).

The test compiles the TypeScript owner with esbuild before exercising the real
push/poll, cancellation, persistence and rendering contract.

NC: neuter _renderBabelResult to a raw-text builder → the markdown wrapper is
gone, proving the real renderer is load-bearing.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._jsdom import run_harness

pytestmark = pytest.mark.unit

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
BABEL_TS = os.path.join(
    ROOT, 'frontend', 'src', 'features', 'paper', 'babel.ts')
ESBUILD = os.path.join(ROOT, 'node_modules', '.bin', 'esbuild')


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


_VITE_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
let releaseZhCache;
let releaseEnPoll;
const calls = { starts: [], aborts: [], saves: 0 };
const subscriptions = [];
const paperApi = {
  translateCache: (_hash, lang) => {
    if (lang === 'zh') return new Promise((resolve) => { releaseZhCache = resolve; });
    return Promise.resolve({ ok: false });
  },
  translateStart: async (body) => {
    calls.starts.push(body.lang);
    return { ok: true, task_id: 'task-' + body.lang };
  },
  translateAbort: async (taskId) => { calls.aborts.push(taskId); },
  translatePoll: async (taskId) => taskId === 'task-en'
    ? new Promise((resolve) => { releaseEnPoll = () => resolve({
      ok: true,
      status: 200,
      json: async () => ({
        ok: true,
        next_cursor: 2,
        status: 'done',
        events: [
          { seq: 0, type: 'chunk', index: 0, total: 1, text: 'translated' },
          { seq: 1, type: 'done', text: 'EN DONE' },
        ],
      }),
    }); })
    : ({
    ok: true,
    status: 200,
    json: async () => ({
      ok: true,
      next_cursor: 2,
      status: 'done',
      events: [
        { type: 'chunk', index: 0, total: 1, text: 'translated' },
        { type: 'done', text: taskId === 'task-en' ? 'EN DONE' : 'ZH DONE' },
      ],
    }),
  }),
};
const { window: win, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="paperTranslateContent"></div></body>',
  targets: [process.argv[2]],
  globals: {
    Api: { paper: paperApi },
    renderMarkdown: (text) => '<md>' + text + '</md>',
    escapeHtml: (text) => String(text == null ? '' : text).replace(/</g, '&lt;'),
    t: (key, vars) => vars && vars.lang ? key + ':' + vars.lang : key,
    pushSubscribe: (channel, taskId, handler) => {
      subscriptions.push({ channel, taskId, handler });
    },
    pushUnsubscribe: (channel, taskId, handler) => {
      const index = subscriptions.findIndex((row) =>
        row.channel === channel && row.taskId === taskId && row.handler === handler);
      if (index >= 0) subscriptions.splice(index, 1);
    },
  },
});
win._saveActivePaperState = () => { calls.saves++; };

(async () => {
  check('globals installed',
    typeof win._initBabelPdfTab === 'function' &&
    typeof win._switchBabelLang === 'function' &&
    typeof win._renderBabelResult === 'function');
  win._initBabelPdfTab();
  check('tab shell rendered', !!document.querySelector('.babel-pdf-module'));
  win._renderBabelResult('hello');
  check('markdown renderer used',
    document.getElementById('babelPdfBody').innerHTML.includes('<md>hello</md>'));

  // Hold zh in its cache probe, switch to en while it owns the single-flight
  // latch, then release zh. The native owner must abort zh and continue en.
  win._paperParsedText = 'paper text';
  win._paperHash = 'paper-hash';
  win._switchBabelLang('zh', document.querySelector('[data-lang="zh"]'));
  await new Promise(setImmediate);
  check('zh worker entered cache probe', typeof releaseZhCache === 'function');
  win._switchBabelLang('en', document.querySelector('[data-lang="en"]'));
  releaseZhCache({ ok: false });
  for (let i = 0; i < 8; i++) await new Promise(setImmediate);

  check('stale language aborted', calls.aborts.includes('task-zh'));
  check('selected language continued', calls.starts.includes('en'));
  const enSubscription = subscriptions.find((row) => row.taskId === 'task-en');
  check('translation uses compatibility channel',
    enSubscription && enSubscription.channel === 'paper-translate');
  check('poll remains in flight', typeof releaseEnPoll === 'function');
  enSubscription.handler({ seq: 0, type: 'chunk', index: 0, total: 1, text: 'translated' });
  enSubscription.handler({ seq: 1, type: 'done', text: 'EN DONE' });
  check('push renders before poll returns',
    document.getElementById('babelPdfBody').innerHTML.includes('<md>EN DONE</md>'));
  check('push terminal detaches', subscriptions.length === 0);
  releaseEnPoll();
  for (let i = 0; i < 4; i++) await new Promise(setImmediate);
  check('selected result persisted', win._babelTranslatedPages.en === 'EN DONE');
  check('selected result rendered',
    document.getElementById('babelPdfBody').innerHTML.includes('<md>EN DONE</md>'));
  check('single flight released', win._babelTranslating === false);
  check('push and poll are exactly once', calls.saves === 1);
  report();
})();
"""


@pytest.mark.skipif(not shutil.which('node') or not os.path.isfile(ESBUILD),
                    reason='node + esbuild dev-deps not installed')
def test_vite_paper_babel_contract(tmp_path):
    built = tmp_path / 'paper-babel.js'
    compiled = subprocess.run(
        [ESBUILD, BABEL_TS, '--bundle', '--format=iife',
         '--platform=browser', f'--outfile={built}'],
        capture_output=True, text=True, timeout=60)
    assert compiled.returncode == 0, compiled.stderr
    run_harness(
        target_js=str(built),
        body_js=_VITE_BODY,
        expect_pass=14,
    )
