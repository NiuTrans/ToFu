"""jsdom regression for the streaming-output FLICKER root cause: the periodic
"refreeze" must NOT tear down and rebuild the already-committed frozen DOM.

WHY
---
``updateStreamingUI`` (streaming_ui.js) renders the live answer as a stable
"frozen" prefix + a small re-rendered "tail". Every time the tail grew past
``REFREEZE_THRESHOLD`` (600 chars) the OLD code advanced the freeze point by
rewriting the ENTIRE ``.md-content`` via ``contentZone.innerHTML = …`` — which
DESTROYS and RECREATES every already-painted frozen node and re-parses the whole
answer so far. On a long reply this fires every ~600 chars, and each rebuild is a
visible full-content flash (the reported streaming flicker).

THE FIX (root cause, not a patch)
---------------------------------
Keep the committed frozen DOM intact. On a refreeze, render ONLY the newly-frozen
segment (the content between the old and new freeze points — both sit on ``\n\n``
block boundaries, so rendering the segment independently is faithful to rendering
the whole prefix) and ``insertAdjacentHTML('beforebegin', …)`` it as new frozen
siblings right before the tail, then reset ONLY the small tail. Nothing already
painted is torn down → no flash.

This harness drives the REAL shipped ``updateStreamingUI`` under jsdom. It STAMPS
an identity marker on the ``.md-content`` element after the first substantial
render, then feeds enough content to cross the refreeze threshold, and asserts:
  • the stamped ``.md-content`` element SURVIVES the refreeze (identity kept);
  • a new frozen sibling was appended before the tail (the newly-frozen segment);
  • the tail still shows the latest content (render stays faithful).

NEUTER: force the incremental branch OFF (``if (false)``) so refreeze falls back
to the whole-``innerHTML`` rebuild → the stamped ``.md-content`` is recreated and
the marker is LOST — proving the incremental preservation is load-bearing.

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')
STREAMING_UI = os.path.join(JS_DIR, 'ui', 'streaming_ui.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const NEUTER = process.argv[4] || 'none';
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatInner"></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
global.setInterval = win.setInterval = () => 0;
global.setTimeout = win.setTimeout = (fn) => 0;
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => 0;
global.getSelection = win.getSelection = () => ({ isCollapsed: true, rangeCount: 0 });

// Globals updateStreamingUI touches at call time. renderMarkdown wraps text in
// a single <p> so a frozen segment is one node — identity is easy to track.
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
win.renderMarkdown = global.renderMarkdown = (s) => '<p>' + global.escapeHtml(s) + '</p>';
win.t = global.t = (k) => k;
win.isNearBottom = global.isNearBottom = () => false;
win.scrollToBottom = global.scrollToBottom = () => {};
win._stampFreshness = global._stampFreshness = () => {};
win._fcFingerprint = global._fcFingerprint = () => 0;
win._extractFileChangesFromRoundsAsync = global._extractFileChangesFromRoundsAsync = () => ({ then: () => {} });
win._renderFileChangesHtml = global._renderFileChangesHtml = () => '';
win.renderMcpLoginHintHtml = global.renderMcpLoginHintHtml = () => '';
win.renderPreferenceLearnedHtml = global.renderPreferenceLearnedHtml = () => '';
win.renderTurnProvenanceHtml = global.renderTurnProvenanceHtml = () => '';
win._isRoundSwarm = global._isRoundSwarm = (r) => !!(r && r._swarm);
win._renderUnifiedToolLine = global._renderUnifiedToolLine = () => '<div class="ptool-line"></div>';
win._buildSwarmPanelHTML = global._buildSwarmPanelHTML = () => '<div class="sw-panel"></div>';
win._buildSwarmInboxChipsHTML = global._buildSwarmInboxChipsHTML = () => '';
win._renderTurnHead = global._renderTurnHead = () => '';
win._renderSoloRoundTag = global._renderSoloRoundTag = () => '';
win._turnLabelText = global._turnLabelText = () => '';
win.CSS = global.CSS = undefined;
win.activeStreams = global.activeStreams = new Map();
win.streamBufs = global.streamBufs = new Map();
win.conversations = global.conversations = [];
global.activeConvId = win.activeConvId = 'c1';

let src = fs.readFileSync(process.argv[3], 'utf8');  // ui/streaming_ui.js
if (NEUTER === 'rebuild') {
  // Force the incremental-preserve branch OFF → refreeze falls back to the
  // whole-innerHTML rebuild (the old behaviour that recreated frozen nodes).
  src = src.replace('if (frozenLen > 0 && mdContentEl && tailEl) {',
                    'if (false) { /* NEUTERED-rebuild */');
  if (src.indexOf('NEUTERED-rebuild') < 0) { console.log('FAIL neuter_rebuild_applied'); process.exit(0); }
}
(0, eval)(src);

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof updateStreamingUI !== 'function') { console.log('FAIL updateStreamingUI_exposed'); process.exit(0); }
check('updateStreamingUI_exposed', true);

function freshBody() {
  document.getElementById('chatInner').innerHTML =
    '<div class="message" id="streaming-msg" data-msg-id="m1">' +
    '<div class="message-body" id="streaming-body"></div></div>';
}
function contentZone() {
  return document.getElementById('streaming-body').querySelector('[data-zone="content"]');
}

// ── Content sequence ──
// P: a 200-char paragraph. content1 establishes the split (frozenLen=200).
// content2 grows the tail past 600 → refreeze, with a NEW \n\n boundary at 602
// so a newly-frozen segment (200..602) is appended before the tail.
const P = 'A'.repeat(200);
const content1 = P + '\n\n' + 'B'.repeat(120);                         // len 322
const content2 = P + '\n\n' + 'B'.repeat(400) + '\n\n' + 'C'.repeat(300);  // len 904

freshBody();

// Frame 1: first substantial render → builds the .md-content split structure.
updateStreamingUI({ content: content1, thinking: '', toolRounds: [], phase: null });
const cz = contentZone();
const md1 = cz && cz.querySelector('.md-content');
check('frame1_split_built', !!md1 && !!cz.querySelector('.md-stream-tail'));

// STAMP the frozen structure with an identity marker + count frozen <p> nodes.
if (!md1) { console.log(out.join('\n')); process.exit(0); }
md1.__identity = 'FROZEN-KEEP';
md1.setAttribute('data-identity', 'FROZEN-KEEP');
const frozenPsBefore = md1.querySelectorAll(':scope > p').length;

// Frame 2: cross the refreeze threshold (tail 704 >= 600).
updateStreamingUI({ content: content2, thinking: '', toolRounds: [], phase: null });
const cz2 = contentZone();
const md2 = cz2 && cz2.querySelector('.md-content');

// (a) IDENTITY: the SAME .md-content element must survive the refreeze. The JS
//     property __identity only survives if the node was never recreated.
check('refreeze_preserves_mdcontent_node',
  !!md2 && md2.__identity === 'FROZEN-KEEP' && md2.getAttribute('data-identity') === 'FROZEN-KEEP');

// (b) A new frozen sibling was appended before the tail (the newly-frozen
//     segment 200..602) → more frozen <p> nodes than before.
const frozenPsAfter = md2 ? md2.querySelectorAll(':scope > p').length : 0;
check('refreeze_appended_new_frozen', frozenPsAfter > frozenPsBefore);

// (c) Render stays faithful: the tail shows the latest 'C' content.
const tail2 = md2 && md2.querySelector('.md-stream-tail');
check('refreeze_tail_shows_latest', !!tail2 && /CCC/.test(tail2.innerHTML));

console.log(out.join('\n'));
"""


def _run_node(neuter: str = 'none') -> str:
    with tempfile.NamedTemporaryFile('w', suffix='.js', dir=HERE,
                                     delete=False, encoding='utf-8') as fh:
        hp = fh.name
        fh.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', hp, ROOT, STREAMING_UI, neuter],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(hp)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return (proc.stdout or '').strip()


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_refreeze_preserves_frozen_dom():
    output = _run_node('none')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'refreeze-no-flash failures:\n' + output
    for name in ('frame1_split_built', 'refreeze_preserves_mdcontent_node',
                 'refreeze_appended_new_frozen', 'refreeze_tail_shows_latest'):
        assert f'PASS {name}' in output, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_incremental_preserve_is_load_bearing():
    """Neuter the incremental branch → refreeze rebuilds the whole `.md-content`
    innerHTML, recreating the frozen nodes → the identity marker is LOST. This is
    the exact full-teardown flash the fix removes."""
    output = _run_node('rebuild')
    assert 'FAIL refreeze_preserves_mdcontent_node' in output, (
        'expected the .md-content node to be recreated (identity lost) under the '
        'whole-innerHTML rebuild:\n' + output)


def test_source_carries_incremental_refreeze():
    """The shipped source must contain the incremental-preserve seam so this
    regression rots with the code, not just the harness copy."""
    with open(STREAMING_UI, encoding='utf-8') as f:
        src = f.read()
    assert "tailEl.insertAdjacentHTML('beforebegin'" in src, (
        'streaming_ui.js no longer appends the newly-frozen segment before the '
        'tail — the refreeze is back to a full innerHTML teardown (flicker).')
    assert 'FLICKER FIX' in src, 'the flicker-fix rationale block is missing from streaming_ui.js'
