"""Regression: terminal turn-metadata fields land through ONE shared reducer
on every keep-local merge path.

WHY (bug class, conv mrzutwddkeuw0n, 2026-07-25)
-------------------------------------------------
The finish bar's cost popover showed only aggregate rows (Input / Cache
read / Output / Cache 节省 / Total) — NO per-round breakdown, NO Task ID
row, NO key-tail route tag — while the DB / done event / serve paths ALL
carried ``apiRounds`` + ``_taskId``. Root cause: every keep-local merge
site hand-enumerated the fields it copied from the server's settled
message, and every hand-written list predated the terminal cost-accounting
fields. THREE sites produced the identical visible symptom:

  1. core/conversations.js ``loadConversationMessages`` MERGE_ACTIVE_TASK
     (fixed in af139ba9 — the original report);
  2. core/cross_tab_sync.js ``_verifyActiveConvFromServer`` Case 2 — the
     merge was gated on "content grew", which a settled turn never does;
  3. main/main_init_tasks.js ``initActiveTasks`` Case B — the poll-done
     merge listed finishReason/usage/preset/fallback*/modifiedFiles but
     not apiRounds/_taskId/cost.

FIX (this pass): ONE shared reducer
``core/conv_reducers.js::_mergeTerminalTurnFields(lm, sm)`` owns the field
list (apiRounds upgrade-if-longer; finishReason/usage/model/_taskId/cost/
provider_id/preset/thinkingDepth/modified*/fallback* fill-if-missing) and
all three sites call it, so the list can never drift a fourth time.
(branch_stream.js deliberately NOT touched — the branch panel never
renders renderFinishInfo, so it has no visible surface today.)

HARNESS
-------
  A. bare-node: REAL loadConversationMessages (MERGE_ACTIVE_TASK) — evals
     the REAL conv_reducers.js + conversations.js in bundle order.
     NEUTER: strip the helper CALL → fields never land → red.
  B. bare-node: REAL initActiveTasks Case B (refresh-on-finished-task) —
     poll payload td → tail am. NEUTER: strip the helper call → red.
  C. bare-node: REAL _verifyActiveConvFromServer Case 2 — equal message
     count, equal content length (growth gate never fires) — terminal
     fields must land anyway, changed=true, repaint fires.
     NEUTER: strip the helper call → red.
  D. jsdom: REAL _msgFingerprint moves when apiRounds/_taskId/usage land
     (the repaint trigger; unchanged from af139ba9).
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._jsdom import run_harness, JS_DIR

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))


def _node_available() -> bool:
    return bool(shutil.which('node'))


def _run_harness(name: str, body: str, *js_paths: str) -> str:
    harness = os.path.join(HERE, f'_terminal_fields_{name}.js')
    with open(harness, 'w') as f:
        f.write(body)
    try:
        proc = subprocess.run(
            ['node', harness, *js_paths],
            capture_output=True, text=True, timeout=90,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


# ═══════════════════════════════════════════════════════════════════════
# Harness A — REAL loadConversationMessages, MERGE_ACTIVE_TASK branch
# ═══════════════════════════════════════════════════════════════════════
_HARNESS_A = r"""
const fs = require('fs');
global.window = global;
global.activeConvId = 'c1';
global.activeStreams = new Map();
global._editingMsgIdx = null;
global.debugLog = () => {};
global.config = {};
global.renderConversationList = () => {};
global.showStreamingUIForConv = () => {};
global._restoreConvToolState = () => {};
// core/conv_apply_settings.js — a real bundle sibling of conversations.js.
// MERGE_ACTIVE_TASK now applies the server's settings blob (model /
// thinkingDepth / tool state) so a pinned conversation cannot keep painting a
// stale model. This harness exercises only the terminal-FIELD merge, so the
// settings reader is stubbed.
global._applySettingsToConv = () => {};
global._convSorter = (a, b) => (b.updatedAt || 0) - (a.updatedAt || 0);
global.apiUrl = (p) => p;
global.AbortSignal = { timeout: () => undefined };
// External helpers living in OTHER bundle files (not eval'd here).
global.convHasPendingSync = () => false;             // core/pending_sync.js
global._serverHasTranslationLocalLacks = () => false; // core/conv_persist_helpers.js
global.ConvCache = {
  isAvailable: () => true,
  get: async () => null,          // cache MISS → Phase-2 runs with cacheHit=false
  getMeta: async () => null,
  getAllMeta: async () => [],
  put: async () => {},
  remove: async () => {},
};
global.ConvView = { replaceAll: () => {} };

// The server conv body: same length as local (no append, no orphan branch).
const _FULL_ASSISTANT = {
  role: 'assistant', _msgId: 'm1', content: 'short',
  thinking: 't', toolRounds: [{ roundNum: 0, toolName: 'grep_search' }],
  model: 'kimi-k3', provider_id: 'sankuai', preset: 'p', thinkingDepth: 'high',
  finishReason: 'stop',
  usage: { prompt_tokens: 5854882, completion_tokens: 34741,
           cache_read_tokens: 5669120, cache_write_tokens: 0 },
  apiRounds: Array.from({ length: 39 }, (_, i) => ({
    round: i + 1, model: 'kimi-k3', tag: 'R' + (i + 1),
    usage: { prompt_tokens: 150000, completion_tokens: 800 },
  })),
  _taskId: 'task-settled-1',
  cost: { costCny: 18.5138, costUsd: 2.5 },
  modifiedFiles: 2, modifiedFileList: [{ path: 'a.js' }],
  fallbackModel: 'aws.claude-opus-4.8', fallbackFrom: 'kimi-k3',
  fallbackReason: 'ratelimit: 429', fallbackKind: 'ratelimit',
};
let SERVER_BODY = null;
global.Api = {
  conversations: {
    getResponse: async () => ({
      status: 200, ok: true,
      headers: { get: () => null },
      json: async () => SERVER_BODY,
    }),
    get: async () => SERVER_BODY,
  },
};

global.conversations = [];
eval(fs.readFileSync(process.argv[2], 'utf8'));  // REAL core/conv_reducers.js
eval(fs.readFileSync(process.argv[3], 'utf8'));  // REAL core/conversations.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

function seedConv(localAssistant) {
  conversations.length = 0;
  conversations.push({
    id: 'c1', title: 'c1', _needsLoad: true,
    messages: [
      { role: 'user', _msgId: 'm0', content: 'q', timestamp: 1 },
      localAssistant,
    ],
    createdAt: 1, updatedAt: 1,
    activeTaskId: 'task-live-now',   // ← the MERGE_ACTIVE_TASK gate
  });
}

(async () => {
  // ══ A. Half-upgraded local (finishReason + usage + cost; NO apiRounds /
  //    _taskId — the exact screenshot state) + full server copy. ══
  {
    const localAssistant = {
      role: 'assistant', _msgId: 'm1',
      content: 'locally-longer-content-than-server',  // keep: upgrade is longer-wins
      thinking: 't', toolRounds: [{ roundNum: 0, toolName: 'grep_search' }],
      model: 'kimi-k3', finishReason: 'stop',
      usage: { prompt_tokens: 5854882, completion_tokens: 34741,
               cache_read_tokens: 5669120, cache_write_tokens: 0 },
      cost: { costCny: 18.5138, costUsd: 2.5 },   // lazy write-back already landed
      // apiRounds / _taskId / provider_id / preset / thinkingDepth /
      // modified* / fallback* — ALL missing (the degraded cache shape).
    };
    seedConv(localAssistant);
    SERVER_BODY = {
      id: 'c1', title: 'c1', rev: 7,
      updatedAt: 2, messages: [
        { role: 'user', _msgId: 'm0', content: 'q', timestamp: 1 },
        JSON.parse(JSON.stringify(_FULL_ASSISTANT)),
      ],
    };
    await loadConversationMessages('c1');
    const lm = conversations[0].messages[1];
    check('A_branch_kept_local_content', lm.content === 'locally-longer-content-than-server');
    check('A_apiRounds_merged', Array.isArray(lm.apiRounds) && lm.apiRounds.length === 39);
    check('A_apiRounds_is_server_copy', lm.apiRounds
      && lm.apiRounds[38] && lm.apiRounds[38].round === 39);
    check('A_taskId_merged', lm._taskId === 'task-settled-1');
    check('A_cost_preserved', !!(lm.cost && lm.cost.costCny === 18.5138));
    check('A_provider_merged', lm.provider_id === 'sankuai');
    check('A_preset_merged', lm.preset === 'p');
    check('A_thinkingDepth_merged', lm.thinkingDepth === 'high');
    check('A_modifiedFiles_merged', lm.modifiedFiles === 2);
    check('A_modifiedFileList_merged',
      Array.isArray(lm.modifiedFileList) && lm.modifiedFileList.length === 1);
    check('A_fallbackModel_merged', lm.fallbackModel === 'aws.claude-opus-4.8');
    check('A_fallbackFrom_merged', lm.fallbackFrom === 'kimi-k3');
    check('A_fallbackReason_merged', lm.fallbackReason === 'ratelimit: 429');
    check('A_fallbackKind_merged', lm.fallbackKind === 'ratelimit');
    // Pre-existing three-field behaviour stays intact (control).
    check('A_control_finishReason', lm.finishReason === 'stop');
    check('A_control_usage', !!(lm.usage && lm.usage.prompt_tokens === 5854882));
    check('A_control_model', lm.model === 'kimi-k3');
  }

  // ══ B. apiRounds NEVER downgrades: local 39 rounds, server 10 → keep 39. ══
  {
    const localAssistant = {
      role: 'assistant', _msgId: 'm1', content: 'x', thinking: 't',
      toolRounds: [{ roundNum: 0 }],
      apiRounds: Array.from({ length: 39 }, (_, i) => ({ round: i + 1 })),
      finishReason: 'stop', usage: { prompt_tokens: 10 },
    };
    seedConv(localAssistant);
    const serverAssistant = JSON.parse(JSON.stringify(_FULL_ASSISTANT));
    serverAssistant.apiRounds = serverAssistant.apiRounds.slice(0, 10);
    serverAssistant.content = 'x';
    SERVER_BODY = {
      id: 'c1', title: 'c1', rev: 8,
      updatedAt: 3, messages: [
        { role: 'user', _msgId: 'm0', content: 'q', timestamp: 1 },
        serverAssistant,
      ],
    };
    await loadConversationMessages('c1');
    const lm = conversations[0].messages[1];
    check('B_apiRounds_no_downgrade',
      Array.isArray(lm.apiRounds) && lm.apiRounds.length === 39);
  }

  // ══ C. Mid-stream partial local (10 rounds) + server 39 → upgrade to 39. ══
  {
    const localAssistant = {
      role: 'assistant', _msgId: 'm1', content: 'x', thinking: 't',
      toolRounds: [{ roundNum: 0 }],
      apiRounds: Array.from({ length: 10 }, (_, i) => ({ round: i + 1 })),
      finishReason: 'stop', usage: { prompt_tokens: 10 },
    };
    seedConv(localAssistant);
    SERVER_BODY = {
      id: 'c1', title: 'c1', rev: 9,
      updatedAt: 4, messages: [
        { role: 'user', _msgId: 'm0', content: 'q', timestamp: 1 },
        JSON.parse(JSON.stringify(_FULL_ASSISTANT)),
      ],
    };
    await loadConversationMessages('c1');
    const lm = conversations[0].messages[1];
    check('C_apiRounds_upgrade_to_longer',
      Array.isArray(lm.apiRounds) && lm.apiRounds.length === 39);
  }

  console.log(out.join('\n'));
  process.exit(0);
})();
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_merge_active_task_fills_terminal_fields():
    output = _run_harness(
        'a', _HARNESS_A,
        os.path.join(JS_DIR, 'core', 'conv_reducers.js'),
        os.path.join(JS_DIR, 'core', 'conversations.js'))
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'MERGE_ACTIVE_TASK terminal-field merge failures:\n' + output
    assert output.count('PASS') >= 18, f'expected >=18 PASS, got:\n{output}'
    print(output)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_NC_helper_call_is_load_bearing_in_conversations(tmp_path):
    """NEUTER: strip the ``_mergeTerminalTurnFields(...)`` CALL in
    conversations.js on a COPY → the kept-local message never receives
    apiRounds/_taskId → the round-table assertions fail. Proves the branch
    now ROUTES THROUGH the shared helper (no inline list left behind).
    Real file untouched."""
    conv_js = os.path.join(JS_DIR, 'core', 'conversations.js')
    with open(conv_js, encoding='utf-8') as f:
        src = f.read()

    needle = '        _mergeTerminalTurnFields(conv.messages[_mi], serverMsgs[_mi]);'
    assert src.count(needle) == 1, (
        'helper-call fragment drifted — update the neuter target')
    neutered = src.replace(needle, '', 1)
    assert neutered != src, 'neuter produced no change'

    copy = tmp_path / 'conversations_neutered_helper_call.js'
    copy.write_text(neutered, encoding='utf-8')

    output = _run_harness(
        'a_nc', _HARNESS_A,
        os.path.join(JS_DIR, 'core', 'conv_reducers.js'),
        str(copy))
    assert 'FAIL A_apiRounds_merged' in output, (
        'NEUTER did not bite: apiRounds still merged without the helper call.\n'
        + output)
    assert 'FAIL A_taskId_merged' in output, (
        'NEUTER did not bite: _taskId still merged without the helper call.\n'
        + output)

    with open(conv_js, encoding='utf-8') as f:
        assert f.read() == src, 'harness mutated the shipped conversations.js'


# ═══════════════════════════════════════════════════════════════════════
# Harness B — REAL initActiveTasks Case B (refresh-on-finished-task)
# ═══════════════════════════════════════════════════════════════════════
_HARNESS_B = r"""
const fs = require('fs');
global.window = global;
global.activeConvId = 'c1';
global.sessionStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
global.activeStreams = new Map();
global._editingMsgIdx = null;
global.debugLog = () => {};
global.config = {};
global.serverModel = 'm';
global.renderConversationList = () => {};
global.showStreamingUIForConv = () => {};
global._restoreConvToolState = () => {};
global._refreshServerQueue = () => {};
global.saveConversations = () => {};
global.syncConversationToServer = () => {};
global.getActiveConv = () => conversations.find((c) => c.id === activeConvId) || null;
global.ConvView = { replaceAll: () => {} };
global.AbortSignal = { timeout: () => undefined };
global.loadConversationsFromServer = async () => {};

// The poll payload for the finished task (td). NO `cost` — the poll
// endpoints never ship one; the lazy calcCostCny path owns cost here.
const _TD = {
  status: 'done',
  content: 'the final settled answer, longer than the partial buffer',
  thinking: 'th',
  toolRounds: [{ roundNum: 0, toolName: 'read_files' }],
  finishReason: 'stop',
  usage: { prompt_tokens: 100, completion_tokens: 10 },
  preset: 'p', model: 'kimi-k3', provider_id: 'sankuai', thinkingDepth: 'high',
  apiRounds: Array.from({ length: 39 }, (_, i) => ({
    round: i + 1, model: 'kimi-k3',
    usage: { prompt_tokens: 150000, completion_tokens: 800 },
  })),
  taskId: 'task-done-1',
  modifiedFiles: 2, modifiedFileList: [{ path: 'a.js' }],
  fallbackModel: 'fb-m', fallbackFrom: 'kimi-k3',
  fallbackReason: 'ratelimit: 429', fallbackKind: 'ratelimit',
};
global.Api = {
  chat: {
    activeResponse: async () => ({ ok: true, json: async () => [] }),
    poll: async () => ({ ok: true, json: async () => JSON.parse(JSON.stringify(_TD)) }),
  },
  conversations: { get: async () => null },
};

global.conversations = [];
eval(fs.readFileSync(process.argv[2], 'utf8'));  // REAL core/conv_reducers.js
eval(fs.readFileSync(process.argv[3], 'utf8'));  // REAL main/main_init_tasks.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

(async () => {
  // The degraded mid-stream local tail: NO terminal fields at all.
  conversations.push({
    id: 'c1', title: 't', createdAt: 1000, updatedAt: 1001,
    _needsLoad: false,
    activeTaskId: 'task-done-1',
    messages: [
      { role: 'user', content: 'q', timestamp: 1000 },
      { role: 'assistant', content: 'partial buf', thinking: '',
        toolRounds: [], model: 'kimi-k3', timestamp: 1001 },
    ],
  });
  await initActiveTasks();
  // _bgRecovery is fired WITHOUT await — flush the microtask/timer queue.
  await new Promise((r) => setTimeout(r, 80));
  const am = conversations[0].messages[1];
  // Pre-existing server-wins behaviour stays intact (controls).
  check('B_content_adopted', am.content === _TD.content);
  check('B_finishReason_adopted', am.finishReason === 'stop');
  check('B_usage_adopted', !!(am.usage && am.usage.prompt_tokens === 100));
  check('B_preset_adopted', am.preset === 'p');
  // The new terminal fields land through the shared helper.
  check('B_apiRounds_merged', Array.isArray(am.apiRounds) && am.apiRounds.length === 39);
  check('B_apiRounds_round39', !!(am.apiRounds && am.apiRounds[38] && am.apiRounds[38].round === 39));
  check('B_taskId_merged', am._taskId === 'task-done-1');
  check('B_provider_merged', am.provider_id === 'sankuai');
  check('B_thinkingDepth_merged', am.thinkingDepth === 'high');
  check('B_model_merged', am.model === 'kimi-k3');
  check('B_modifiedFileList_merged',
    Array.isArray(am.modifiedFileList) && am.modifiedFileList.length === 1);
  check('B_fallbackModel_merged', am.fallbackModel === 'fb-m');
  // The poll carries no cost — the helper must NOT fabricate one.
  check('B_cost_not_fabricated', am.cost === undefined);
  // Case B tail cleanup still runs (control).
  check('B_activeTask_cleared', conversations[0].activeTaskId === null);
  console.log(out.join('\n'));
  process.exit(0);
})();
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_case_b_poll_done_fills_terminal_fields():
    output = _run_harness(
        'b', _HARNESS_B,
        os.path.join(JS_DIR, 'core', 'conv_reducers.js'),
        os.path.join(JS_DIR, 'main', 'main_init_tasks.js'))
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'Case B terminal-field merge failures:\n' + output
    assert output.count('PASS') >= 13, f'expected >=13 PASS, got:\n{output}'
    print(output)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_NC_helper_call_is_load_bearing_in_case_b(tmp_path):
    """NEUTER: strip the ``_mergeTerminalTurnFields(...)`` CALL in
    main_init_tasks.js Case B on a COPY → apiRounds/_taskId never land on
    the refreshed tail → red. Real file untouched."""
    js = os.path.join(JS_DIR, 'main', 'main_init_tasks.js')
    with open(js, encoding='utf-8') as f:
        src = f.read()

    needle = """                _mergeTerminalTurnFields(am, Object.assign({}, td, {
                  _taskId: td.taskId || td.id || conv.activeTaskId,
                }));"""
    assert src.count(needle) == 1, (
        'Case B helper-call fragment drifted — update the neuter target')
    neutered = src.replace(needle, '', 1)
    assert neutered != src, 'neuter produced no change'

    copy = tmp_path / 'main_init_tasks_neutered.js'
    copy.write_text(neutered, encoding='utf-8')

    output = _run_harness(
        'b_nc', _HARNESS_B,
        os.path.join(JS_DIR, 'core', 'conv_reducers.js'),
        str(copy))
    assert 'FAIL B_apiRounds_merged' in output, (
        'NEUTER did not bite: apiRounds still merged without the helper call.\n'
        + output)
    assert 'FAIL B_taskId_merged' in output, (
        'NEUTER did not bite: _taskId still merged without the helper call.\n'
        + output)
    assert 'PASS B_content_adopted' in output, output

    with open(js, encoding='utf-8') as f:
        assert f.read() == src, 'harness mutated the shipped main_init_tasks.js'


# ═══════════════════════════════════════════════════════════════════════
# Harness C — REAL _verifyActiveConvFromServer Case 2 (cross-tab verify)
# ═══════════════════════════════════════════════════════════════════════
_HARNESS_C = r"""
const fs = require('fs');
global.window = global;
global.addEventListener = () => {};                    // window.addEventListener at module level
global.document = { addEventListener: () => {}, visibilityState: 'visible',
                    getElementById: () => null };      // document.addEventListener at module level
global.activeConvId = 'c1';
global.activeStreams = new Map();
global.debugLog = () => {};
global.config = {};
global._applySettingsToConv = () => {};
global.saveConversations = () => {};
global.ConvCache = { put: () => {} };
global.AbortSignal = { timeout: () => undefined };
let repaintCount = 0;
global.ConvView = { replaceAll: () => { repaintCount++; } };

let SERVER_BODY = null;
global.Api = {
  conversations: {
    get: async () => SERVER_BODY,
  },
};

global.conversations = [];
eval(fs.readFileSync(process.argv[2], 'utf8'));  // REAL core/conv_reducers.js
eval(fs.readFileSync(process.argv[3], 'utf8'));  // REAL core/cross_tab_sync.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const _SERVER_TAIL_FULL = {
  role: 'assistant', content: 'same length', thinking: '',
  toolRounds: [], timestamp: 1001,
  finishReason: 'stop',
  usage: { prompt_tokens: 100, completion_tokens: 10 },
  apiRounds: Array.from({ length: 39 }, (_, i) => ({
    round: i + 1, usage: { prompt_tokens: 150000 } })),
  _taskId: 'task-x', cost: { costCny: 18.5138 },
  provider_id: 'sankuai', preset: 'p', thinkingDepth: 'high',
  model: 'kimi-k3',
  modifiedFiles: 2, modifiedFileList: [{ path: 'a.js' }],
};

function seed() {
  conversations.length = 0;
  repaintCount = 0;
  conversations.push({
    id: 'c1', title: 't', createdAt: 1000, updatedAt: 1001,
    _serverMsgCount: 2,
    messages: [
      { role: 'user', content: 'q', timestamp: 1000 },
      // Degraded tail: same content length as the server (growth gate can
      // never fire), ZERO terminal fields.
      { role: 'assistant', content: 'same length', thinking: '',
        toolRounds: [], timestamp: 1001 },
    ],
  });
}

(async () => {
  // ══ A. Equal count + equal content length → growth gate never fires;
  //    terminal fields must land via the helper anyway. ══
  {
    seed();
    SERVER_BODY = {
      id: 'c1', title: 't', updatedAt: 2000, rev: 9, settings: {},
      messages: [
        { role: 'user', content: 'q', timestamp: 1000 },
        JSON.parse(JSON.stringify(_SERVER_TAIL_FULL)),
      ],
    };
    const changed = await _verifyActiveConvFromServer('c1');
    const am = conversations[0].messages[1];
    check('C_changed_true', changed === true);
    check('C_apiRounds_merged', Array.isArray(am.apiRounds) && am.apiRounds.length === 39);
    check('C_taskId_merged', am._taskId === 'task-x');
    check('C_cost_merged', !!(am.cost && am.cost.costCny === 18.5138));
    check('C_provider_merged', am.provider_id === 'sankuai');
    check('C_preset_merged', am.preset === 'p');
    check('C_thinkingDepth_merged', am.thinkingDepth === 'high');
    check('C_finishReason_merged', am.finishReason === 'stop');
    check('C_usage_merged', !!(am.usage && am.usage.prompt_tokens === 100));
    check('C_modifiedFileList_merged',
      Array.isArray(am.modifiedFileList) && am.modifiedFileList.length === 1);
    check('C_repaint_fired', repaintCount >= 1);
    // Content must NOT be clobbered by the fill (same length, keep-local).
    check('C_content_untouched', am.content === 'same length');
  }

  // ══ B. Growth control: server content longer → the existing server-wins
  //    adoption still fires alongside the helper (no interference). ══
  {
    seed();
    const serverTail = JSON.parse(JSON.stringify(_SERVER_TAIL_FULL));
    serverTail.content = 'a genuinely longer settled answer body';
    SERVER_BODY = {
      id: 'c1', title: 't', updatedAt: 2001, rev: 10, settings: {},
      messages: [
        { role: 'user', content: 'q', timestamp: 1000 },
        serverTail,
      ],
    };
    const changed = await _verifyActiveConvFromServer('c1');
    const am = conversations[0].messages[1];
    check('C_growth_adopted_content', am.content === 'a genuinely longer settled answer body');
    check('C_growth_changed_true', changed === true);
    check('C_growth_apiRounds_too', Array.isArray(am.apiRounds) && am.apiRounds.length === 39);
  }

  console.log(out.join('\n'));
  process.exit(0);
})();
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_cross_tab_case2_fills_terminal_fields_without_growth():
    output = _run_harness(
        'c', _HARNESS_C,
        os.path.join(JS_DIR, 'core', 'conv_reducers.js'),
        os.path.join(JS_DIR, 'core', 'cross_tab_sync.js'))
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'cross-tab Case 2 terminal-field merge failures:\n' + output
    assert output.count('PASS') >= 14, f'expected >=14 PASS, got:\n{output}'
    print(output)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_NC_helper_call_is_load_bearing_in_cross_tab(tmp_path):
    """NEUTER: strip the ``_mergeTerminalTurnFields(...)`` CALL in
    cross_tab_sync.js Case 2 on a COPY → with equal content length nothing
    lands and no repaint fires → red. Real file untouched."""
    js = os.path.join(JS_DIR, 'core', 'cross_tab_sync.js')
    with open(js, encoding='utf-8') as f:
        src = f.read()

    needle = """      if (_mergeTerminalTurnFields(am, serverLast) > 0) {
        conv.updatedAt = data.updatedAt || data.updated_at || conv.updatedAt;
        changed = true;
      }"""
    assert src.count(needle) == 1, (
        'cross-tab helper-call fragment drifted — update the neuter target')
    neutered = src.replace(needle, '', 1)
    assert neutered != src, 'neuter produced no change'

    copy = tmp_path / 'cross_tab_sync_neutered.js'
    copy.write_text(neutered, encoding='utf-8')

    output = _run_harness(
        'c_nc', _HARNESS_C,
        os.path.join(JS_DIR, 'core', 'conv_reducers.js'),
        str(copy))
    assert 'FAIL C_apiRounds_merged' in output, (
        'NEUTER did not bite: apiRounds still merged without the helper call.\n'
        + output)
    assert 'FAIL C_taskId_merged' in output, (
        'NEUTER did not bite: _taskId still merged without the helper call.\n'
        + output)
    # The growth path does NOT route through the helper — stays green.
    assert 'PASS C_growth_adopted_content' in output, output

    with open(js, encoding='utf-8') as f:
        assert f.read() == src, 'harness mutated the shipped cross_tab_sync.js'


# ═══════════════════════════════════════════════════════════════════════
# Harness D — REAL _msgFingerprint moves when terminal fields land
# ═══════════════════════════════════════════════════════════════════════
_HARNESS_D = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body></body>',
  // argv[4]=core/translation_model.js (ships translationFingerprint, which
  // _msgFingerprint delegates to) — loaded BEFORE the fn under test.
  targets: [process.argv[4], process.argv[2]],   // then ui/chat_render.js
  globals: {},
});

if (typeof _msgFingerprint !== 'function') {
  check('fingerprint_exposed', false);
  report();
  return;
}
check('fingerprint_exposed', true);

// The exact pre-merge steady state: settled-looking message (finishReason +
// usage + cost all present) but NO apiRounds / _taskId — the screenshot shape.
function degradedMsg() {
  return {
    role: 'assistant',
    content: 'The final answer.',
    thinking: '',
    finishReason: 'stop',
    usage: { prompt_tokens: 5854882, completion_tokens: 34741,
             cache_read_tokens: 5669120, cache_write_tokens: 0 },
    cost: { costCny: 18.5138, costUsd: 2.5 },
    model: 'kimi-k3',
    toolRounds: [{ roundNum: 0 }],
  };
}

const before = _msgFingerprint(degradedMsg());

// apiRounds land (the merge fills 39 rounds) → fingerprint MUST move or the
// surgical diff never repaints the rounds-less finish bar.
const m1 = degradedMsg();
m1.apiRounds = Array.from({ length: 39 }, (_, i) => ({ round: i + 1 }));
const withRounds = _msgFingerprint(m1);
check('fp_moves_when_apiRounds_land', before !== withRounds);

// The fold is count-sensitive: a partial (mid-stream) 10-round list upgrading
// to the settled 39 must ALSO move it (upgrade-if-longer repaint).
const m2 = degradedMsg();
m2.apiRounds = Array.from({ length: 10 }, (_, i) => ({ round: i + 1 }));
const with10 = _msgFingerprint(m2);
check('fp_moves_when_round_count_grows', with10 !== withRounds);

// _taskId lands → moves (the popover Task ID row repaint).
const m3 = degradedMsg();
m3.apiRounds = m1.apiRounds;
m3._taskId = 'task-settled-1';
const withTaskId = _msgFingerprint(m3);
check('fp_moves_when_taskId_lands', withRounds !== withTaskId);

// usage landing on a bare message moves it too (the token-tag repaint) —
// guards the same class for a message painted before any usage existed.
const bare = { role: 'assistant', content: 'x', thinking: '' };
const bareFp = _msgFingerprint(bare);
const withUsage = _msgFingerprint({ ...bare, usage: { prompt_tokens: 5 } });
check('fp_moves_when_usage_lands', bareFp !== withUsage);

// Stability control: two equally-degraded messages fingerprint identically
// (the fold adds no noise when the fields are absent).
check('NC_degraded_pair_stable',
  _msgFingerprint(degradedMsg()) === _msgFingerprint(degradedMsg()));

report();
"""


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_fingerprint_moves_when_terminal_fields_land():
    run_harness(
        target_js=os.path.join(JS_DIR, 'ui', 'chat_render.js'),
        body_js=_HARNESS_D,
        extra_targets=[os.path.join(JS_DIR, 'core', 'translation_model.js')],
        min_pass=6,
        label='terminal-fields-fingerprint',
    )
