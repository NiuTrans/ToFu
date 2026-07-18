"""tests/test_frontend_pending_queued_render.py — the LAST MILE of cross-device
send visibility: the message reaches the DOM AND its state reaches the user's eyes.

WHY
---
The backend now lands a cross-device queued user message in the transcript as a
display-only `_pendingQueued:true` row and pushes a real rev, so the other
device's `_verifyActiveConvFromServer` refetches it. But `_pendingQueued` was
zero-referenced in the frontend — the row would render as an indistinguishable
sent bubble (no "queued" state), and nothing proved the flag-flip (dispatch
reconcile clears it) transitions cleanly. And the active-conv verify still taxed
EVERY cross-device frame with the 1s self-write debounce, so "instant" was "1s
later".

This suite loads the REAL shipped chat_render.js + cross_tab_sync.js under node
and asserts:

  A. renderMessage(_pendingQueued user row) →
     • carries the `pending-queued` class (muted bubble), AND
     • emits the `queued-indicator` chip (SVG clock, NOT emoji — CLAUDE.md §3.4).
  B. _msgFingerprint folds `_pendingQueued`, so the SAME row with the flag
     cleared has a DIFFERENT fingerprint → the surgical renderChat diff repaints
     it → clean transition to a normal sent bubble (no whole-conv rebuild, and
     the queued chip is gone). Also: a normal user row has neither the class nor
     the chip (no false positive).
  C. cross_tab_sync: a cross-device conv_changed frame schedules the active
     verify at the SHORT delay (~60ms), NOT a full second. Local self-writes
     never reach this path — the self-echo guard (fresh `_localWriteAt` →
     return) is their authoritative protection and returns earlier — so there
     is exactly ONE verify-delay regime and it must be fast. Proven by capturing
     setTimeout's delay argument, plus asserting a fresh local write is skipped
     by the outer guard (no verify scheduled at all).

NEUTER controls (on a MUTATED source copy; shipped files never modified):
  • strip the `_pendingQueued ? ... : ''` class/indicator wiring → the queued
    row renders identical to a normal sent bubble (proves A is load-bearing).
  • strip the `_pendingQueued ? "PQ"` fingerprint fold → the flag-flip produces
    the SAME fingerprint → the reconcile would NOT repaint (proves B).
  • bump the active-verify delay constant to 1000 → the cross-device frame no
    longer verifies fast (proves the fast delay is load-bearing for C).

Skips cleanly when node isn't installed.
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
TOOL_ROUNDS = os.path.join(JS_DIR, 'ui', 'tool_rounds.js')
CHAT_RENDER = os.path.join(JS_DIR, 'ui', 'chat_render.js')
XTAB = os.path.join(JS_DIR, 'core', 'cross_tab_sync.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


# ════════════════════════════════════════════════════════════════════
#  Harness 1 — renderMessage + _msgFingerprint (parts A & B)
# ════════════════════════════════════════════════════════════════════

_RENDER_HARNESS = r"""
const fs = require('fs');
global.window = global;
global.document = {
  addEventListener: function () {}, removeEventListener: function () {},
  querySelector: () => null, querySelectorAll: () => [], getElementById: () => null,
  createElement: () => ({ style: {}, classList: { add(){}, remove(){}, toggle(){} }, setAttribute(){}, appendChild(){} }),
};
const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

global.config = { segmentTimeline: true };
global.t = (k) => k;   // identity → _tOr falls back to the English default arg
global._fmtAbsoluteDateTime = () => '';
global.stripNoTranslateTags = (s) => (s == null ? '' : String(s));
global.renderMarkdown = (s) => '<md>' + String(s == null ? '' : s) + '</md>';
global.renderMcpLoginHintHtml = () => '';
global.renderTurnProvenanceHtml = () => '';
global.renderFileChangesBar = () => '';
global.renderErrorEnvelope = () => '';
global.renderBranchZone = () => '';
global.renderTurnCtxNote = () => '';
global.renderPreferenceLearnedHtml = () => '';
global.getActiveConv = () => null;
global.activeStreams = new Set();
global._USER_AVATAR_SVG = '<img data-avatar="onigiri">';
global._buildSwarmPanelHTML = () => '<swarm/>';
global._buildSwarmInboxChipsHTML = () => '';
global._isRoundSwarm = () => false;
global._TOOL_DISPLAY = {};
global._toolPanelHeaderLabel = () => 'HDR';
global.getToolRoundsFromMsg = (m) => (m && m.toolRounds) || [];
global.renderFinishInfo = () => '';
global._TOFU_WORKER_SVG = '<img data-avatar="worker">';
global._TOFU_PLANNER_SVG = '<img data-avatar="planner">';
global._TOFU_CRITIC_SVG = '<img data-avatar="critic">';
global.calcCostCny = () => 0;

function loadAll(chatSrc) {
  (0, eval)(fs.readFileSync(process.argv[2], 'utf8'));  // escape_html.js
  (0, eval)(fs.readFileSync(process.argv[3], 'utf8'));  // safe_html.js
  (0, eval)(fs.readFileSync(process.argv[4], 'utf8'));  // tool_rounds.js
  (0, eval)(fs.readFileSync(process.argv[2].replace('escape_html.js', 'translation_model.js'), 'utf8'));
  (0, eval)(fs.readFileSync(process.argv[2].replace('core/escape_html.js', 'ui/translation_indicator.js'), 'utf8'));
  (0, eval)(chatSrc);
}

function mkUser(extra) {
  return Object.assign({ role: 'user', content: 'from my phone', timestamp: 3 }, extra || {});
}

const CHAT = fs.readFileSync(process.argv[5], 'utf8');
loadAll(CHAT);
if (typeof renderMessage !== 'function' || typeof _msgFingerprint !== 'function') {
  console.log('FAIL fn_exposed renderMessage/_msgFingerprint missing'); process.exit(0);
}
check('fn_exposed', true);

// ══ A. Pending-queued user row → muted class + queued chip (SVG, no emoji) ══
{
  const html = renderMessage(mkUser({ _pendingQueued: true }), 5);
  check('A_has_pending_queued_class', /class="message[^"]*\bpending-queued\b/.test(html));
  check('A_has_queued_indicator', html.indexOf('queued-indicator') !== -1);
  check('A_indicator_is_svg', html.indexOf('<svg') !== -1 && html.indexOf('queued-indicator') !== -1);
  // CLAUDE.md §3.4 — no emoji glyphs used as the icon.
  check('A_no_emoji_icon', !/[\u23F0\u231B\u{1F551}\u{1F4E4}\u{1F4EC}]/u.test(html));
  check('A_body_present', html.indexOf('from my phone') !== -1);
}

// ══ B. Normal user row (no flag) → neither class nor chip (no false positive) ══
{
  const html = renderMessage(mkUser(), 5);
  check('B_normal_no_pending_class', !/\bpending-queued\b/.test(html));
  check('B_normal_no_queued_indicator', html.indexOf('queued-indicator') === -1);
}

// ══ B2. Fingerprint folds _pendingQueued → flag-flip repaints (clean transition) ══
{
  const fpPending = _msgFingerprint(mkUser({ _pendingQueued: true }));
  const fpSettled = _msgFingerprint(mkUser());   // dispatch reconcile cleared it
  check('B2_fingerprint_differs_on_flag', fpPending !== fpSettled);
  check('B2_pending_fp_tags_PQ', fpPending.indexOf('PQ') !== -1);
  check('B2_settled_fp_no_PQ', fpSettled.indexOf('PQ') === -1);
}

// ══ NEUTER 1: strip the pending-queued class/indicator wiring in renderMessage ══
{
  const NEEDLE = "const _pendingQueued = isUser && !!msg._pendingQueued;";
  const neutered = CHAT.split(NEEDLE).join("const _pendingQueued = false;  /* NEUTERED */");
  check('N1_patch_applied', neutered !== CHAT);
  loadAll(neutered);
  const html = renderMessage(mkUser({ _pendingQueued: true }), 5);
  check('N1_queued_row_indistinguishable', !/\bpending-queued\b/.test(html) && html.indexOf('queued-indicator') === -1);
  loadAll(CHAT);  // restore
}

// ══ NEUTER 2: strip the _pendingQueued fingerprint fold → flag-flip = same fp ══
{
  const NEEDLE = '(msg._pendingQueued ? "PQ" : "") + ":" +';
  const neutered = CHAT.split(NEEDLE).join('"" +');
  check('N2_patch_applied', neutered !== CHAT);
  loadAll(neutered);
  const fpP = _msgFingerprint(mkUser({ _pendingQueued: true }));
  const fpS = _msgFingerprint(mkUser());
  check('N2_fingerprint_same_would_not_repaint', fpP === fpS);
  loadAll(CHAT);
}

console.log(out.join('\n'));
process.exit(0);
"""


# ════════════════════════════════════════════════════════════════════
#  Harness 2 — cross_tab_sync active-verify delay split (part C)
# ════════════════════════════════════════════════════════════════════

_XTAB_HARNESS = r"""
const fs = require('fs');
global.window = global;
const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

let scheduledDelays = [];   // every setTimeout(fn, ms) delay captured
global._syncChannel = null;
global.TAB_ID = 'tab-test';
global.debugLog = () => {};
global.conversations = [];
global.activeStreams = new Map();
global._editingMsgIdx = null;
global.activeConvId = null;
global.addEventListener = () => {};
global.document = { visibilityState: 'visible', addEventListener: () => {} };
global.setTimeout = (fn, ms) => { scheduledDelays.push(ms); return scheduledDelays.length; };
global.clearTimeout = () => {};
global.setInterval = () => 0;
global.clearInterval = () => {};
global.ConvCache = { remove: () => {}, put: () => {}, get: async () => null };
global.renderChat = () => {};
global.saveConversations = () => {};
global.loadConversationsFromServer = async () => {};
global.loadConversationMessages = async () => {};
global.pushIsConnected = () => true;
global.pushSubscribe = () => {};
global.Api = { conversations: { get: async () => null } };

const SRC = fs.readFileSync(process.argv[2], 'utf8');
function loadModule(src) { (0, eval)(src); }
function reset() {
  scheduledDelays = [];
  global.conversations = []; global.activeStreams = new Map();
  global._editingMsgIdx = null; global.activeConvId = null; global._currentUserId = undefined;
}
loadModule(SRC);
check('fn_exposed', typeof _onConvNotifyPush === 'function');

// The active-verify delay is the LAST setTimeout scheduled by the handler for
// an active-conv newer-rev frame (list-refresh uses its own timer path).
function lastVerifyDelay() { return scheduledDelays[scheduledDelays.length - 1]; }

// ══ C1. CROSS-DEVICE frame (no local write) → SHORT delay, near-immediate ══
{
  reset();
  conversations = [{ id: 'c1', _serverRev: 5, messages: [{ role: 'user', content: 'q' }] }];
  activeConvId = 'c1';
  _onConvNotifyPush({ type: 'conv_changed', convId: 'c1', rev: 6, userId: 1 });
  const d = lastVerifyDelay();
  check('C1_scheduled', typeof d === 'number');
  check('C1_verify_is_fast', d <= 200);   // near-immediate, not a 1s debounce
}

// ══ C2. FRESH local self-write → skipped ENTIRELY by the outer self-echo
//        guard (no verify scheduled). This is why there is exactly ONE
//        verify-delay regime: the local-write case never reaches the delay. ══
{
  reset();
  conversations = [{ id: 'c1', _serverRev: 5, messages: [{ role: 'user', content: 'q' }],
                     _localWriteAt: Date.now() - 100 }];  // fresh, within self-echo window
  activeConvId = 'c1';
  _onConvNotifyPush({ type: 'conv_changed', convId: 'c1', rev: 6, userId: 1 });
  check('C2_fresh_localwrite_skipped_by_outer_guard', scheduledDelays.length === 0);
}

// ══ C3. Single active-verify delay constant exists and is fast (<= 200ms).
//        No second "full-debounce" constant should linger — a dead 1000ms
//        branch was removed (the self-echo guard already covers local writes). ══
{
  const m = SRC.match(/_CONV_ACTIVE_VERIFY_DELAY_MS\s*=\s*(\d+)/);
  check('C3_delay_const_present', !!m);
  check('C3_delay_is_fast', m && Number(m[1]) <= 200);
  check('C3_no_dead_xdev_const', SRC.indexOf('_CONV_ACTIVE_VERIFY_DELAY_XDEV_MS') === -1);
  // The unreachable _localWriteFresh ternary must be gone (dead-branch removal).
  check('C3_no_dead_localWriteFresh_branch', SRC.indexOf('_localWriteFresh') === -1);
}

// ══ NEUTER 3: bump the active-verify delay to 1000 → cross-device no longer
//        fast (proves the fast delay itself is load-bearing for C1). ══
{
  const neutered = SRC.replace(/_CONV_ACTIVE_VERIFY_DELAY_MS\s*=\s*\d+/,
                               '_CONV_ACTIVE_VERIFY_DELAY_MS = 1000');
  check('N3_patch_applied', neutered !== SRC);
  loadModule(neutered);
  reset();
  conversations = [{ id: 'c1', _serverRev: 5, messages: [{ role: 'user', content: 'q' }] }];
  activeConvId = 'c1';
  _onConvNotifyPush({ type: 'conv_changed', convId: 'c1', rev: 6, userId: 1 });
  const d = lastVerifyDelay();
  check('N3_verify_no_longer_fast', d === 1000);
  loadModule(SRC);
}

console.log(out.join('\n'));
process.exit(0);
"""


def _run(harness_src: str, args: list[str], name: str, min_pass: int):
    harness = os.path.join(HERE, f'_{name}.js')
    with open(harness, 'w') as f:
        f.write(harness_src)
    try:
        proc = subprocess.run(['node', harness, *args],
                              capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, f'{name} failures:\n{output}'
    assert output.count('PASS') >= min_pass, f'expected >={min_pass} PASS, got:\n{output}'
    return output


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_pending_queued_render():
    # Sanity: the wiring must be present in the shipped source (stale-test guard).
    chat_src = open(CHAT_RENDER, encoding='utf-8').read()
    assert '_pendingQueued = isUser && !!msg._pendingQueued' in chat_src, \
        'pending-queued render wiring missing from chat_render.js — test stale'
    assert '(msg._pendingQueued ? "PQ"' in chat_src, \
        'pending-queued fingerprint fold missing — test stale'
    _run(_RENDER_HARNESS, [ESCAPE_HTML, SAFE_HTML, TOOL_ROUNDS, CHAT_RENDER],
         'pending_queued_render_harness', 12)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_cross_device_verify_delay_split():
    xtab_src = open(XTAB, encoding='utf-8').read()
    assert '_CONV_ACTIVE_VERIFY_DELAY_MS' in xtab_src, \
        'active-verify delay constant missing from cross_tab_sync.js — test stale'
    assert '_CONV_ACTIVE_VERIFY_DELAY_XDEV_MS' not in xtab_src, \
        'dead _XDEV constant still present — dead-branch removal incomplete'
    _run(_XTAB_HARNESS, [XTAB], 'pending_queued_xtab_harness', 7)


if __name__ == '__main__':
    if not _node_available():
        print('SKIP — node not available')
    else:
        test_pending_queued_render()
        test_cross_device_verify_delay_split()
        print('PASS both')
