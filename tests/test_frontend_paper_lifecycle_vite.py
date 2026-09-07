"""The native Paper lifecycle owns and releases every page-level listener."""

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
    ROOT, 'frontend', 'src', 'features', 'paper', 'lifecycle.ts')
ESBUILD = os.path.join(ROOT, 'scripts', 'vite_test_bundle.mjs')


_HARNESS = r"""
const fs = require('fs'), path = require('path');
const ROOT = process.argv[1], BUILT = process.argv[2];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body>' +
  '<div id="paperPdfViewer"></div><div id="paperModeContainer"></div>' +
  '<div id="paperSidebarOverlay"></div></body>', { url: 'http://localhost/' });
global.window = global;
global.document = dom.window.document;
global.Node = dom.window.Node;
global.addEventListener = dom.window.addEventListener.bind(dom.window);
global.removeEventListener = dom.window.removeEventListener.bind(dom.window);
global.dispatchEvent = dom.window.dispatchEvent.bind(dom.window);
global.paperMode = true;
global._paperScale = 1;
const calls = { key: 0, selection: 0, unload: 0, library: 0,
  drop: 0, zoom: 0, render: 0, qa: 0, recPaint: 0, searchPaint: 0,
  qaDestroy: 0,
  researchDestroy: 0, podcastStop: 0, videoStop: 0 };
global._handlePaperKeyDown = () => { calls.key++; };
global._handlePaperTextSelection = () => { calls.selection++; };
global._teardownReadingTracker = () => { calls.unload++; };
global._loadPaperLibrary = () => { calls.library++; };
global._handlePaperFileDrop = async () => { calls.drop++; };
global._syncZoomUI = () => { calls.zoom++; };
global._renderAllPages = () => { calls.render++; };
global._reportView = () => null;
global._renderPaperQA = () => { calls.qa++; };
global._paperSearchResults = [{ title:'A $x$ paper' }];
global._lastArxivSearchQuery = 'paper';
global._recStream = { status:'done' };
global._paintRecommendFromState = () => { calls.recPaint++; };
global._renderArxivSearchResults = () => { calls.searchPaint++; };
global._destroyPaperQA = () => { calls.qaDestroy++; };
global._destroyResearchRuntime = () => { calls.researchDestroy++; };
global._pcStopPolling = () => { calls.podcastStop++; };
global._pvStopPolling = () => { calls.videoStop++; };

(0, eval)(fs.readFileSync(BUILT, 'utf8'));
_installPaperLifecycle();
document.dispatchEvent(new dom.window.Event('DOMContentLoaded'));

function keydown() {
  document.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: '+' }));
}
function dropPdf() {
  const event = new dom.window.Event('drop', { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'dataTransfer', { value: {
    types: ['Files'], files: [new dom.window.File(['pdf'], 'paper.pdf',
      { type: 'application/pdf' })],
  }});
  document.getElementById('paperPdfViewer').dispatchEvent(event);
}

(async () => {
  keydown();
  const afterFirstKey = calls.key;
  document.dispatchEvent(new dom.window.Event('DOMContentLoaded'));
  keydown();
  const secondReadyDidNotDuplicate = calls.key === afterFirstKey + 1;

  document.dispatchEvent(new dom.window.MouseEvent('mouseup', { bubbles: true }));
  window.dispatchEvent(new dom.window.Event('beforeunload'));
  document.getElementById('paperPdfViewer').innerHTML =
    '<div class="paper-search"><div class="paper-result-list"></div></div>';
  window.dispatchEvent(new dom.window.Event('katex:loaded'));
  const staleRecommendDidNotClobber = calls.searchPaint === 1
    && calls.recPaint === 0
    && !!document.querySelector('.paper-search .paper-result-list');
  dropPdf();
  document.getElementById('paperPdfViewer').dispatchEvent(
    new dom.window.WheelEvent('wheel', { ctrlKey: true, deltaY: -1,
      bubbles: true, cancelable: true }));
  await new Promise((resolve) => setTimeout(resolve, 180));

  const beforeDestroy = { ...calls };
  _destroyPaperSession();
  const afterSessionDestroy = { ...calls };
  _destroyPaperLifecycle();
  keydown();
  document.dispatchEvent(new dom.window.MouseEvent('mouseup', { bubbles: true }));
  window.dispatchEvent(new dom.window.Event('beforeunload'));
  dropPdf();
  await new Promise((resolve) => setTimeout(resolve, 30));

  console.log(JSON.stringify({ calls, beforeDestroy, afterSessionDestroy,
    staleRecommendDidNotClobber,
    secondReadyDidNotDuplicate,
    scale: _paperScale, noListenersAfterDestroy:
      calls.key === beforeDestroy.key &&
      calls.selection === beforeDestroy.selection &&
      calls.unload === beforeDestroy.unload && calls.drop === beforeDestroy.drop }));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""


@pytest.mark.skipif(
    not shutil.which('node') or not os.path.isfile(ESBUILD)
    or not os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom')),
    reason='node/jsdom/esbuild dev dependencies required',
)
def test_native_lifecycle_is_idempotent_and_releases_every_listener(tmp_path):
    built = tmp_path / 'paper-lifecycle.js'
    compiled = compile_feature_owner(ESBUILD, SOURCE, built, tmp_path)
    assert compiled.returncode == 0, compiled.stderr
    process = subprocess.run(
        ['node', '-e', _HARNESS, ROOT, str(built)],
        capture_output=True, text=True, timeout=60)
    assert process.returncode == 0, process.stderr + process.stdout
    result = json.loads(process.stdout.strip().splitlines()[-1])
    before = result['beforeDestroy']
    assert result['secondReadyDidNotDuplicate'] is True
    assert before['key'] == 2
    assert before['selection'] == 1
    assert before['unload'] == 1
    assert before['library'] == 1
    assert before['drop'] == 1
    assert before['zoom'] == 1 and before['render'] == 1
    assert before['qa'] == 1
    assert result['staleRecommendDidNotClobber'] is True
    assert before['searchPaint'] == 1 and before['recPaint'] == 0
    after_session = result['afterSessionDestroy']
    assert after_session['qaDestroy'] == 1
    assert after_session['researchDestroy'] == 1
    assert after_session['podcastStop'] == 1
    assert after_session['videoStop'] == 1
    assert result['scale'] > 1
    assert result['noListenersAfterDestroy'] is True
