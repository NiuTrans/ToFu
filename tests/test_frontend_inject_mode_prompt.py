"""tests/test_frontend_inject_mode_prompt.py — the post-send inject-mode
chooser (redesign of the old always-on composer toggle).

A message the user SENDS WHILE a turn is generating should prompt HOW to deliver
it (steer into the running reply vs queue a fresh turn). A message sent when
NOTHING is running must send normally — zero extra UI. This suite drives the
REAL shipped JS under jsdom:

  * ``static/js/core/dialog.js``  — the ``showChoice`` themed chooser.
  * ``static/js/main/main_send_pipeline.js`` — ``_promptInjectMode``.

Invariants pinned (each with a byte-reverting NEUTER):
  1. showChoice resolves the CHOSEN option's value; a click on 'steer' → 'steer'.
  2. showChoice AUTO-RESOLVES to the dismiss value when ``liveCheck`` goes false
     (the running turn ended while the dialog was open) — no stuck dialog.
  3. _promptInjectMode returns 'steer' / 'queue' straight from the choice, and
     never crashes when the dialog base isn't loaded (returns 'queue').

Skips cleanly when node + jsdom aren't installed.

Run::

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_frontend_inject_mode_prompt.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_sections_dir

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = runtime_sections_dir()


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


# The harness boots jsdom, loads dialog.js (real showChoice), then extracts and
# loads ONLY _promptInjectMode from main_send_pipeline.js (the surrounding file
# pulls the whole app, so we splice out just the function under test). It then
# runs scripted scenarios and prints a JSON verdict.
_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[1];
const SOURCES = process.argv[2];
const NEUTER = process.argv[3] === 'neuter';
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => setTimeout(fn, 0);

// Minimal i18n: identity (return key) so labels fall back to code defaults.
global.t = win.t = (k) => k;

// ── Load the real showChoice from dialog.js ──
const dialogSrc = fs.readFileSync(
  path.join(SOURCES, 'core', 'dialog.js'), 'utf8');
eval(dialogSrc);   // defines showChoice etc. on this scope + window

const NEUTER_ARROWS = process.argv[3] === 'neuter_arrows';

if (NEUTER) {
  // NEUTER: break the liveCheck auto-resolve — the dialog will then hang open
  // forever even when the running turn ends. Scenario 2 must FAIL under this.
  const _orig = win.showChoice;
  global.showChoice = win.showChoice = function(cfg) {
    const c = Object.assign({}, cfg);
    delete c.liveCheck;              // drop the auto-resolve
    return _orig(c);
  };
}

if (NEUTER_ARROWS) {
  // NEUTER: swallow ArrowDown/ArrowUp at the capture phase BEFORE showChoice's
  // handler runs, so focus never moves. Scenario 4 must then FAIL (focus stays
  // on the first button after ArrowDown). Proves the arrow handler is
  // load-bearing.
  win.document.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') e.stopImmediatePropagation();
  }, true);
}

// ── Extract _promptInjectMode from the shipped send pipeline ──
const sendSrc = fs.readFileSync(
  path.join(SOURCES, 'main', 'main_send_pipeline.js'), 'utf8');
const m = sendSrc.match(/async function _promptInjectMode\([\s\S]*?\n\}\n/);
if (!m) { console.log(JSON.stringify({error: '_promptInjectMode not found'})); process.exit(0); }
// conversations + activeStreams globals the liveCheck reads.
let conversations = [];
const activeStreams = new Map();
win.conversations = conversations;
win.activeStreams = activeStreams;
global.conversations = conversations;
global.activeStreams = activeStreams;
global.showChoice = win.showChoice;
eval(m[0]);   // defines _promptInjectMode

// Click the option button in the NEWEST overlay (index 0=steer, 1=queue),
// then purge any lingering overlays so the next scenario starts clean (the
// real close() schedules removal at +160ms; we don't want to await that).
function clickChoice(value) {
  setTimeout(() => {
    const overlays = [...win.document.querySelectorAll('.app-dialog-overlay')];
    const newest = overlays[overlays.length - 1];
    if (!newest) return;
    const btns = [...newest.querySelectorAll('.app-choice-btn')];
    const idx = value === 'steer' ? 0 : 1;
    if (btns[idx]) btns[idx].click();
    // Drop any older overlays immediately so querySelectorAll stays unambiguous.
    overlays.slice(0, -1).forEach((o) => o.remove());
  }, 15);
}

(async () => {
  const out = {};

  // ── Scenario 1: user picks steer → 'steer' ──
  conversations.length = 0;
  conversations.push({ id: 'c1', activeTaskId: 't1' });
  clickChoice('steer');
  out.pick_steer = await _promptInjectMode('c1');
  win.document.querySelectorAll('.app-dialog-overlay').forEach((o) => o.remove());

  // ── Scenario 1b: user picks queue → 'queue' ──
  clickChoice('queue');
  out.pick_queue = await _promptInjectMode('c1');
  win.document.querySelectorAll('.app-dialog-overlay').forEach((o) => o.remove());

  // ── Scenario 2: turn ENDS while dialog open → auto-resolve to 'queue' ──
  conversations.length = 0;
  conversations.push({ id: 'c2', activeTaskId: 't2' });
  const p = _promptInjectMode('c2');
  setTimeout(() => {
    // The running turn ends: clear activeTaskId so liveCheck() goes false.
    conversations[0].activeTaskId = null;
  }, 30);
  // Give the liveCheck poll (250ms) time to fire. Cap so a hung dialog (NEUTER)
  // is detected as a timeout rather than hanging the test.
  out.auto_resolve = await Promise.race([
    p,
    new Promise((res) => setTimeout(() => res('__TIMEOUT__'), 2000)),
  ]);
  win.document.querySelectorAll('.app-dialog-overlay').forEach((o) => o.remove());

  // ── Scenario 4: ArrowDown/ArrowUp move focus between option buttons ──
  {
    const p2 = showChoice({
      title: 'nav',
      options: [
        { value: 'steer', label: 'Steer', accent: true },
        { value: 'queue', label: 'Queue' },
      ],
      dismissValue: 'queue',
    });
    // Wait for the rAF-deferred initial focus, then fire the arrow keys.
    await new Promise((res) => setTimeout(res, 20));
    const overlay = [...win.document.querySelectorAll('.app-dialog-overlay')].pop();
    const btns = [...overlay.querySelectorAll('.app-choice-btn')];
    out.arrow_first_focus = (win.document.activeElement === btns[0]);
    const fire = (key) => win.document.dispatchEvent(
      new win.KeyboardEvent('keydown', { key, bubbles: true, cancelable: true }));
    fire('ArrowDown');
    out.arrow_down_moves = (win.document.activeElement === btns[1]);
    fire('ArrowDown');   // wraps back to first
    out.arrow_down_wraps = (win.document.activeElement === btns[0]);
    fire('ArrowUp');     // wraps to last
    out.arrow_up_wraps = (win.document.activeElement === btns[1]);
    // Activate the focused (second) button with a click to resolve the promise.
    btns[1].click();
    out.arrow_activate = await p2;
    win.document.querySelectorAll('.app-dialog-overlay').forEach((o) => o.remove());
  }

  // ── Scenario 3: no dialog base → graceful 'queue' ──
  const savedSC = win.showChoice;
  global.showChoice = win.showChoice = undefined;
  out.no_dialog = await _promptInjectMode('c3');
  global.showChoice = win.showChoice = savedSC;

  console.log(JSON.stringify(out));
  process.exit(0);
})().catch((e) => { console.log(JSON.stringify({ error: String(e && e.stack || e) })); process.exit(0); });
"""


def _run(neuter: bool = False, mode: str | None = None) -> dict:
    arg = mode if mode else ('neuter' if neuter else 'normal')
    proc = subprocess.run(
        ['node', '-e', _HARNESS, ROOT, JS_DIR, arg],
        capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert proc.returncode == 0, f'node harness failed: {proc.stderr[:2000]}'
    line = [ln for ln in proc.stdout.strip().splitlines() if ln.startswith('{')][-1]
    return json.loads(line)


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_prompt_returns_chosen_channel():
    r = _run()
    assert 'error' not in r, r.get('error')
    assert r['pick_steer'] == 'steer', \
        "clicking 'Steer into this reply' must yield injectMode='steer'"
    assert r['pick_queue'] == 'queue', \
        "clicking 'Queue as the next turn' must yield injectMode='queue'"


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_dialog_auto_resolves_when_turn_ends():
    r = _run()
    assert r['auto_resolve'] == 'queue', (
        "when the running turn ends while the chooser is open, liveCheck must "
        f"auto-close it to the safe 'queue' default (got {r['auto_resolve']!r})")


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_graceful_without_dialog_base():
    r = _run()
    assert r['no_dialog'] == 'queue', \
        'with no showChoice available, _promptInjectMode must fall back to queue'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NEUTER_no_livecheck_leaves_dialog_stuck():
    """Byte-reverting NEUTER: strip the liveCheck auto-resolve → the dialog
    hangs open after the turn ends, so scenario 2 times out instead of
    resolving to 'queue'. Proves the liveCheck is load-bearing."""
    r = _run(neuter=True)
    assert r['auto_resolve'] == '__TIMEOUT__', (
        'NEUTER: without liveCheck the moot dialog must NOT auto-resolve '
        f'(expected a timeout, got {r["auto_resolve"]!r})')


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_arrow_keys_navigate_options():
    r = _run()
    assert 'error' not in r, r.get('error')
    assert r['arrow_first_focus'] is True, \
        'the first (accent) option must be focused when the chooser opens'
    assert r['arrow_down_moves'] is True, \
        'ArrowDown must move focus to the next option button'
    assert r['arrow_down_wraps'] is True, \
        'ArrowDown on the last option must wrap focus back to the first'
    assert r['arrow_up_wraps'] is True, \
        'ArrowUp on the first option must wrap focus to the last'
    assert r['arrow_activate'] == 'queue', \
        'activating the arrow-focused second option must resolve its value'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NEUTER_no_arrow_nav_leaves_focus_stuck():
    """Byte-reverting NEUTER: swallow the arrow keydown before showChoice sees
    it → focus never moves off the first button. Proves the arrow-navigation
    handler is load-bearing."""
    r = _run(mode='neuter_arrows')
    assert r['arrow_down_moves'] is False, (
        'NEUTER: with the arrow handler suppressed, ArrowDown must NOT move '
        f'focus (got arrow_down_moves={r["arrow_down_moves"]!r})')


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
