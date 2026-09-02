"""jsdom regression: Project Brain hover-preview for opaque conversation IDs.

WHY
Every conversation reference in the Project Brain panel (activity chips, board
owner chips, influence rows, peer roster, the peer-message from→to thread) is a
short opaque conv id — a reader could not tell what a conversation was about
without opening it. The panel now resolves any ``[data-conv-id]`` element on
hover to a preview card built from a tiny backend endpoint
(``Api.conversations.preview`` → ``{title, firstUserMessage, msgCount}``), so
the id reads as "the first thing that conversation asked".

This harness loads the REAL shipped ``project-brain.js`` under jsdom, stubs
``Api.conversations.preview``, mounts the real overlay + a ``[data-conv-id]``
chip, and drives the delegated hover path (``_initConvPreview`` +
``_showConvPreview``) plus the pure ``buildConvPreviewCard`` renderer. Asserts:
  1. ``buildConvPreviewCard`` renders the title + the first-question body;
  2. a conv with no user turn shows the "no messages yet" empty line;
  3. hovering a chip fetches (once, cached) and floats the populated card;
  4. the fetch result is cached — a second show does not refetch.

DOUBLE-NEUTER (in a COPY; shipped file byte-identical after):
  • NC: make ``buildConvPreviewCard`` ignore ``firstUserMessage`` (render only
    the title) → the first-question body is gone → assertion (1) fails.

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest
from tests._runtime_sections import orchestration_legacy_test_root as _legacy_test_root

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = _legacy_test_root()
JS_DIR = os.path.join(ROOT, 'static', 'js')
_BRAIN_SRC = os.path.join(JS_DIR, 'project-brain.js')

_FIRST_Q = 'How do I fix a CORS error in the Flask login route?'


def _node_deps_available():
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_DOM = r'''<!DOCTYPE html><body>
<div class="project-brain-overlay" id="projectBrainOverlay">
  <div class="project-brain-panel">
    <div class="project-brain-columns">
      <div class="project-brain-col pb-tab-panel pb-tab-panel-active" data-pb-panel="activity">
        <div class="project-brain-col-body">
          <button class="pb-conv-chip" id="chipA" data-conv-id="convAAAA1111">convAAAA1111</button>
          <button class="pb-conv-chip" id="chipB" data-conv-id="convBBBB2222">convBBBB2222</button>
        </div>
      </div>
    </div>
  </div>
</div>
</body>'''


def _harness():
    return r'''
const fs = require('fs');
const path = require('path');
const SRC = process.argv[1];
const ROOT = process.argv[2];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(DOM_PLACEHOLDER, { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;
win.t = global.t = (k, f) => (f || k);
win.Icon = global.Icon = () => '<svg></svg>';
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);
win.activeConvId = global.activeConvId = '';

// Stub the preview endpoint. Count calls per conv id to prove caching.
let fetchCounts = {};
win.Api = global.Api = { conversations: {
  preview: function (convId) {
    fetchCounts[convId] = (fetchCounts[convId] || 0) + 1;
    if (convId === 'convAAAA1111') {
      return Promise.resolve({ id: convId, title: 'Fix Flask CORS',
        firstUserMessage: FIRST_Q_PH, msgCount: 6 });
    }
    // convBBBB2222 → an empty conversation (no user turn).
    return Promise.resolve({ id: convId, title: '', firstUserMessage: '', msgCount: 0 });
  },
} };

eval(fs.readFileSync(SRC, 'utf8'));
const PB = win.ProjectBrain;
const out = {};

// (1)+(2) Pure renderer.
const cardA = PB.buildConvPreviewCard(
  { id: 'convAAAA1111', title: 'Fix Flask CORS', firstUserMessage: FIRST_Q_PH, msgCount: 6 },
  'convAAAA1111');
out.cardHasTitle = cardA.indexOf('Fix Flask CORS') !== -1;
out.cardHasFirstQ = cardA.indexOf(FIRST_Q_PH) !== -1;
out.cardHasLabelClass = cardA.indexOf('pb-preview-label') !== -1;
const cardB = PB.buildConvPreviewCard(
  { id: 'convBBBB2222', title: '', firstUserMessage: '', msgCount: 0 }, 'convBBBB2222');
out.emptyHasEmptyLine = cardB.indexOf('pb-preview-empty') !== -1;
out.emptyHasShortId = cardB.indexOf('convBBBB') !== -1;

// (3) Hover flow: init the delegated listener, then directly show a chip.
PB._initConvPreview();
const chipA = win.document.getElementById('chipA');
PB._showConvPreview(chipA);

// _showConvPreview fetches on a microtask; drain then inspect the floating card.
setTimeout(function () {
  const pv = win.document.querySelector('.pb-conv-preview');
  out.previewNodeExists = !!pv;
  out.previewVisible = pv ? !pv.hidden : false;
  out.previewShowsFirstQ = pv ? (pv.textContent.indexOf(FIRST_Q_PH) !== -1) : false;

  // (4) Caching: a second show must NOT refetch convAAAA1111.
  PB._hideConvPreview();
  PB._showConvPreview(chipA);
  setTimeout(function () {
    out.fetchCountA = fetchCounts['convAAAA1111'] || 0;
    console.log('__RESULT__' + JSON.stringify(out));
  }, 30);
}, 30);
'''.replace('DOM_PLACEHOLDER', json.dumps(_DOM)) \
   .replace('FIRST_Q_PH', json.dumps(_FIRST_Q))


def _run(src):
    proc = subprocess.run(
        ['node', '-e', _harness(), src, ROOT],
        capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(f'harness failed: {proc.stderr or proc.stdout}')
    for line in proc.stdout.splitlines():
        if line.startswith('__RESULT__'):
            return json.loads(line[len('__RESULT__'):])
    raise AssertionError(f'no result line: {proc.stdout}\n{proc.stderr}')


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_conv_preview_renders_and_caches():
    out = _run(_BRAIN_SRC)
    # (1) Pure renderer shows the title + the opening question.
    assert out['cardHasTitle'] is True, out
    assert out['cardHasFirstQ'] is True, \
        f'preview card must render the first user question: {out}'
    assert out['cardHasLabelClass'] is True, out
    # (2) Empty conversation → an explicit empty line + the short id.
    assert out['emptyHasEmptyLine'] is True, out
    assert out['emptyHasShortId'] is True, out
    # (3) Hover flow floats the populated card.
    assert out['previewNodeExists'] is True, out
    assert out['previewVisible'] is True, out
    assert out['previewShowsFirstQ'] is True, \
        f'hovering a chip must show its first question: {out}'
    # (4) Second show is served from cache — no refetch.
    assert out['fetchCountA'] == 1, \
        f'preview must be cached (fetched once), got {out.get("fetchCountA")}: {out}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_preview_body_is_load_bearing(tmp_path):
    """NC: make buildConvPreviewCard ignore firstUserMessage (title-only) → the
    first-question body vanishes. Shipped file byte-identical after."""
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = '    var first = preview && preview.firstUserMessage;'
    assert anchor in original, 'preview first-question anchor not found'
    patched = original.replace(
        anchor, '    var first = null;  // NC: ignore firstUserMessage', 1)
    assert patched != original, 'NC patch did not apply'
    src = os.path.join(tmp_path, 'brain-nc.js')
    with open(src, 'w', encoding='utf-8') as f:
        f.write(patched)
    out = _run(src)
    assert out['cardHasFirstQ'] is False, \
        f'NC: ignoring firstUserMessage must drop the question body: {out}'
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain.js must be byte-identical'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
