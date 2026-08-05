"""tests/test_frontend_timer_id_chip.py — the timer-watcher header's
owner-reported defects, policed under jsdom against the REAL shipped JS.

WHY
---
The collapsed/expanded timer card (``_renderTimerWatcherBlock`` in
``static/js/ui/tool_rounds.js``) had three problems (owner, 2026-07-16):

  1. Header-control clicks TOGGLED the card. The header's toggle ``onclick``
     called ``event.stopPropagation()`` UNCONDITIONALLY, so a click on a
     control inside the header also expanded/collapsed the card (and, for
     the since-removed model-view button, never reached the document-level
     delegation). FIX: the header onclick bails when the click lands on
     ``.timer-id-chip,.ri-tool-anchor`` — the two controls that can sit in
     the header today (the id chip + the debug entry).

  2. The long timer id was baked into the label TEXT (``定时器 tmr_xxx — …``)
     with no way to copy it. FIX: the id is extracted into a dedicated
     ``.timer-id-chip`` button whose click (delegated) copies the FULL id.

  3. Too many tiny glyphs cluttering the header. FIX: the id is one prominent
     token; the label no longer repeats it.

2026-07-28: the "模型原文" (model-view) button was removed from every tool
row per owner directive, so this suite now PINS ITS ABSENCE on the timer
header and re-points the click-guard contract at the surviving header
control — the ``</> R{n}`` debug entry (``.ri-tool-anchor``).

This harness loads the real ``tool_rounds.js`` + ``upload_preview.js`` under
jsdom, renders an active timer round (debug_mode on, llmRound + _taskId set
so the debug entry renders), and asserts:
  • a ``.timer-id-chip`` carrying the FULL id in ``data-timer-id`` exists;
  • the header LABEL text does NOT contain the raw id (it moved to the chip);
  • NO model-view control exists anywhere in the row;
  • the debug entry renders and clicking it calls openToolDebugPanel WITHOUT
    toggling the watcher body (the guard works);
  • clicking the id chip calls the clipboard writer with the full id.

A NEUTER drops ``.ri-tool-anchor`` from the guard selector → clicking the
debug entry toggles the body — proving the guard is load-bearing.
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
const ROOT = process.argv[5];
const NEUTER = process.argv[6] === 'neuter';
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
// runScripts:'dangerously' so the header's INLINE onclick attribute (set via
// innerHTML) actually executes — that inline handler is exactly what the
// guard lives in, so without this the contract can't be exercised.
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
// ── debug-panel spy: the debug entry's inline onclick calls this global ──
win.openToolDebugPanel = function () {
  win.__anchorCalls = (win.__anchorCalls || 0) + 1;
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
let richSrc = fs.readFileSync(process.argv[3], 'utf8'); // ui/tool_rounds_rich.js (timer watcher block)
if (NEUTER) {
  // Drop the debug-entry selector from the bubble-through guard — the click
  // then falls through to the toggle, proving the guard is load-bearing.
  const before = richSrc;
  richSrc = richSrc.replace(
    "onclick=\"if(event.target.closest('.timer-id-chip,.ri-tool-anchor'))return;event.stopPropagation();var w=document.getElementById('${uid}-wrap');",
    "onclick=\"event.stopPropagation();var w=document.getElementById('${uid}-wrap');");
  if (richSrc === before) { console.log('FAIL neuter_no_op_regex_drift'); }
}
// ONE eval so the rich block's top-level consts stay in scope for it.
eval(trSrc + '\n' + richSrc);
// tool_rounds_rich.js installs a real 1Hz setInterval (the countdown ticker) that
// keeps node's event loop alive forever — cancel it so the harness can exit.
if (win._timerCountdownTicker) { clearInterval(win._timerCountdownTicker); }
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

// 3. The model-view button is GONE (removed 2026-07-28 per owner) — neither
//    the toolContent-backed nor the registry-backed variant may reappear.
check('model_view_absent',
  !container.querySelector('[data-tc-preview],[data-tc-preview-text],.tc-preview-btn'));

// 4. The debug entry is the surviving header control. Clicking it must reach
//    openToolDebugPanel and must NOT toggle the watcher body.
const anchor = container.querySelector('.ri-tool-anchor');
check('debug_entry_present', !!anchor);
const bodyWrap = container.querySelector('#tmr-r3-wrap');
const expandedBefore = bodyWrap ? bodyWrap.classList.contains('expanded') : null;
if (anchor) {
  anchor.dispatchEvent(new win.MouseEvent('click', { bubbles: true, cancelable: true }));
  check('debug_click_opens_panel', (win.__anchorCalls || 0) === 1);
  const expandedAfter = bodyWrap && bodyWrap.classList.contains('expanded');
  check('debug_click_does_not_toggle', expandedBefore === expandedAfter);
}

// 5. clicking the id chip copies the FULL id.
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
             os.path.join(JS_DIR, 'ui', 'tool_rounds.js'),      # argv[2]
             os.path.join(JS_DIR, 'ui', 'tool_rounds_rich.js'), # argv[3]
             os.path.join(JS_DIR, 'upload_preview.js'),         # argv[4]
             ROOT,                                              # argv[5]
             'neuter' if neuter else 'live',                    # argv[6]
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
    proc = _run(neuter=False)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'timer id-chip / debug-entry failures:\n' + output
    assert output.count('PASS') >= 8, f'expected >=8 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_neuter_guard_dropped_debug_click_toggles():
    """NEUTER: drop `.ri-tool-anchor` from the header onclick guard → clicking
    the debug entry falls through to the toggle. Proves the bubble-through
    guard is what keeps header controls from expanding/collapsing the card."""
    proc = _run(neuter=True)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    assert 'FAIL neuter_no_op_regex_drift' not in output, (
        'NEUTER regex did not match — the header onclick guard string drifted:\n' + output
    )
    assert 'FAIL debug_click_does_not_toggle' in output, (
        'NEUTER did not surface the toggling click — the guard is NOT '
        'load-bearing:\n' + output
    )
