"""Expired human-guidance presentation contracts.

A leftover ``awaiting_human`` round on a SETTLED turn (e.g. the task died
with a server restart — the blocked handler never finalized the round) is
unanswerable forever: the pending-request map in lib/tasks_pkg/human_guidance.py
is process-local. Three pins cover the fix:

1. ``renderToolBlockHtml`` (conversation_turn_store.js) stamps ``_turnSettled``
   on decorated rounds from the turn's own status — the definitional signal.
2. ``_renderUnifiedToolLine`` (tool_rounds.js) renders such a round as a
   read-only expired card: full question + static options, NO submit controls
   (no data-tofu-action, no hg-submit-btn), expired badge.
3. ``_submitHumanGuidanceResponse`` (project.js) maps a backend 404 (pending
   request gone) to the expired toast + an authoritative re-render, instead of
   claiming a network error; genuine transport failures keep the old message.

All skip cleanly without node.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_section_path


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
TOOL_ROUNDS = Path(runtime_section_path('ui/tool_rounds.js'))
TURN_STORE = Path(runtime_section_path('main/conversation_turn_store.js'))
PROJECT = Path(runtime_section_path('project.js'))


def _node(harness: str, *paths: str) -> dict:
    if not shutil.which('node'):
        pytest.skip('node is required')
    result = subprocess.run(
        ['node', '-e', harness, *paths], cwd=ROOT,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_settled_turn_stamps_rounds_turn_settled():
    """decorateRound must mark rounds of terminal turns so the renderer can
    never offer an answer UI for a question nobody is listening to."""
    harness = r"""
const fs = require('fs');
global.window = globalThis;
global.addEventListener = () => {};
global.conversationSyncApi = {};
global.conversations = [];
global.createBranchComposerSession = () => ({ current() { return null; }, close() {} });
global.createHumanGuidancePresentationStore = () => ({
  read() { return null; }, patch() { return null; },
  decorate(_c, round) { return round; }, clearConversation() {},
});
let renderDeps = null;
global.createClassicConversationRenderers = (deps) => { renderDeps = deps; return {}; };
global.createPlanDecisionBar = () => ({ render() {}, activateConversation() {} });
global.createConversationSurfaceController = () => ({});
global.createTransientTurnOverlay = () => ({});
global.activeConversationAttemptIds = () => [];
global.activeMainConversationAttemptId = () => null;
global.orderedConversationTurns = () => [];
global.latestConversationTurn = () => null;
global.conversationHasActor = () => false;
global.createConversationTurnRuntime = () => ({
  emptyState() { return {}; }, reducer(s) { return s; },
  readRuntimeState() { return null; },
});
global.escapeHtml = (s) => String(s == null ? '' : s);
global.t = (k) => k;

const captured = [];
global._renderToolSlot = (round) => { captured.push(round); return '<x/>'; };

eval(fs.readFileSync(process.argv[1], 'utf8'));
if (!renderDeps) throw new Error('classic renderers deps not captured');

const hgRound = {
  roundNum: 34, toolCallId: 'call-hg', toolName: 'ask_human',
  status: 'awaiting_human', guidanceId: 'hg_x', guidanceQuestion: 'q?',
};
const block = { kind: 'tool', round: hgRound, toolCallId: 'call-hg' };
const turnOf = (status) => ({
  turnId: 'turn-1', status, taskId: 'task-1',
  source: { conversationId: 'conv-a', projection: { toolRounds: [hgRound] } },
});

renderDeps.renderToolBlockHtml(block, turnOf('interrupted'));
renderDeps.renderToolBlockHtml(block, turnOf('running'));
renderDeps.renderToolBlockHtml(block, turnOf('pending'));
console.log(JSON.stringify(captured.map((r) => r._turnSettled)));
process.exit(0);
"""
    assert _node(harness, str(TURN_STORE)) == [True, False, False]


def test_expired_awaiting_human_round_renders_read_only():
    harness = r"""
const fs = require('fs');
global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
global.t = (k) => k;
global.renderMarkdown = (s) => s;
global._shortUrl = (u) => u;
global.formatNumber = (n) => String(n);
global.window = { location: { href: 'http://localhost/' }, addEventListener() {}, removeEventListener() {} };
global.document = { addEventListener() {}, removeEventListener() {},
  createElement: () => ({ style: {}, setAttribute() {}, appendChild() {} }) };
eval(fs.readFileSync(process.argv[1], 'utf8'));

const base = {
  roundNum: 34, toolCallId: 'call-hg', toolName: 'ask_human',
  status: 'awaiting_human', guidanceId: 'hg_x',
  guidanceQuestion: 'Scope?', guidanceType: 'choice',
  guidanceOptions: [{ label: 'A' }, { label: 'B', description: 'd' }],
  results: [{}],
};
const settled = _renderUnifiedToolLine({ ...base, _turnSettled: true }, false);
const live = _renderUnifiedToolLine({ ...base }, false);
console.log(JSON.stringify({
  expiredHasClass: settled.includes('hg-expired'),
  expiredBadge: settled.includes('project.hgExpired'),
  expiredHasSubmitAction: settled.includes('submitHumanGuidance'),
  expiredHasSubmitBtn: settled.includes('hg-submit-btn'),
  expiredHasAction: settled.includes('data-tofu-action'),
  expiredStaticOptions: (settled.match(/hg-option-static/g) || []).length,
  liveHasAction: live.includes('submitHumanGuidanceChoice'),
  liveBadge: live.includes('project.hgWaitingReply'),
  liveExpiredAbsent: !live.includes('hg-expired'),
}));
process.exit(0);
"""
    assert _node(harness, str(TOOL_ROUNDS)) == {
        'expiredHasClass': True,
        'expiredBadge': True,
        'expiredHasSubmitAction': False,
        'expiredHasSubmitBtn': False,
        'expiredHasAction': False,
        'expiredStaticOptions': 2,
        'liveHasAction': True,
        'liveBadge': True,
        'liveExpiredAbsent': True,
    }


def test_submit_404_toasts_expired_and_repaints():
    harness = r"""
const fs = require('fs');
global.window = globalThis;
global.runtimeScope = globalThis;
global.escapeHtml = (s) => String(s == null ? '' : s);
global.t = (k) => k;
global.debugLog = () => {};
global.getActiveConv = () => ({ id: 'conv-a' });
global.createProjectBrowseCoordinator = () => ({
  begin() { return { token: 0, generation: 0, signal: { aborted: false } }; },
  cancel() {}, isCurrent() { return false; },
});
const toasts = [];
global.showToast = (...args) => toasts.push(args.map(String));
const repaints = [];
global.requestAuthoritativeConversationRender = undefined;
globalThis.requestAuthoritativeConversationRender = undefined;
let mode = '404';
global.Api = { chat: { humanResponse: async () => {
  if (mode === '404') { const e = new Error('not found'); e.status = 404; throw e; }
  if (mode === 'net') { throw new TypeError('fetch failed'); }
  return { ok: true };
} } };
global.document = {
  getElementById: () => null, querySelector: () => null, querySelectorAll: () => [],
  addEventListener() {}, createElement: () => ({ style: {}, setAttribute() {}, appendChild() {} }),
};
eval(fs.readFileSync(process.argv[1], 'utf8'));
// The section consults runtimeScope.requestAuthoritativeConversationRender.
globalThis.runtimeScope.requestAuthoritativeConversationRender =
  (convId, opts) => repaints.push([convId, !!(opts && opts.force)]);

(async () => {
  const expired = await _submitHumanGuidanceResponse('hg_x', 'answer');
  mode = 'net';
  const network = await _submitHumanGuidanceResponse('hg_x', 'answer');
  mode = 'ok';
  const fine = await _submitHumanGuidanceResponse('hg_x', 'answer');
  console.log(JSON.stringify({ expired, network, fine, toasts, repaints }));
  process.exit(0);
})().catch((e) => { console.error(e); process.exitCode = 1; });
"""
    result = _node(harness, str(PROJECT))
    assert result['expired'] is False
    assert result['network'] is False
    assert result['fine'] is True
    assert result['toasts'][0][:1] == ['project.hgExpiredToast'], result['toasts']
    assert result['toasts'][1][:1] == ['project.hgNetworkError'], result['toasts']
    assert len(result['toasts']) == 2
    assert result['repaints'] == [['conv-a', True]], result['repaints']
