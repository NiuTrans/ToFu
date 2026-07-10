"""tests/test_frontend_autopilot_inline_summary.py — a concluded autopilot
run's close-out REPORT renders as a STANDALONE boundary node, always-visible,
docked at the run's true boundary (NOT grafted onto a neighbouring bubble).

WHY
---
The 2026-07-07 "flatten" deleted the `autopilot-run-fold` `<details>` card that
used to AUTO-RENDER a concluded run's report. After the flatten the report was
reachable only via a hover "View report" button → functionally invisible. A
first fix grafted an inline panel onto a proxy message row, but the anchors were
strange: a run's report is a SIDECAR fact (conv.autopilotSummaries[runId]), not
a messages[] entry, so grafting it under the newest VU turn put the AGENT's
work-summary beneath a synthetic USER bubble, and the compaction tail-fallback
piled orphaned reports onto whatever unrelated message happened to be last.

The clean design (this file): the report is rendered as a STANDALONE node by a
post-render DOM pass `_applyAutopilotSummaryPanels(inner, conv)` — mirroring the
existing `_applyAutopilotRunFolds` hook, invoked at every render exit — that
docks each report at the run's TRUE BOUNDARY (right after the run's last stamped
turn `#msg-N`), never inside a message. `_apSummaryPlacements(conv)` resolves
the dock point: `{runId, afterMsgIdx}` at the boundary, or `{runId, tail}` for a
compacted run whose turns didn't survive the window. The panel is NEVER a
`msg-N` node, so renderChat's index-based surgical diff ignores it; the pass is
idempotent (clear-all + re-insert). A background-sync arrival is caught by the
conv-level Guard 2 fingerprint (`_apSummariesFp` in core.js), NOT `_msgFingerprint`.

This test drives the REAL shipped helpers via jsdom:

  Case A (placements): a clean-TASK_DONE run with surviving turns resolves to a
    boundary placement `{afterMsgIdx}` = the run's LAST stamped turn; a run with
    no report content yields no placement; a compacted run (no surviving turn)
    resolves to a `{tail:true}` placement.
  Case B (boundary DOM): the pass inserts an OPEN `<details.ap-summary-panel>`
    as a direct child of #chatInner immediately AFTER the boundary turn's
    `#msg-N`, with the report body in the initial DOM (no click), and it is NOT
    nested inside any `.message` bubble.
  Case C (compacted tail): a report whose run has no surviving `#msg-N` is
    appended at the END of #chatInner (reachable, not orphaned).
  Case D (idempotent): running the pass twice yields exactly ONE panel per run
    (no duplication across re-renders).
  Case E (wiring): all three render exits in chat_render.js call
    `_applyAutopilotSummaryPanels` (source-level assertion).
  Case F (fingerprint clean): `_msgFingerprint` does NOT carry a `:aps` token —
    the report is out of the per-message fingerprint by design.

NEUTER CONTROLS
  • NC-1 (pass neutered): make `_applyAutopilotSummaryPanels` a no-op → Case B's
    boundary panel disappears → the "panel present" assertion FAILS.
  • NC-2 (boundary→tail collapse): force `_apSummaryPlacements` to drop the
    boundary branch (everything becomes a tail placement) → Case B's
    "docked after #msg-N" assertion FAILS (panel lands at the tail instead).

Skips cleanly when node/jsdom isn't installed.
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
ESCAPE_HTML = os.path.join(JS_DIR, 'core', 'escape_html.js')
SAFE_HTML = os.path.join(JS_DIR, 'core', 'safe_html.js')
CHAT_RENDER = os.path.join(JS_DIR, 'ui', 'chat_render.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[5];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner"></div></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

let _activeConv = { id: 'c', messages: [] };
win.activeStreams = global.activeStreams = new Map();
win.activeConvId = global.activeConvId = 'c';
win.getActiveConv = global.getActiveConv = () => _activeConv;
win.t = global.t = (k) => k;   // fall back to hardcoded English labels
win.renderMarkdown = global.renderMarkdown = (s) => '<md>' + String(s == null ? '' : s) + '</md>';
win.convAutoTranslate = global.convAutoTranslate = () => false;

const CHAT = fs.readFileSync(process.argv[2], 'utf8');
const NC = process.argv[6] || '';
let chatSrc = CHAT;
if (NC === 'nc_neuter_pass') {
  // NC-1: make the summary-panel placement pass a no-op.
  chatSrc = CHAT.replace(
    'function _applyAutopilotSummaryPanels(inner, conv) {\n  if (!inner) return;',
    'function _applyAutopilotSummaryPanels(inner, conv) {\n  if (inner) return;  /* neutered */\n  if (!inner) return;');
} else if (NC === 'nc_boundary_to_tail') {
  // NC-2: collapse the boundary branch so every report becomes a tail
  // placement — the report should then NOT dock after its #msg-N.
  chatSrc = CHAT.replace(
    'if (lastIdxByRun.has(runId)) {',
    'if (false && lastIdxByRun.has(runId)) {');
}
const _applied = (NC === '') || (chatSrc !== CHAT);
check('nc_pattern_applied', _applied);

(0, eval)(fs.readFileSync(process.argv[3], 'utf8'));  // escape_html.js
(0, eval)(fs.readFileSync(process.argv[4], 'utf8'));  // safe_html.js
(0, eval)(chatSrc);                                   // chat_render.js (real / neutered)

for (const fn of ['_apSummaryPlacements', '_applyAutopilotSummaryPanels',
                  '_apReportPanelHTML', '_msgFingerprint']) {
  if (typeof eval(fn) !== 'function') { console.log('FAIL fn_exposed ' + fn); process.exit(0); }
}
check('fn_exposed', true);

const inner = win.document.getElementById('chatInner');
function resetInner() { inner.innerHTML = ''; }
// Build the message-row DOM the real renderChat would have produced.
function paintRows(conv) {
  resetInner();
  conv.messages.forEach((m, i) => {
    const d = win.document.createElement('div');
    d.id = 'msg-' + i;
    d.className = 'message ' + (m.role === 'assistant' ? 'assistant' : 'user');
    d.textContent = (m.role || '') + ' ' + i;
    inner.appendChild(d);
  });
}
function vu(runId) { return { role: 'user', _isVirtualUser: true, _autopilotRunId: runId, content: 'go on' }; }
function agent(txt) { return { role: 'assistant', content: txt || 'agent reply' }; }
function human(txt) { return { role: 'user', content: txt || 'human' }; }

// ══ Case A — _apSummaryPlacements resolves boundary / none / tail ══
{
  // Run R1: human, agentReply, vu(R1)  → boundary = idx 2 (last stamped turn).
  _activeConv = { id: 'cA', messages: [human('obj'), agent(), vu('R1')],
    autopilotSummaries: { R1: { runId: 'R1', status: 'concluded',
      reason: 'task_done', content: '# Report\nDone.', ts: 1 } } };
  const pl = _apSummaryPlacements(_activeConv);
  check('A_one_placement', pl.length === 1);
  check('A_boundary_is_last_stamped', pl[0] && pl[0].runId === 'R1' && pl[0].afterMsgIdx === 2 && !pl[0].tail);

  // A record with NO content → no placement.
  _activeConv.autopilotSummaries = { R2: { runId: 'R2', status: 'concluded', reason: 'stopped', ts: 2 } };
  _activeConv.messages = [human('obj'), vu('R2')];
  check('A_no_content_no_placement', _apSummaryPlacements(_activeConv).length === 0);

  // Compacted: report exists, run has NO surviving turn → tail placement.
  _activeConv.messages = [human('later'), agent('later reply')];
  _activeConv.autopilotSummaries = { RC: { runId: 'RC', status: 'concluded',
    reason: 'task_done', content: '# Compacted\nSurvived.', ts: 3 } };
  const plC = _apSummaryPlacements(_activeConv);
  check('A_compacted_is_tail', plC.length === 1 && plC[0].runId === 'RC' && plC[0].tail === true);
}

// ══ Case B — the pass docks an OPEN panel right AFTER the boundary #msg-N ══
//    The run's boundary (last stamped turn) is #msg-2 (the VU turn); a LATER
//    unrelated turn (#msg-3) follows it, so a correct boundary dock lands after
//    #msg-2 (mid-list), distinguishable from a tail append after #msg-3.
{
  _activeConv = { id: 'cB', messages: [human('obj'), agent(), vu('R1'), human('next unrelated turn')],
    autopilotSummaries: { R1: { runId: 'R1', status: 'concluded',
      reason: 'task_done', content: '# Report\nBoundary body.', ts: 1 } } };
  paintRows(_activeConv);
  _applyAutopilotSummaryPanels(inner, _activeConv);
  const panel = inner.querySelector(':scope > details.ap-summary-panel');
  check('B_panel_present', !!panel);
  check('B_panel_open', panel && panel.hasAttribute('open'));
  check('B_body_present', inner.innerHTML.indexOf('Boundary body.') !== -1);
  // Docked immediately after the boundary turn (#msg-2), as a SIBLING — and
  // BEFORE the later unrelated #msg-3 (proves boundary, not tail append).
  const boundaryEl = win.document.getElementById('msg-2');
  check('B_docked_after_boundary', boundaryEl && boundaryEl.nextElementSibling === panel);
  check('B_before_later_turn', panel && panel.nextElementSibling === win.document.getElementById('msg-3'));
  check('B_not_last_child', inner.lastElementChild !== panel);
  // NOT nested inside any .message bubble.
  check('B_not_nested_in_message', panel && !panel.closest('.message'));
  // Run id carried for the idempotent removal selector.
  check('B_carries_run_attr', panel && panel.getAttribute('data-ap-report-run') === 'R1');
}

// ══ Case C — compacted report appended at the tail (reachable) ══
{
  _activeConv = { id: 'cC', messages: [human('later'), agent('later reply')],
    autopilotSummaries: { RC: { runId: 'RC', status: 'concluded',
      reason: 'task_done', content: '# Compacted\nStill here.', ts: 3 } } };
  paintRows(_activeConv);
  _applyAutopilotSummaryPanels(inner, _activeConv);
  const panel = inner.querySelector(':scope > details.ap-summary-panel');
  check('C_tail_panel_present', !!panel);
  // It's the LAST child of #chatInner.
  check('C_panel_is_last_child', panel && inner.lastElementChild === panel);
  check('C_tail_body_present', inner.innerHTML.indexOf('Still here.') !== -1);
}

// ══ Case D — idempotent: two passes → exactly one panel per run ══
{
  _activeConv = { id: 'cD', messages: [human('obj'), agent(), vu('R1')],
    autopilotSummaries: { R1: { runId: 'R1', status: 'concluded',
      reason: 'task_done', content: '# Report', ts: 1 } } };
  paintRows(_activeConv);
  _applyAutopilotSummaryPanels(inner, _activeConv);
  _applyAutopilotSummaryPanels(inner, _activeConv);   // re-render
  check('D_single_panel_after_two_passes',
        inner.querySelectorAll('details.ap-summary-panel').length === 1);
}

// ══ Case F — _msgFingerprint carries NO :aps token (report is out of it) ══
{
  const m = agent('x');
  const fp = _msgFingerprint(m);
  check('F_no_aps_token', typeof fp === 'string' && fp.indexOf(':aps') === -1);
}

console.log(out.join('\n'));
process.exit(0);
"""


def _run(nc: str = '') -> str:
    harness = os.path.join(HERE, f'_ap_inline_summary_harness_{nc or "main"}.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, CHAT_RENDER, ESCAPE_HTML, SAFE_HTML, ROOT, nc],
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


# ── Case E — source-level: all three render exits call the panel pass ──
def _render_exit_call_count() -> int:
    with open(CHAT_RENDER, encoding='utf-8') as f:
        src = f.read()
    return src.count('_applyAutopilotSummaryPanels(inner, conv)')


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_summary_panel_docks_at_run_boundary():
    output = _run('')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'autopilot summary-panel failures:\n' + output
    assert output.count('PASS') >= 18, f'expected >=18 PASS lines, got:\n{output}'


def test_all_three_render_exits_apply_panels():
    """Case E: surgical + full + bg-refresh exits each invoke the placement pass."""
    n = _render_exit_call_count()
    assert n >= 3, (
        f'expected >=3 call-sites of _applyAutopilotSummaryPanels (surgical/full/'
        f'bg-refresh render exits), found {n}')


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_nc_neutering_the_pass_is_caught():
    """NC-1: a no-op placement pass must break the boundary-panel presence check."""
    output = _run('nc_neuter_pass')
    assert 'PASS nc_pattern_applied' in output, f'NC mutation did not apply:\n{output}'
    assert 'FAIL B_panel_present' in output, (
        'Neutering the placement pass did NOT fail the panel-present assertion — '
        f'it is not load-bearing:\n{output}')


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_nc_boundary_collapse_is_caught():
    """NC-2: collapsing boundary→tail must break the 'docked after #msg-N' check."""
    output = _run('nc_boundary_to_tail')
    assert 'PASS nc_pattern_applied' in output, f'NC mutation did not apply:\n{output}'
    assert 'FAIL B_docked_after_boundary' in output, (
        'Collapsing the boundary branch did NOT fail the boundary-dock assertion '
        f'— the boundary placement is not load-bearing:\n{output}')


if __name__ == '__main__':
    test_all_three_render_exits_apply_panels()
    if not _node_deps_available():
        print('SKIP — node + jsdom not available')
    else:
        test_summary_panel_docks_at_run_boundary()
        test_nc_neutering_the_pass_is_caught()
        test_nc_boundary_collapse_is_caught()
        print('PASS test_frontend_autopilot_inline_summary')
