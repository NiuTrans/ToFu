"""tests/test_frontend_timer_id_chip.py — the timer-watcher header's three
owner-reported defects, policed under jsdom against the REAL shipped JS.

WHY
---
The collapsed/expanded timer card (``_renderTimerWatcherBlock`` in
``static/js/ui/tool_rounds.js``) had three problems (owner, 2026-07-16):

  1. "模型原文" (model-view) button DID NOTHING. The header's toggle
     ``onclick`` called ``event.stopPropagation()`` UNCONDITIONALLY, so the
     click never bubbled to the document-level delegation in
     ``upload_preview.js`` that opens the verbatim-text modal. FIX: the header
     onclick now bails when the click lands on
     ``.timer-id-chip,[data-tc-preview],[data-tc-preview-text]`` — the exact
     bubble-through guard the search results-header already uses.

  2. The long timer id was baked into the label TEXT (``定时器 tmr_xxx — …``)
     with no way to copy it. FIX: the id is extracted into a dedicated
     ``.timer-id-chip`` button whose click (delegated) copies the FULL id.

  3. Too many tiny glyphs cluttering the header. FIX: the id is one prominent
     token; the label no longer repeats it.

This harness loads the real ``tool_rounds.js`` + ``upload_preview.js`` under
jsdom, renders an active timer round, and asserts:
  • a ``.timer-id-chip`` carrying the FULL id in ``data-timer-id`` exists;
  • the header LABEL text does NOT contain the raw id (it moved to the chip);
  • clicking the model-view button reaches the document delegation and opens
    the preview modal (proving the stopPropagation guard works);
  • clicking the id chip calls the clipboard writer with the full id.

A NEUTER restores the unconditional ``stopPropagation()`` and proves the
model-view click is then swallowed (modal never opens) — the guard is
load-bearing.
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
const ROOT = process.argv[4];
const NEUTER = process.argv[5] === 'neuter';
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
// runScripts:'dangerously' so the header's INLINE onclick attribute (set via
// innerHTML) actually executes — that inline handler is exactly what calls
// stopPropagation, so without this the guard can't be exercised.
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

let trSrc = fs.readFileSync(process.argv[2], 'utf8');
if (NEUTER) {
  // Restore the OLD unconditional stopPropagation — drop the bubble-through
  // guard from the timer header onclick, proving the guard is load-bearing.
  const before = trSrc;
  trSrc = trSrc.replace(
    "onclick=\"if(event.target.closest('.timer-id-chip,[data-tc-preview],[data-tc-preview-text]'))return;event.stopPropagation();var w=document.getElementById('${uid}-wrap');",
    "onclick=\"event.stopPropagation();var w=document.getElementById('${uid}-wrap');");
  if (trSrc === before) { console.log('FAIL neuter_no_op_regex_drift'); }
}
eval(trSrc);                                   // ui/tool_rounds.js
// tool_rounds.js installs a real 1Hz setInterval (the countdown ticker) that
// keeps node's event loop alive forever — cancel it so the harness can exit.
if (win._timerCountdownTicker) { clearInterval(win._timerCountdownTicker); }
eval(fs.readFileSync(process.argv[3], 'utf8')); // upload_preview.js (installs click delegation)

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const FULL_ID = 'tmr_b3b3a438deadbeef';
const round = {
  roundNum: 3,
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
const _conv = { messages: [{ role: 'assistant', toolRounds: [round] }] };

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

// 3. model-view button exists (toolContent-backed) and, when clicked, reaches
//    the document delegation → opens the preview modal.
const mv = container.querySelector('[data-tc-preview],[data-tc-preview-text]');
check('model_view_btn_present', !!mv);
if (mv) {
  mv.dispatchEvent(new win.MouseEvent('click', { bubbles: true, cancelable: true }));
  const modalOpen = document.getElementById('previewModal').classList.contains('open');
  check('model_view_opens_modal', modalOpen);
  // reset for isolation
  document.getElementById('previewModal').classList.remove('open');
}

// 4. clicking the id chip copies the FULL id.
if (chip) {
  copiedValue = null;
  chip.dispatchEvent(new win.MouseEvent('click', { bubbles: true, cancelable: true }));
  check('id_chip_copies_full_id', copiedValue === FULL_ID);
}

console.log(out.join('\n'));
process.exit(0);
"""


def _run(neuter: bool):
    harness = os.path.join(HERE, '_timer_id_chip_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'ui', 'tool_rounds.js'),   # argv[2]
             os.path.join(JS_DIR, 'upload_preview.js'),       # argv[3]
             ROOT,                                            # argv[4]
             'neuter' if neuter else 'live',                  # argv[5]
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
def test_timer_id_chip_and_model_view():
    proc = _run(neuter=False)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'timer id-chip / model-view failures:\n' + output
    assert output.count('PASS') >= 7, f'expected >=7 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_neuter_unconditional_stopprop_swallows_model_view():
    """NEUTER: restore the old unconditional stopPropagation() on the header →
    the model-view click is swallowed and the modal never opens. Proves the
    bubble-through guard is what fixes issue #1."""
    proc = _run(neuter=True)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    assert 'FAIL neuter_no_op_regex_drift' not in output, (
        'NEUTER regex did not match — the header onclick guard string drifted:\n' + output
    )
    assert 'FAIL model_view_opens_modal' in output, (
        'NEUTER did not surface the swallowed click — the guard is NOT '
        'load-bearing (or the delegation opened the modal some other way):\n' + output
    )
