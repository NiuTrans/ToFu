"""tests/test_frontend_autopilot_flat_render.py — autopilot VU turns render
FLAT (identical agent bubble) and their delete affordance is REACHABLE.

WHY
---
Owner directive (2026-07-07, option A "full flatten"): an autopilot
virtual-user (VU) turn must render with the IDENTICAL agent bubble layout —
plain markdown body, the normal grouped tool panel, the normal thinking block —
and the ONLY visible difference is the avatar + the "Autopilot" label. The old
provenance framing (the dashed "Private · not sent" zone, the green "Sent to
the agent" zone, the "Autopilot investigation" header) is GONE.

Crucially, the real root of the reported "can't delete an autopilot message"
symptom was the `autopilot-run-fold` `<details>`: `_applyAutopilotRunFolds`
moved concluded VU turns into a collapsed card where the per-message hover
action bar (incl. delete) was never reachable. That grouping is now REMOVED —
the function instead defensively UNWRAPS any stale fold so the transcript is a
flat sequence. This test guards both halves:

  (a) renderMessage(VU) → agent bubble structure:
        • the `.md-content` reply body is present (plain, like an agent turn);
        • the grouped tool panel (ptool-panel) + a standalone thinking-block
          are present (routed straight into the body, NOT a private zone);
        • NO `.vu-private-zone`, NO `.vu-sent-zone`, NO `.vu-investigation-header`;
        • the avatar is the Autopilot (critic) glyph and the role label reads
          "Autopilot" (this is the ONLY who-is-speaking signal);
        • the per-message action bar carries a REACHABLE `.msg-delete-btn`
          calling deleteTurn(idx) — not buried in a collapsed fold.
  (b) _applyAutopilotRunFolds no longer FOLDS: given a concluded run's DOM it
        creates NO `<details.autopilot-run-fold>` and leaves every VU turn a
        direct child of chatInner (hover-able); and it UNWRAPS a pre-existing
        stale fold, hoisting the turns back to the flat level.

NEUTER CONTROLS
  • NC-1 (flatten regression): re-introduce the private-zone wrapper in a COPY
    of chat_render.js → the VU render grows a `.vu-private-zone` again → the
    "no private zone" assertion FAILS. Proves that assertion is load-bearing.
  • NC-2 (fold regression): replace the unwrap body with a real fold that wraps
    the run in `<details.autopilot-run-fold>` → the "no fold / turns are flat"
    assertions FAIL. Proves the flatten-not-fold contract is load-bearing.

Skips cleanly when node isn't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
ESCAPE_HTML = runtime_section_path('core/escape_html.js')
SAFE_HTML = runtime_section_path('core/safe_html.js')
TRANSLATION_MODEL = runtime_section_path('core/translation_model.js')
TRANSLATION_INDICATOR = runtime_section_path('ui/translation_indicator.js')
CHAT_RENDER = runtime_section_path('ui/chat_render.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[5];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatInner"></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
global.setTimeout = win.setTimeout = (fn) => 0;
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => 0;
win.CSS = global.CSS = { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, '\\$&') };

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── An idle conv so renderMessage's `canDelete` (no active stream/task) is
//    TRUE → the delete button is emitted. getActiveConv is read by both the
//    action-bar block and the finish-bar live-tail guard. ──
const _conv = { id: 'c-flat', messages: [], activeTaskId: null };
win.activeStreams = global.activeStreams = new Map();     // no live stream
win.conversations = global.conversations = [_conv];
win.activeConvId = global.activeConvId = 'c-flat';
win.getActiveConv = global.getActiveConv = () => _conv;

win.t = global.t = (k) => k;   // fall back to hardcoded English labels
win._fmtAbsoluteDateTime = global._fmtAbsoluteDateTime = () => '';
win.stripNoTranslateTags = global.stripNoTranslateTags = (s) => (s == null ? '' : String(s));
win.renderMarkdown = global.renderMarkdown = (s) => '<md>' + String(s == null ? '' : s) + '</md>';
// getToolRoundsFromMsg lives in another module — return the msg's toolRounds
// so the REAL grouped tool-panel path renders.
win.getToolRoundsFromMsg = global.getToolRoundsFromMsg = (m) => (m && m.toolRounds) || [];
// The grouped tool panel renderer lives in tool_rounds.js (not eval'd here);
// stub it to emit the ptool-panel marker so we can assert the body carries the
// NORMAL agent tool panel (this is the render path VU now takes — no header).
win.renderToolRoundsHTML = global.renderToolRoundsHTML = () => '<div class="ptool-panel">TOOLS</div>';
// Segment timeline: the VU message carries no `segments`, so the real
// renderSegmentTimelineHTML would no-op; stub it to '' to force the grouped
// (segment-less) fallback deterministically.
win.renderSegmentTimelineHTML = global.renderSegmentTimelineHTML = () => '';

const _noop = () => '';
for (const name of [
  'renderMcpLoginHintHtml','renderTurnProvenanceHtml','renderFileChangesBar',
  'renderErrorEnvelope','renderBranchZone','renderTurnCtxNote',
  'renderPreferenceLearnedHtml','renderFinishInfo','_buildSwarmInboxChipsHTML',
  '_injectAnchoredBranches','_prefetchConvCosts','_prefetchConvFileChanges',
  '_stampFreshness','buildTurnNav','calcCostCny',
]) {
  if (typeof win[name] === 'undefined') { win[name] = global[name] = _noop; }
}
win._USER_AVATAR_SVG = global._USER_AVATAR_SVG = '<img data-avatar="onigiri">';
win._TOFU_WORKER_SVG = global._TOFU_WORKER_SVG = '<img data-avatar="worker">';
win._TOFU_PLANNER_SVG = global._TOFU_PLANNER_SVG = '<img data-avatar="planner">';
win._TOFU_CRITIC_SVG = global._TOFU_CRITIC_SVG = '<img data-avatar="critic">';
win.BASE_PATH = global.BASE_PATH = '';
win._INITIAL_RENDER = global._INITIAL_RENDER = 20;

const CHAT = fs.readFileSync(process.argv[2], 'utf8');
// The test harness may run a NEUTERED copy of chat_render.js (argv[6] holds a
// marker selecting which mutation to apply). Default: the shipped file.
const NC = process.argv[6] || '';
let chatSrc = CHAT;
if (NC === 'nc_private') {
  // NC-1: re-wrap the VU body in a private zone (regression). We wrap the
  // grouped tool panel emit + the standalone thinking emit back into a
  // vu-private-zone <details> by re-adding the class marker after the reply.
  chatSrc = CHAT.replace(
    'body += `<div class="md-content${isUser ? " user-content" : ""}">${mdHtml}</div>`;',
    'body += `<div class="md-content${isUser ? " user-content" : ""}">${mdHtml}</div>`;'
      + ' if (msg._isVirtualUser) body += `<details class="vu-private-zone"><summary>NC</summary></details>`;');
} else if (NC === 'nc_actions') {
  // NC-3: re-introduce the old VU carve-out so a VU (role=user) row LOSES its
  // Regen affordance again (the pre-2026-07-17 behaviour). Proves the
  // `_showRegen = isUser` gate (which now shows Regen on VU turns too) is
  // load-bearing. (Edit is unconditional edit-in-place, not gated by this.)
  chatSrc = CHAT.replace(
    'const _showRegen = isUser;',
    'const _showRegen = isUser && !msg._isVirtualUser;');
} else if (NC === 'nc_fold') {
  // NC-2: make _applyAutopilotRunFolds actually FOLD again (regression) — wrap
  // every stamped VU turn's parent range into a <details.autopilot-run-fold>.
  chatSrc = CHAT.replace(
    "inner.querySelectorAll('details.autopilot-run-fold').forEach(fold => {",
    "inner.querySelectorAll('.message[data-ap-run]').forEach(msgEl => {"
      + " if (msgEl.closest('.autopilot-run-fold')) return;"
      + " const d = document.createElement('details');"
      + " d.className = 'autopilot-run-fold';"
      + " d.setAttribute('data-ap-run-fold', msgEl.getAttribute('data-ap-run'));"
      + " msgEl.parentNode.insertBefore(d, msgEl); d.appendChild(msgEl); });"
      + " if (false) inner.querySelectorAll('details.autopilot-run-fold').forEach(fold => {");
}
const _applied = (NC === '') || (chatSrc !== CHAT);
check('nc_pattern_applied', _applied);

(0, eval)(fs.readFileSync(process.argv[3], 'utf8'));  // escape_html.js
(0, eval)(fs.readFileSync(process.argv[4], 'utf8'));  // safe_html.js
(0, eval)(fs.readFileSync(process.argv[7], 'utf8'));  // core/translation_model.js
(0, eval)(fs.readFileSync(process.argv[8], 'utf8'));  // ui/translation_indicator.js
(0, eval)(chatSrc);                                   // chat_render.js (real / neutered)

if (typeof renderMessage !== 'function') {
  console.log('FAIL fn_exposed renderMessage missing'); process.exit(0);
}
if (typeof _applyAutopilotRunFolds !== 'function') {
  console.log('FAIL fn_exposed _applyAutopilotRunFolds missing'); process.exit(0);
}
check('fn_exposed', true);

// A finished VU message: role=user + _isVirtualUser, with tool investigation
// (toolRounds), private reasoning (thinking) and the reply (content).
function mkVu() {
  return {
    role: 'user',
    _isVirtualUser: true,
    _msgId: 'vu1',
    _autopilotRunId: 'RX',
    content: 'Keep going, and verify the exporter path.',
    thinking: 'STANDALONE_VU_THINKING',
    toolRounds: [
      { toolCallId: 'tc1', toolName: 'read_files', status: 'done',
        toolContent: 'ok', llmRound: 0, roundNum: 1 },
    ],
  };
}

// ══ (a) renderMessage(VU) → flat agent bubble + reachable delete ══
{
  const html = renderMessage(mkVu(), 3);
  const frag = win.document.createElement('div');
  frag.innerHTML = html;

  // Reply body renders as a plain md-content (agent-identical).
  check('a_reply_md_content', !!frag.querySelector('.md-content'));
  check('a_reply_text_present', html.indexOf('Keep going, and verify') !== -1);
  // Grouped tool panel routed straight into the body (NOT a private zone).
  check('a_grouped_tool_panel', !!frag.querySelector('.ptool-panel'));
  // Standalone thinking block present in the body (agent-identical), lazy-load.
  check('a_thinking_block',
        html.indexOf('thinking-block" data-tofu-action="_toggleThinking') !== -1);

  // The provenance zones are GONE.
  check('a_no_private_zone', !frag.querySelector('.vu-private-zone'));
  check('a_no_sent_zone', !frag.querySelector('.vu-sent-zone'));
  check('a_no_investigation_header', !frag.querySelector('.vu-investigation-header'));
  check('a_no_handoff_header', !frag.querySelector('.vu-handoff-header'));

  // The ONLY who-is-speaking signals: Autopilot avatar (critic glyph) + label.
  const roleEl = frag.querySelector('.message-role');
  check('a_role_label_autopilot', roleEl && roleEl.textContent.trim() === 'Autopilot');
  const avatar = frag.querySelector('.message-avatar');
  check('a_avatar_is_critic_glyph',
        avatar && avatar.innerHTML.indexOf('data-avatar="critic"') !== -1);
  // Still tagged as a VU bubble for lane styling.
  check('a_vu_class', !!frag.querySelector('.message.vu-user-msg'));

  // ── The delete affordance is PRESENT and reachable (not folded away). ──
  const delBtn = frag.querySelector('.msg-delete-btn');
  check('a_delete_btn_present', !!delBtn);
  check('a_delete_btn_calls_deleteTurn',
        delBtn && (delBtn.getAttribute('data-tofu-action') || '')
          .indexOf('deleteTurn(_msgElIndex(this))') !== -1);
  // The action bar is a direct child of message-content (hover-reachable),
  // NOT inside any collapsed <details>.
  check('a_delete_not_in_details',
        delBtn && !delBtn.closest('details'));

  // ── (a3) ACTION-SET: a VU row gets the SHARED actions (Copy / Translate /
  //    Delete), Edit (edit-in-place — Save only, no Save & Resend), AND Regen
  //    (owner directive 2026-07-17: re-running the agent's reply to an
  //    autopilot step is a valid recovery affordance). ──
  check('a3_vu_has_copy', !!frag.querySelector('.copy-msg-btn'));
  check('a3_vu_has_translate', !!frag.querySelector('.msg-translate-btn'));
  check('a3_vu_has_delete', !!frag.querySelector('.msg-delete-btn'));
  // Edit has no dedicated class; detect by its onclick handler. Edit is now
  // available on every lane (edit-in-place), so the VU row DOES carry it.
  check('a3_vu_has_edit', html.indexOf('startEditMessage(_msgElIndex(this))') !== -1);
  check('a3_vu_has_regen', !!frag.querySelector('.msg-regen-btn'));
  // A VU is never the last assistant → no Continue; and Export is assistant-only.
  check('a3_vu_no_continue', !frag.querySelector('.msg-continue-btn'));
  check('a3_vu_no_export', !frag.querySelector('.msg-export-img-btn'));
}

// ══ (a4) a REAL human user turn STILL shows Edit + Regen (no over-narrowing) ══
{
  const humanUser = { role: 'user', _msgId: 'u1', content: 'hello there' };
  const html = renderMessage(humanUser, 4);
  const frag = win.document.createElement('div');
  frag.innerHTML = html;
  check('a4_human_has_edit', html.indexOf('startEditMessage(_msgElIndex(this))') !== -1);
  check('a4_human_has_regen', !!frag.querySelector('.msg-regen-btn'));
  check('a4_human_has_copy', !!frag.querySelector('.copy-msg-btn'));
  check('a4_human_has_delete', !!frag.querySelector('.msg-delete-btn'));
}

// ══ (a5) a role=user PEER-MESSAGE turn (NOT _isVirtualUser) is treated as
//    human — Edit/Regen present (guards the sibling's idle-drain peer turns) ══
{
  const peerUser = { role: 'user', _msgId: 'p1', content: 'note from peer',
                     _peerMessage: true, _fromConv: 'abcd1234' };
  const html = renderMessage(peerUser, 5);
  check('a5_peer_has_edit', html.indexOf('startEditMessage(_msgElIndex(this))') !== -1);
  check('a5_peer_has_regen', html.indexOf('regenerateFromUser(_msgElIndex(this))') !== -1);
}

// ══ (b) _applyAutopilotRunFolds no longer FOLDS a concluded run ══
function mkMsgEl(id, opts) {
  opts = opts || {};
  const el = win.document.createElement('div');
  el.className = 'message' + (opts.user ? ' user-msg' : '');
  el.id = 'msg-' + id;
  if (opts.run) el.setAttribute('data-ap-run', opts.run);
  el.textContent = opts.text || ('msg' + id);
  return el;
}
{
  const inner = win.document.getElementById('chatInner');
  inner.innerHTML = '';
  inner.appendChild(mkMsgEl(0, { user: true, text: 'objective' }));
  inner.appendChild(mkMsgEl(1, { run: 'RX', user: true, text: 'VU keep going' }));
  inner.appendChild(mkMsgEl(2, { text: 'worker reply' }));
  const conv = {
    id: 'c-fold', messages: [], activeTaskId: null,
    autopilotSummaries: { RX: { runId: 'RX', status: 'concluded', reason: 'task_done',
                                content: 'done', ts: 1 } },
  };
  _applyAutopilotRunFolds(inner, conv);
  // No fold is ever created — even for a fully concluded run.
  check('b_no_fold_created', !inner.querySelector('details.autopilot-run-fold'));
  // Every VU/worker turn stays a direct child of chatInner (hover-able).
  check('b_vu_turn_flat',
        win.document.getElementById('msg-1').parentNode === inner);
  check('b_worker_turn_flat',
        win.document.getElementById('msg-2').parentNode === inner);
}

// ══ (b2) _applyAutopilotRunFolds UNWRAPS a pre-existing stale fold ══
{
  const inner = win.document.getElementById('chatInner');
  inner.innerHTML = '';
  const fold = win.document.createElement('details');
  fold.className = 'autopilot-run-fold';
  fold.setAttribute('data-ap-run-fold', 'RY');
  const summary = win.document.createElement('summary');
  summary.className = 'autopilot-run-fold-summary';
  summary.textContent = 'Autopilot run';
  fold.appendChild(summary);
  const vu = mkMsgEl(9, { run: 'RY', user: true, text: 'buried VU turn' });
  fold.appendChild(vu);
  inner.appendChild(fold);

  _applyAutopilotRunFolds(inner, { id: 'c-unwrap', messages: [], autopilotSummaries: {} });

  check('b2_fold_unwrapped', !inner.querySelector('details.autopilot-run-fold'));
  // The formerly-buried VU turn is hoisted back to a flat sibling.
  check('b2_turn_hoisted',
        win.document.getElementById('msg-9')
        && win.document.getElementById('msg-9').parentNode === inner);
}

console.log(out.join('\n'));
process.exit(0);
"""


def _run(nc: str = '') -> str:
    harness = os.path.join(HERE, f'_autopilot_flat_harness_{nc or "main"}.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             CHAT_RENDER,   # argv[2]
             ESCAPE_HTML,   # argv[3]
             SAFE_HTML,     # argv[4]
             ROOT,          # argv[5]
             nc,            # argv[6]
             TRANSLATION_MODEL,      # argv[7]
             TRANSLATION_INDICATOR,  # argv[8]
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
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_autopilot_flat_render_and_delete():
    # Sanity: the shipped source must NOT contain the removed provenance zones.
    chat_src = open(CHAT_RENDER, encoding='utf-8').read()
    assert 'vu-private-zone' not in chat_src.replace(
        'mirrors the vu-private-zone framing', ''), \
        'vu-private-zone still rendered in chat_render.js — flatten incomplete'
    assert 'vu-sent-zone' not in chat_src, 'vu-sent-zone still present — flatten incomplete'
    assert 'vu-investigation-header' not in chat_src, \
        'vu-investigation-header still present — flatten incomplete'

    # The shipped source must carry the broadened Regen gate (Regen on every
    # user-lane turn, incl. autopilot VU).
    assert 'const _showRegen = isUser;' in chat_src, \
        'Broadened Regen gate (_showRegen = isUser) missing from chat_render.js'

    output = _run('')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'autopilot flat-render failures:\n' + output
    assert output.count('PASS') >= 31, f'expected >=31 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_nc_private_zone_regression_is_caught():
    """NC-1: re-adding a private zone must break the 'no private zone' check."""
    output = _run('nc_private')
    assert 'PASS nc_pattern_applied' in output, f'NC mutation did not apply:\n{output}'
    assert 'FAIL a_no_private_zone' in output, (
        'Re-introducing .vu-private-zone did NOT fail the assertion — '
        f'the check is not load-bearing:\n{output}')


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_nc_fold_regression_is_caught():
    """NC-2: making _applyAutopilotRunFolds fold again must break the flat checks."""
    output = _run('nc_fold')
    assert 'PASS nc_pattern_applied' in output, f'NC mutation did not apply:\n{output}'
    assert ('FAIL b_no_fold_created' in output or 'FAIL b_vu_turn_flat' in output), (
        'Re-introducing the fold did NOT fail the flat-transcript assertions — '
        f'they are not load-bearing:\n{output}')


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_nc_action_gate_regression_is_caught():
    """NC-3: re-introducing the old VU carve-out must strip the VU's Regen
    button, failing the 'VU has Regen' assertion."""
    output = _run('nc_actions')
    assert 'PASS nc_pattern_applied' in output, f'NC mutation did not apply:\n{output}'
    assert 'FAIL a3_vu_has_regen' in output, (
        'Re-introducing the VU carve-out did NOT fail the VU Regen assertion — '
        f'the broadened gate is not load-bearing:\n{output}')


if __name__ == '__main__':
    if not _node_deps_available():
        print('SKIP — node + jsdom not available')
    else:
        test_autopilot_flat_render_and_delete()
        test_nc_private_zone_regression_is_caught()
        test_nc_fold_regression_is_caught()
        test_nc_action_gate_regression_is_caught()
        print('PASS test_frontend_autopilot_flat_render')
