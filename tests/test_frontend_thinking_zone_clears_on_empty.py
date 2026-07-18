"""Regression harness: the streaming top-level thinking zone must CLEAR the
instant the live buffer's ``thinking`` goes empty.

WHY
---
Symptom (owner report): during multi-round tool calling, if an early round
streams reasoning but a later round is tool-only (no reasoning), the OLD
thinking text stays pinned at the very bottom of the bubble forever.

Mechanism: ``updateStreamingUI`` (static/js/ui/streaming_ui.js) renders
``msg.thinking`` into ``[data-zone="thinking"]``. After a round issues tool
calls, the ``delta_reset`` handler moves that round's prose onto the tool
round's per-round ``.seg-thinking`` and ZEROES ``msg.thinking``. Every
subsequent tool-only round therefore has empty ``msg.thinking`` and skipped
the ``if (msg.thinking)`` write — but there was NO ``else`` branch to remove
the already-rendered block, so it lingered. This is asymmetric with the
content zone, which DOES clear on empty (``contentZone.innerHTML = ""``).

The fix adds the symmetric ``else if (thinkZone.firstChild)`` clear. This
harness loads the REAL shipped JS under jsdom and locks the contract:
render thinking → next frame with empty thinking → zone is empty.

Runs the REAL shipped JS under jsdom; skips cleanly when node + jsdom aren't
installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
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

win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
win.renderMarkdown = global.renderMarkdown = (s) => '<p>' + global.escapeHtml(s) + '</p>';
win._TOOL_DISPLAY = global._TOOL_DISPLAY = {};
win.t = global.t = (k, o) => k + (o && o.n != null ? (':' + o.n) : '');
win.isNearBottom = global.isNearBottom = () => false;
win.scrollToBottom = global.scrollToBottom = () => {};
win._stampFreshness = global._stampFreshness = () => {};
win._fcFingerprint = global._fcFingerprint = () => 0;
win._extractFileChangesFromRoundsAsync = global._extractFileChangesFromRoundsAsync = () => ({ then: () => {} });
win._renderFileChangesHtml = global._renderFileChangesHtml = () => '';
win.renderMcpLoginHintHtml = global.renderMcpLoginHintHtml = () => '';
win.renderPreferencesAppliedHtml = global.renderPreferencesAppliedHtml = () => '';
win.renderPreferenceLearnedHtml = global.renderPreferenceLearnedHtml = () => '';
win.renderMemoryPrefetchHtml = global.renderMemoryPrefetchHtml = () => '';
win.renderTurnProvenanceHtml = global.renderTurnProvenanceHtml = () => '';
win._isRoundSwarm = global._isRoundSwarm = (r) => !!(r && r._swarm);
win.convAutoTranslate = global.convAutoTranslate = () => false;
win.convAutoTranslateEffective = global.convAutoTranslateEffective = () => false;
win._startAutoTranslateForMsg = global._startAutoTranslateForMsg = () => {};
win._renderUnifiedToolLine = global._renderUnifiedToolLine = () => '<div class="ptool-line"></div>';
win._buildSwarmPanelHTML = global._buildSwarmPanelHTML = () => '<div class="sw-panel"></div>';
win._renderTurnHead = global._renderTurnHead = () => '';
win._renderSoloRoundTag = global._renderSoloRoundTag = () => '';
win._turnLabelText = global._turnLabelText = () => '';
win._buildSwarmInboxChipsHTML = global._buildSwarmInboxChipsHTML = () => '';
win.autoTranslate = global.autoTranslate = false;
win.CSS = global.CSS = undefined;

eval(fs.readFileSync(process.argv[2], 'utf8'));  // ui/streaming_ui.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof updateStreamingUI !== 'function') {
  console.log('FAIL functions_exposed updateStreamingUI missing'); process.exit(0);
}
check('functions_exposed', true);

const body = document.createElement('div');
body.id = 'streaming-body';
document.getElementById('chatInner').appendChild(body);
global.activeConvId = win.activeConvId = 'c1';
global.conversations = win.conversations = [{ id: 'c1', messages: [] }];

// ── Round 1: model streams reasoning BEFORE issuing tool calls ──
updateStreamingUI({
  content: '',
  thinking: 'thinking about round one plan',
  toolRounds: [{ roundNum: 1, status: 'searching', toolName: 'grep_search', llmRound: 0 }],
  phase: null,
});
const thinkZone = body.querySelector('[data-zone="thinking"]');
check('round1_thinking_rendered',
  !!thinkZone && thinkZone.textContent.includes('thinking about round one plan'));

// ── delta_reset moved the prose onto the round's per-round seg-thinking and
//    zeroed the live buffer's thinking. A later tool-only round now paints
//    with EMPTY msg.thinking. The top-level zone MUST clear (symmetric with
//    the content zone), not keep the round-1 block pinned. ──
updateStreamingUI({
  content: '',
  thinking: '',
  toolRounds: [
    { roundNum: 1, status: 'done', toolName: 'grep_search', llmRound: 0, thinking: 'thinking about round one plan' },
    { roundNum: 2, status: 'searching', toolName: 'read_files', llmRound: 1 },
  ],
  phase: null,
});
check('stale_thinking_cleared',
  !!thinkZone && thinkZone.querySelector('.thinking-block') === null
    && thinkZone.textContent.trim() === '');

// ── A fresh round that streams NEW reasoning must re-render the block ──
updateStreamingUI({
  content: '',
  thinking: 'brand new reasoning for round three',
  toolRounds: [
    { roundNum: 1, status: 'done', toolName: 'grep_search', llmRound: 0 },
    { roundNum: 2, status: 'done', toolName: 'read_files', llmRound: 1 },
    { roundNum: 3, status: 'searching', toolName: 'run_command', llmRound: 2 },
  ],
  phase: null,
});
check('new_thinking_reappears',
  !!thinkZone && thinkZone.textContent.includes('brand new reasoning for round three'));

console.log(out.join('\n'));
"""


def _run():
    harness = os.path.join(HERE, '_thinking_zone_clear_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'ui', 'streaming_ui.js'),   # argv[2]
             ROOT,                                            # argv[3]
             ],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'thinking-zone-clear failures:\n' + output
    assert output.count('PASS') >= 4, f'expected >=4 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_thinking_zone_clears_on_empty():
    _run()
