"""jsdom test: the swarm "Parallel Execution" panel must update IN PLACE, not
by tearing down and recreating its whole subtree on every SSE event.

Reported bug: "the subagent panel flickers every time it updates." Root cause —
every ``swarm_*`` SSE event (per-agent phase, each streamed preview char, each
tool-call tick) landed in ``_syncToolRoundsDOM`` and ran
``slot.innerHTML = _buildSwarmPanelHTML(...)``, a COMPLETE teardown+rebuild of
the ``.sw-panel`` subtree many times a second. Two visible effects: (1) the
brand-new ``.sw-panel.sw-active`` node restarts its ``swarmBorderPulse``
animation from 0% every event → the border/box-shadow flashes; (2) any agent
card the user expanded collapses.

Fix — ``_morphSwarmSlot`` patches the existing panel in place (reusing the live
DOM node so the animation clock is never interrupted), syncing only changed
attributes/text and treating the user-toggle classes (``sw-collapsed`` /
``sw-a-open`` / ``sw-tl-open``) as old-node-authoritative so expanded cards
survive. Same surgical-diff principle as the 1 Hz ``[data-sw-start]`` ticker in
the same file and ``renderChat``'s ``data-mfp`` message diff.

Harness mirrors tests/test_frontend_debug_preserve_open.py. Skips cleanly when
node + jsdom aren't installed.

NEGATIVE CONTROL (patches a COPY; the shipped file stays byte-identical):
  • degrade ``_morphSwarmSlot`` to the old ``slot.innerHTML = html`` behavior →
    the panel node identity changes (teardown = flicker) AND the expanded agent
    card collapses — proving the in-place morph is what fixes the reported bug.
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
_SWARM_SRC = os.path.join(JS_DIR, 'ui', 'streaming_swarm_panel.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="slot"></div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;

// Prevent the file's load-time setInterval tickers (reconciler + 1 Hz timer)
// from keeping the node event loop alive → the subprocess would hang.
win._swReconcileTicker = 1;
win._swTimerTicker = 1;

win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
win.t = global.t = (k) => k;
win._TOOL_DISPLAY = global._TOOL_DISPLAY = {};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // ui/streaming_swarm_panel.js (maybe patched)

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof _buildSwarmPanelHTML !== 'function' || typeof _morphSwarmSlot !== 'function') {
  console.log('FAIL fn_present builders not defined');
  console.log(out.join('\n'));
  process.exit(0);
}
check('fn_present', true);

const slot = document.getElementById('slot');

// ── Round object with two running agents, one streaming a preview ──
function mkRound() {
  return {
    roundNum: 1, _swarm: true, _swarmActive: true, status: 'searching',
    _swarmStartTime: Date.now() - 3000,
    _swarmAgents: [
      { id: 'agent-aaaaaaaa', role: 'researcher', objective: 'Investigate X',
        status: 'running', phase: 'thinking', preview: 'Looking', tools: [] },
      { id: 'agent-bbbbbbbb', role: 'coder', objective: 'Audit Y',
        status: 'running', phase: 'thinking', preview: 'Reading', tools: [] },
    ],
  };
}

let round = mkRound();

// ── First render: seed the slot (falls back to innerHTML — genuine first paint) ──
_morphSwarmSlot(slot, _buildSwarmPanelHTML(round, [round]));
const panel0 = slot.firstElementChild;
check('panel_rendered', !!panel0 && panel0.classList.contains('sw-panel'));
check('panel_active_pulse', !!panel0 && panel0.classList.contains('sw-active'));

// ── User expands agent #1's card (adds sw-a-open) ──
const card0 = panel0.querySelector('.sw-agent[data-agent-id="agent-aaaaaaaa"]');
check('agent_card_present', !!card0);
// The .sw-a-header carries an inline onclick that toggles 'sw-a-open' on the
// card. jsdom (no runScripts) doesn't compile inline handlers to callables, so
// simulate the user's expand by applying the exact class the handler toggles.
card0.classList.add('sw-a-open');
check('agent_card_open_after_click', card0.classList.contains('sw-a-open'));
// Stamp a marker on the live node so we can prove node IDENTITY is preserved.
panel0._identityMark = 'PANEL-KEEP';
card0._identityMark = 'CARD-KEEP';

// ── An SSE update lands: agent #1's preview grows + phase changes, a THIRD
//   agent is spawned. This is the per-event churn that used to flicker. ──
round._swarmAgents[0].preview = 'Looking deeper into the problem now';
round._swarmAgents[0].phase = 'tool_use';
round._swarmAgents[0].tools = ['web_search'];
round._swarmAgents.push(
  { id: 'agent-cccccccc', role: 'analyst', objective: 'Crunch Z',
    status: 'running', phase: 'thinking', preview: '', tools: [] });

_morphSwarmSlot(slot, _buildSwarmPanelHTML(round, [round]));

const panel1 = slot.firstElementChild;
// ── The panel DOM node must be the SAME object (no teardown → no animation
//   restart → no flicker). This is the core assertion. ──
check('PANEL_NODE_IDENTITY_PRESERVED', panel1 === panel0 && panel1._identityMark === 'PANEL-KEEP');

// ── The expanded agent card must survive as the same node, still open. ──
const card0b = panel1.querySelector('.sw-agent[data-agent-id="agent-aaaaaaaa"]');
check('EXPANDED_CARD_SURVIVES', !!card0b && card0b._identityMark === 'CARD-KEEP'
  && card0b.classList.contains('sw-a-open'));

// ── The streamed content must actually update (morph is not a no-op). ──
const prevText = panel1.querySelector('.sw-agent[data-agent-id="agent-aaaaaaaa"] .sw-a-preview');
check('PREVIEW_TEXT_UPDATED', !!prevText && prevText.textContent.indexOf('deeper into the problem') >= 0);

// ── The newly-spawned third agent card must appear. ──
check('NEW_AGENT_APPENDED',
  !!panel1.querySelector('.sw-agent[data-agent-id="agent-cccccccc"]'));

// ── A second identical update must be a genuine no-op on unchanged text nodes
//   (identity still preserved, still open). ──
_morphSwarmSlot(slot, _buildSwarmPanelHTML(round, [round]));
const panel2 = slot.firstElementChild;
check('IDENTITY_STABLE_ACROSS_MULTIPLE_UPDATES', panel2 === panel0);
const card0c = panel2.querySelector('.sw-agent[data-agent-id="agent-aaaaaaaa"]');
check('CARD_STILL_OPEN_AFTER_SECOND_UPDATE', !!card0c && card0c.classList.contains('sw-a-open'));

console.log(out.join('\n'));
"""


def _run(js_path: str) -> str:
    harness = os.path.join(HERE, '_swarm_no_flicker_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, js_path, ROOT],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_swarm_panel_updates_in_place():
    output = _run(_SWARM_SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'swarm-panel no-flicker failures:\n' + output
    assert output.count('PASS') >= 10, f'expected >=10 PASS lines, got:\n{output}'
    print(output)


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NEUTER_full_innerHTML_rebuild_flickers_and_collapses():
    """Patch a COPY so ``_morphSwarmSlot`` degrades to the old
    ``slot.innerHTML = html`` behavior (a full teardown/rebuild). The panel
    node identity MUST then change (teardown = the flicker) AND the expanded
    agent card MUST collapse — proving the in-place morph is load-bearing."""
    with open(_SWARM_SRC, encoding='utf-8') as f:
        src = f.read()
    # Replace the morph function body with the pre-fix behavior. Anchor on the
    # unique opening line so the neuter can't accidentally match elsewhere.
    needle = 'function _morphSwarmSlot(slot, html) {'
    assert needle in src, 'anchor for neuter not found — did the fix change shape?'
    patched = src.replace(
        needle,
        'function _morphSwarmSlot(slot, html) { slot.innerHTML = html; return; } '
        'function _morphSwarmSlot_ORIG(slot, html) {',
        1,
    )
    assert patched != src, 'neuter did not modify the source'
    tmp = os.path.join(HERE, '_swarm_panel_neutered.js')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(patched)
    try:
        output = _run(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    # With a full-rebuild, the panel node identity is lost and the expanded
    # card collapses.
    assert 'FAIL PANEL_NODE_IDENTITY_PRESERVED' in output, (
        'neutered (full-rebuild) build unexpectedly preserved node identity — '
        'the assertion is not load-bearing:\n' + output)
    assert 'FAIL EXPANDED_CARD_SURVIVES' in output, (
        'neutered build unexpectedly kept the card open:\n' + output)
    # Shipped file must be untouched.
    with open(_SWARM_SRC, encoding='utf-8') as f:
        assert f.read() == src, 'shipped streaming_swarm_panel.js must be byte-identical'


if __name__ == '__main__':
    print(_run(_SWARM_SRC))
