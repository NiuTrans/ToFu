"""tests/test_frontend_timer_id_chip.py — the timer-watcher header's
owner-reported defects, policed under jsdom against the REAL shipped JS.

WHY
---
The collapsed/expanded timer card (``_renderTimerWatcherBlock`` in
``static/js/ui/tool_rounds.js``) had three problems (owner, 2026-07-16):

  1. The long timer id was baked into the label TEXT (``定时器 tmr_xxx — …``)
     with no way to copy it. FIX: the id is extracted into a dedicated
     ``.timer-id-chip`` button whose click (delegated) copies the FULL id.

  2. Too many tiny glyphs cluttering the header. FIX: the id is one prominent
     token; the label no longer repeats it.

2026-07-28: the "模型原文" (model-view) button was removed from every tool
row per owner directive, so this suite now PINS ITS ABSENCE on the timer
header; the ``</> R{n}`` debug entry remains the stable request action.

This harness loads the real ``tool_rounds.js`` + ``upload_preview.js`` under
jsdom, renders an active timer round (debug_mode on, llmRound + _taskId set
so the debug entry renders), and asserts:
  • a ``.timer-id-chip`` carrying the FULL id in ``data-timer-id`` exists;
  • the header LABEL text does NOT contain the raw id (it moved to the chip);
  • NO model-view control exists anywhere in the row;
  • the debug entry renders with a stable task/round action;
  • clicking the id chip calls the clipboard writer with the full id.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest
from tests._runtime_sections import orchestration_legacy_test_root as _legacy_test_root

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = _legacy_test_root()
JS_DIR = os.path.join(ROOT, 'static', 'js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[5];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="previewModal"></div><div id="previewBody"></div></body>',
                      { url: 'http://localhost/', runScripts: 'dangerously' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.navigator = win.navigator;
global.console = console;

// ── clipboard spy ──
let copiedValue = null;
global._safeClipboardWrite = win._safeClipboardWrite = (txt) => {
  copiedValue = txt;
  return Promise.resolve();
};
// ── globals the renderers + preview delegation touch ──
win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
win.t = global.t = (k, o) => k + (o && o.n != null ? (':' + o.n) : '');
win.Icon = global.Icon = (name, size) => '<svg data-icon="' + name + '"></svg>';
win.renderMarkdown = global.renderMarkdown = (s) => '<p>' + global.escapeHtml(s) + '</p>';
win._isRoundSwarm = global._isRoundSwarm = () => false;
win.getActiveConv = global.getActiveConv = () => _conv;
// debug_mode on so _renderDebugEntry emits the entry.
win._featureFlags = global._featureFlags = { debug_mode: true };

let trSrc = fs.readFileSync(process.argv[2], 'utf8');   // ui/tool_rounds.js (dispatcher)
const richSrc = fs.readFileSync(process.argv[3], 'utf8'); // ui/tool_rounds_rich.js (timer watcher block)
// ONE eval so the rich block's top-level consts stay in scope for it.
eval(trSrc + '\n' + richSrc);
eval(fs.readFileSync(process.argv[4], 'utf8')); // upload_preview.js (installs click delegation)

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const FULL_ID = 'tmr_b3b3a438deadbeef';
const round = {
  roundNum: 3,
  llmRound: 2,               // → debug entry labels R3
  _taskId: 'task-timer-1',
  status: 'searching',
  toolName: 'timer_create',
  query: 'watch build',
  results: null,
  toolContent: 'Timer created — polling every 120s (max 60)',
  _timerPolls: [{ pollNum: 1, decision: 'started', reason: 'created', ts: 1 }],
  _timerTimerId: FULL_ID,
  _timerNextPollTs: Date.now() + 52 * 1000,
  _timerPollInterval: 120,
  _timerMaxPolls: 60,
  _timerCheckInstruction: 'check the GitHub Actions run status',
};
const _conv = { id: 'conv-timer' };

const container = document.createElement('div');
document.body.appendChild(container);
container.innerHTML = _renderUnifiedToolLine(round, true);

// 1. id chip present, carries the FULL id.
const chip = container.querySelector('.timer-id-chip[data-timer-id]');
check('id_chip_present', !!chip);
check('id_chip_has_full_id', !!chip && chip.getAttribute('data-timer-id') === FULL_ID);

// 2. header LABEL text does NOT contain the raw id (moved out to the chip).
const label = container.querySelector('.timer-watcher-label');
check('label_present', !!label);
check('label_has_no_raw_id', !!label && label.textContent.indexOf('tmr_') < 0);

// 3. The model-view button is GONE (removed 2026-07-28 per owner) — neither
//    the toolContent-backed nor the registry-backed variant may reappear.
check('model_view_absent',
  !container.querySelector('[data-tc-preview],[data-tc-preview-text],.tc-preview-btn'));

// 4. The debug entry carries stable task/round identity for ActionRegistry.
const anchor = container.querySelector('.ri-tool-anchor');
check('debug_entry_present', !!anchor);
check('debug_entry_has_stable_action', !!anchor &&
  anchor.getAttribute('data-tofu-action') ===
    "openToolDebugPanel('task-timer-1',3,this)");

// 5. clicking the id chip copies the FULL id.
if (chip) {
  copiedValue = null;
  chip.dispatchEvent(new win.MouseEvent('click', { bubbles: true, cancelable: true }));
  check('id_chip_copies_full_id', copiedValue === FULL_ID);
}

console.log(out.join('\n'));
process.exit(0);
"""


def _run():
    harness = os.path.join(HERE, '_timer_id_chip_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'ui', 'tool_rounds.js'),      # argv[2]
             os.path.join(JS_DIR, 'ui', 'tool_rounds_rich.js'), # argv[3]
             os.path.join(JS_DIR, 'upload_preview.js'),         # argv[4]
             ROOT,                                              # argv[5]
             ],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    return proc


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_timer_id_chip_and_debug_entry():
    proc = _run()
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'timer id-chip / debug-entry failures:\n' + output
    assert output.count('PASS') >= 8, f'expected >=8 PASS lines, got:\n{output}'
