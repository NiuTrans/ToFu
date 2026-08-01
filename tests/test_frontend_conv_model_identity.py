"""Regression: a conversation's STORED model must survive every open path,
and a paint-time default must never become stored truth.

WHY (bug report, conv ms352oniikgq10, 2026-07-27)
--------------------------------------------------
The user had Claude Opus 5 selected. Opening this ONE conversation silently
flipped the composer's model picker to Kimi K3 — while the right-hand tag and
the stream phase text (which read the MESSAGE / task model, a different
source) correctly said ``yuju-claude-opus-5-evaDaily``. The DB row's
``settings.model`` was ``yuju-claude-opus-5-evaDaily`` and every one of the
task's 24 LLM rounds ran on Opus 5 — no fallback ever fired. So the composer
was displaying a model the conversation never used.

Kimi K3 specifically, because ``model_defaults.default_model = kimi-k3`` →
``serverModel`` → every ``|| serverModel`` fall-through lands there.

MECHANISM (two independent layers, both required)
-------------------------------------------------
1. ``ConvCache._extractSettings`` mirrors ONLY ``conv.model`` into IndexedDB
   with no ``preset`` / ``effort`` fallback, while the reader
   ``_applySettingsToConv`` resolves ``model || preset || effort``. A conv
   cached while ``conv.model`` was unset stores a model-LESS record, and on
   reopen the reader's guard is false for all three keys so it skips the
   assignment entirely — ``conv.model`` stays undefined.

2. ``loadConversationMessages`` Phase 2 dispatches to one of KEEP_LOCAL /
   MERGE_ACTIVE_TASK / OVERWRITE / NOOP. Only OVERWRITE called
   ``_applySettingsToConv``; the MERGE_ACTIVE_TASK branch merged message
   BODIES but never applied the server's ``settings`` — so the correct model
   sitting in the payload was read and DISCARDED. That branch's gate is
   ``conv.activeTaskId && hasLocalData``, which is why the bug needed an
   active task and why every other conversation self-healed via OVERWRITE.

WHY IT WAS DESTRUCTIVE, not cosmetic
------------------------------------
The composer value is not read-only: ``_saveConvToolState`` and
``loadConversation``'s prevConv snapshot both do
``conv.model = config.model || serverModel`` and PATCH it. So the wrong paint
laundered itself into persisted truth and the next send would run on the
wrong model. Production evidence: of 26 settings-vs-message model mismatches
in the 150 most-recent convs, 17 carry genuine fallback markers, and two
(``mrnejm4zdfe5ba``, ``mrnem0a0jatj95``) point the WRONG way — stored
``kimi-k3`` on convs whose turns ran ``aws.claude-opus-4.7/4.8``, which a
fallback can never produce.

INVARIANTS GUARDED (behaviour, not source text — charter 2026-07-27)
--------------------------------------------------------------------
  A. MERGE_ACTIVE_TASK: a conv with a live activeTaskId whose server payload
     carries ``settings.model = X`` ends up with ``conv.model === X``, never
     ``serverModel``. THIS is the reported bug's exact branch.
  B. The IDB settings mirror round-trips a conv that has only ``preset`` /
     ``effort`` (no flat ``model``) — cache → reader → model still resolves.
  C. A paint-time default (the ``|| serverModel`` display fallback) never
     reaches ``conv.model`` / the persisted settings payload; only an
     explicit user model choice does.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

from tests._jsdom import JS_DIR

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))


def _node_available() -> bool:
    return bool(shutil.which('node'))


def _conv_family(*, override=None):
    """The drift-proof conv-family eval list (see
    tests/_conv_bundle_sources.conv_family_sources). Harness A drives
    loadConversationMessages, whose OVERWRITE path calls
    _rescuableLocalTail (core/conv_rescue_tail.js) — a leaf the old
    three-file inline list never loaded, so the branch threw inside the
    loader's own try/catch and the settings call never fired (A3 RED,
    2026-08-01)."""
    sys.path.insert(0, HERE)
    from _conv_bundle_sources import conv_family_sources
    return conv_family_sources(override=override)


def _run_harness(name: str, body: str, *js_paths: str) -> str:
    harness = os.path.join(HERE, f'_conv_model_identity_{name}.js')
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
# Harness A — REAL loadConversationMessages, MERGE_ACTIVE_TASK branch.
#
# Reproduces the reported conversation exactly: activeTaskId pinned, local
# (cache-shaped) messages present, and the server payload carrying the true
# settings.model. The composer's resolution is then evaluated the way
# _restoreConvToolState does it.
# ═══════════════════════════════════════════════════════════════════════
_HARNESS_A = r"""
const fs = require('fs');
global.window = global;
global.document = { getElementById: () => null, addEventListener: () => {},
                    querySelectorAll: () => [], visibilityState: 'visible' };
global.activeConvId = 'c1';
global.activeStreams = new Map();
global._editingMsgIdx = null;
global.debugLog = () => {};
global.config = {};
global.saveConversations = () => {};
global.syncConversationToServer = () => {};
global._refreshServerQueue = () => {};
global._mergeServerTranslations = () => 0;
global._retriggerHgTranslations = () => {};
global._hydrateImageBase64 = () => {};
global._setCacheVerifying = () => {};
global._bgRefreshChat = () => {};
global.errorEnvelopeKind = () => '';
global.attachCompactionMarkersToConversation = null;
// The production value that made the symptom specifically "Kimi K3".
global.serverModel = 'kimi-k3';
global.renderConversationList = () => {};
global.showStreamingUIForConv = () => {};
global._restoreConvToolState = () => {};
global._convSorter = (a, b) => (b.updatedAt || 0) - (a.updatedAt || 0);
global.apiUrl = (p) => p;
global.AbortSignal = { timeout: () => undefined };
global.convHasPendingSync = () => false;
global._serverHasTranslationLocalLacks = () => false;
global.ConvCache = {
  isAvailable: () => true,
  get: async () => null,
  getMeta: async () => null,
  getAllMeta: async () => [],
  put: async () => {},
  remove: async () => {},
};
global.ConvView = { replaceAll: () => {} };

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
for (const f of process.argv.slice(2)) eval(fs.readFileSync(f, 'utf8'));  // bundle-order conv family via _conv_bundle_sources.conv_family_sources

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const TRUE_MODEL = 'yuju-claude-opus-5-evaDaily';

// Exactly what _restoreConvToolState feeds _applyModelUI.
function composerModelFor(conv) {
  return conv.model || conv.preset || conv.effort || serverModel;
}

function seedPinnedConv(localModelFields) {
  conversations.length = 0;
  const conv = {
    id: 'c1', title: 'c1', _needsLoad: true,
    messages: [
      { role: 'user', _msgId: 'm0', content: 'q', timestamp: 1 },
      { role: 'assistant', _msgId: 'm1', content: 'a', thinking: '',
        toolRounds: [], model: TRUE_MODEL, timestamp: 2 },
    ],
    createdAt: 1, updatedAt: 1,
    // The MERGE_ACTIVE_TASK gate — the reported conv held this pin.
    activeTaskId: '4d5d4e81-4bbd-49bb-a5f1-b3cfbec90d9d',
  };
  Object.assign(conv, localModelFields || {});
  conversations.push(conv);
  return conv;
}

function serverBodyWithModel(model) {
  return {
    id: 'c1', title: 'c1', rev: 7, updatedAt: 2,
    settings: { model: model, preset: model, thinkingDepth: 'max' },
    messages: [
      { role: 'user', _msgId: 'm0', content: 'q', timestamp: 1 },
      { role: 'assistant', _msgId: 'm1', content: 'a', thinking: '',
        toolRounds: [], model: model, timestamp: 2 },
    ],
  };
}

(async () => {
  // ══ A1. THE REPORTED BUG. Local conv has NO model (the model-less cache
  //    record). Server payload carries the true Opus 5. After the open, the
  //    composer must show Opus 5 — not serverModel/kimi-k3. ══
  {
    const conv = seedPinnedConv({});   // conv.model undefined
    SERVER_BODY = serverBodyWithModel(TRUE_MODEL);
    await loadConversationMessages('c1');
    check('A1_model_adopted_from_server_settings', conv.model === TRUE_MODEL);
    check('A1_composer_not_serverModel', composerModelFor(conv) !== 'kimi-k3');
    check('A1_composer_shows_true_model', composerModelFor(conv) === TRUE_MODEL);
    // The branch must still be the keep-local one (control: no clobber).
    check('A1_control_activeTask_pin_kept',
      conv.activeTaskId === '4d5d4e81-4bbd-49bb-a5f1-b3cfbec90d9d');
    check('A1_control_messages_kept', conv.messages.length === 2);
  }

  // ══ A2. Other per-conv toolbar settings ride the same seam — a pinned
  //    conv must not silently lose its thinkingDepth either. ══
  {
    const conv = seedPinnedConv({});
    SERVER_BODY = serverBodyWithModel(TRUE_MODEL);
    await loadConversationMessages('c1');
    check('A2_thinkingDepth_adopted', conv.thinkingDepth === 'max');
  }

  // ══ A3. Control — an IDLE conv (no activeTaskId) already worked via
  //    OVERWRITE. Proves the harness itself isn't trivially passing and
  //    pins the self-healing path so a fix can't regress it. ══
  {
    const conv = seedPinnedConv({});
    conv.activeTaskId = null;
    SERVER_BODY = serverBodyWithModel(TRUE_MODEL);
    await loadConversationMessages('c1');
    check('A3_idle_conv_model_adopted', conv.model === TRUE_MODEL);
  }

  console.log(out.join('\n'));
  process.exit(0);
})();
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_merge_active_task_adopts_server_model():
    """INVARIANT A — the reported bug's exact branch."""
    output = _run_harness(
        'a', _HARNESS_A,
        *_conv_family())
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, (
        'a pinned (active-task) conversation lost its stored model:\n' + output)
    assert output.count('PASS') >= 7, f'expected >=7 PASS, got:\n{output}'
    print(output)


# ═══════════════════════════════════════════════════════════════════════
# Harness B — the IDB settings mirror must round-trip the model.
#
# Drives the REAL _extractSettings (via ConvCache.put's record builder) and
# the REAL _applySettingsToConv reader back to back.
# ═══════════════════════════════════════════════════════════════════════
_HARNESS_B = r"""
const fs = require('fs');
global.window = global;
global.indexedDB = undefined;          // ConvCache degrades; we only need the
global.debugLog = () => {};            // pure settings extract/apply pair.
global.apiUrl = (p) => p;
global.serverModel = 'kimi-k3';

eval(fs.readFileSync(process.argv[2], 'utf8'));  // REAL core/conv_apply_settings.js
eval(fs.readFileSync(process.argv[3], 'utf8'));  // REAL idb-cache.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const TRUE_MODEL = 'yuju-claude-opus-5-evaDaily';

const extract = (typeof ConvCache !== 'undefined'
                 && ConvCache.__testExtractSettings)
  ? ConvCache.__testExtractSettings
  : null;

if (!extract) {
  check('B_extractSettings_reachable', false);
  console.log(out.join('\n'));
  process.exit(0);
}
check('B_extractSettings_reachable', true);

function roundTrip(conv) {
  const mirrored = JSON.parse(JSON.stringify(extract(conv)));
  const restored = {};
  _applySettingsToConv(restored, mirrored);
  return restored;
}

// ══ B1. A conv carrying the flat model round-trips (control). ══
check('B1_flat_model_round_trips',
  roundTrip({ model: TRUE_MODEL }).model === TRUE_MODEL);

// ══ B2. A conv carrying ONLY `preset` (the legacy/alternate shape the
//    reader accepts) must survive the mirror. The writer previously
//    persisted only `model`, so this shape cached as model-LESS and the
//    reader then skipped the assignment entirely. ══
check('B2_preset_only_round_trips',
  roundTrip({ preset: TRUE_MODEL }).model === TRUE_MODEL);

// ══ B3. Same for the `effort` shape the reader also resolves. ══
check('B3_effort_only_round_trips',
  roundTrip({ effort: TRUE_MODEL }).model === TRUE_MODEL);

// ══ B4. A genuinely model-less conv must NOT gain a fabricated model —
//    the mirror may not invent serverModel on the user's behalf. ══
const bare = roundTrip({});
check('B4_no_model_is_not_fabricated',
  !bare.model || bare.model === undefined);

console.log(out.join('\n'));
process.exit(0);
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_idb_settings_mirror_round_trips_model():
    """INVARIANT B — the cache writer must mirror the reader's resolution."""
    output = _run_harness(
        'b', _HARNESS_B,
        os.path.join(JS_DIR, 'core', 'conv_apply_settings.js'),
        os.path.join(JS_DIR, 'idb-cache.js'))
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'IDB settings mirror dropped the model:\n' + output
    assert output.count('PASS') >= 5, f'expected >=5 PASS, got:\n{output}'
    print(output)


# ═══════════════════════════════════════════════════════════════════════
# Harness C — a paint-time default must never become STORED truth.
#
# ★ Drives the REAL PRODUCTION ENTRY POINTS — `_restoreConvToolState` (the
#   conversation-open path) and `_resetToolsToDefaults` (new chat) — NOT
#   `_applyModelUI` directly.
#
#   An earlier version of this harness called `_applyModelUI(undefined)`, a
#   call shape NO shipped code produces: every real caller pre-resolved
#   `conv.model || serverModel` first, so the provisional flag was always
#   false and the guard was dead code while this test stayed green. That is
#   charter's third guard-failure mode — "a green guard testing code that
#   never existed". The rule this harness now obeys: enter through the same
#   door production does.
# ═══════════════════════════════════════════════════════════════════════
_HARNESS_C = r"""
const fs = require('fs');
global.window = global;
global.document = { getElementById: () => null, querySelector: () => null,
                    querySelectorAll: () => [], addEventListener: () => {} };
global.debugLog = () => {};
global.serverModel = 'kimi-k3';
global.config = {};
global.projectState = { active: false, path: '', extraRoots: [] };
global.activeStreams = new Map();
global.pendingImages = []; global.pendingPdfTexts = [];
global.saveConversations = () => {};
global._syncToolStateDebounced = () => {};
global.updateSubmenuCounts = () => {};
global.t = (k) => k;
global.requestAnimationFrame = (f) => f();
global._scheduleReflow = () => {};
global._updateDepthButtons = () => {};
global._modelShortName = (m) => m;
global._isThinkingCapable = () => false;
global._detectBrand = () => 'generic';
global._DEPTH_ICONS = {}; global._DEPTH_LABELS = {};
global._LEGACY_PRESET_TO_MODEL = {};
global.Api = { chat: { patchToolState: async () => ({ ok: true }) } };

// Toolbar globals _saveConvToolState reads.
global.searchMode = 'multi';
['fetchEnabled','codeExecEnabled','browserEnabled','desktopEnabled',
 'memoryEnabled','schedulerEnabled','swarmEnabled','endpointEnabled',
 'autopilotEnabled','imageGenEnabled','imageGenMode','humanGuidanceEnabled',
 'autoTranslate','thinkingEnabled'].forEach((k) => { global[k] = false; });
global.activeFlow = ''; global.chatMode = 'chat';
global._igSelectedModel = 'gemini'; global._igSelectedCount = 1;
global._igSelectedAspect = '1:1'; global._igSelectedResolution = '1K';

let ACTIVE = null;
global.getActiveConv = () => ACTIVE;

// UI painters _restoreConvToolState / _resetToolsToDefaults fan out to.
// Stubbed because this harness asserts STATE (what gets stored), not pixels.
['_applySearchModeUI','_applyFetchEnabledUI','_applyCodeExecUI','_applyBrowserUI',
 '_applyDesktopUI','_applyMemoryUI','_applySwarmUI','_applyEndpointUI',
 '_applyAutopilotUI','_applyFlowUI','_applyImageGenToolUI','_applyImageGenUI',
 '_applyHumanGuidanceUI','_applyChatModeUI','_applyAutoTranslateUI',
 '_applySchedulerUI','updateContextBar','presenceRefresh','projectBrainRefresh',
 'convInfluenceRefresh','exitPaperMode','_clearProjectStateLocal',
].forEach((k) => { global[k] = () => {}; });
global.convAutoTranslate = () => false;
global._deriveChatModeFromFlags = () => 'chat';
global.paperMode = false;
global.autoTranslate = false;
/* Dependencies the REAL painters in main.js reach for. main.js DEFINES several
 * of the _apply*UI functions itself (so it overwrites the stubs above); those
 * real bodies then call these helpers, which live in other bundle files. */
global._updateMemoryModalBtn = () => {};
global._renderHintHtml = (s) => s;
global._inputSendHintText = () => '';
global.syncToolsetBanner = () => {};
global._igModelsLoaded = true;
global._loadIgModels = () => {};
global._resetToolsToDefaultsExtra = () => {};

global.isChatModel = () => true;
global.applyCapabilityTaxonomy = () => {};
global._populateModelDropdown = () => {};
global._warnModelCapsMissing = () => {};
global._maybeAutoOpenSettings = () => {};
global.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
global._CHAT_MODE_DEFAULTS = { chat: {}, studio: {} };

/* main.js ends in a boot IIFE that wires the whole app. We are unit-driving
 * three functions out of it, so stub what boot touches. It logs a ConvView
 * banner and catches its own errors, so a partial environment is fine — but
 * the stubs must exist or the crash aborts before our assertions run. */
global.ConvView = { apply: () => {}, replaceAll: () => {} };
global._loadServerConfigAndPopulate = () => {};
global.refreshInputSendHint = () => {};
global.renderConversationList = () => {};
global.addEventListener = () => {};
global._installViewportHeightGuard = () => {};
global.sessionStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
global.navigator = { userAgent: 'node' };
global.location = { search: '', pathname: '/', hash: '' };
global.setInterval = () => 0;
global.matchMedia = () => ({ matches: false, addEventListener: () => {} });

eval(fs.readFileSync(process.argv[2], 'utf8'));  // REAL main/main_toolbar_ui.js
/* Load the REAL main.js but WITHOUT its trailing boot IIFE. We are
 * unit-driving three of its functions; running the app's whole wiring block
 * would require stubbing the entire UI. The boot block is located by its own
 * marker comment and sliced off AT RUNTIME from the shipped source — never
 * copied into this file, so the functions under test are always the shipped
 * ones (charter: no hand-copied production logic in harnesses). */
{
  const mainSrc = fs.readFileSync(process.argv[3], 'utf8');
  const marker = '// \u2500\u2500 Event bindings \u2500\u2500';
  const cut = mainSrc.indexOf(marker);
  if (cut < 0) {
    console.log('FAIL C_harness_boot_marker_found');
    process.exit(0);
  }
  eval(mainSrc.slice(0, cut));
}

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const TRUE_MODEL = 'yuju-claude-opus-5-evaDaily';
check('C_harness_boot_marker_found', true);
check('C_harness_drives_real_open_path',
  typeof _restoreConvToolState === 'function'
  && typeof _resetToolsToDefaults === 'function'
  && typeof _saveConvToolState === 'function'
  && typeof selectModel === 'function');

// ══ C1. THE LAUNDERING PATH, entered exactly as production does. Opening a
//    conversation that stored no model paints the serverModel default; a
//    later tool toggle must NOT stamp that default onto the conversation.
//    Without the fix this writes 'kimi-k3' and PATCHes it to the server. ══
{
  ACTIVE = { id: 'c1', messages: [{ role: 'user', content: 'q' }], model: undefined };
  _restoreConvToolState(ACTIVE);          // ← the REAL conversation-open path
  check('C1_composer_paints_default', config.model === 'kimi-k3');
  _saveConvToolState();                   // ← the REAL write-back
  check('C1_default_paint_not_persisted', ACTIVE.model !== 'kimi-k3');
}

// ══ C2. An EXPLICIT user choice must still persist — the guard may not
//    break the normal path (this is what makes C1 a real constraint and
//    not just "never write the field"). ══
{
  ACTIVE = { id: 'c2', messages: [{ role: 'user', content: 'q' }], model: undefined };
  selectModel(TRUE_MODEL);                // the real user-choice entry point
  check('C2_explicit_choice_persisted', ACTIVE.model === TRUE_MODEL);
}

// ══ C3. A conv that ALREADY stored a model: opening it restores that model,
//    and an unrelated toggle-save must never downgrade it to the default. ══
{
  ACTIVE = { id: 'c3', messages: [{ role: 'user', content: 'q' }], model: TRUE_MODEL };
  _restoreConvToolState(ACTIVE);          // real open path, stored value present
  check('C3_stored_model_restored', config.model === TRUE_MODEL);
  _saveConvToolState();
  check('C3_stored_model_not_downgraded', ACTIVE.model === TRUE_MODEL);
}

// ══ C4. NEW CHAT (_resetToolsToDefaults) is the other production entry that
//    paints a default nobody chose. Its value must be provisional too. ══
{
  _resetToolsToDefaults();
  check('C4_new_chat_paints_default', config.model === 'kimi-k3');
  ACTIVE = { id: 'c4', messages: [{ role: 'user', content: 'q' }], model: undefined };
  _saveConvToolState();
  check('C4_new_chat_default_not_persisted', ACTIVE.model !== 'kimi-k3');
}

// ══ C5. Provenance must SURVIVE the config-load repaint. Boot re-paints the
//    toolbar once the model dropdown is populated; if that repaint promoted
//    the fallback to a "choice", the guard would silently reopen. ══
{
  ACTIVE = { id: 'c5', messages: [{ role: 'user', content: 'q' }], model: undefined };
  _restoreConvToolState(ACTIVE);
  _applyModelUI(config._modelIsProvisional ? null : config.model);  // the repaint shape
  _saveConvToolState();
  check('C5_provisional_survives_repaint', ACTIVE.model !== 'kimi-k3');
}

console.log(out.join('\n'));
process.exit(0);
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_paint_default_never_becomes_stored_model():
    """INVARIANT C — the destructive half: a display fallback must never be
    laundered into the conversation's persisted identity."""
    output = _run_harness(
        'c', _HARNESS_C,
        os.path.join(JS_DIR, 'main', 'main_toolbar_ui.js'),
        os.path.join(JS_DIR, 'main.js'))
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, (
        'a paint-time default reached conv.model (write-back laundering):\n'
        + output)
    assert output.count('PASS') >= 10, f'expected >=10 PASS, got:\n{output}'
    print(output)



# ═══════════════════════════════════════════════════════════════════════
# NEUTER round — each fix must be LOAD-BEARING. Every neuter runs against a
# COPY; the shipped files are byte-verified untouched afterwards.
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_NC_merge_active_task_settings_call_is_load_bearing(tmp_path):
    """NEUTER A: strip the ``_applySettingsToConv`` call from the
    MERGE_ACTIVE_TASK branch → a pinned conv again discards the server's
    model and the composer falls back to serverModel → red."""
    conv_js = os.path.join(JS_DIR, 'core', 'conversations.js')
    with open(conv_js, encoding='utf-8') as f:
        src = f.read()

    needle = """      {
        const keepPinned = conv.pinned, keepPinnedAt = conv.pinnedAt;
        _applySettingsToConv(conv, data.settings);
        conv.pinned = keepPinned; conv.pinnedAt = keepPinnedAt;
        if (convId === activeConvId && typeof _restoreConvToolState === 'function') {
          _restoreConvToolState(conv);
        }
      }"""
    assert src.count(needle) == 1, (
        'MERGE_ACTIVE_TASK settings-apply fragment drifted — '
        'update the neuter target')
    copy = tmp_path / 'conversations_neutered_settings.js'
    copy.write_text(src.replace(needle, '', 1), encoding='utf-8')

    output = _run_harness(
        'a_nc', _HARNESS_A,
        *_conv_family(override={'core/conversations.js': str(copy)}))
    assert 'FAIL A1_model_adopted_from_server_settings' in output, (
        'NEUTER did not bite: model still adopted without the settings call.\n'
        + output)
    assert 'FAIL A1_composer_shows_true_model' in output, output
    # The IDLE path must stay green — it routes through OVERWRITE, proving
    # the neuter hit the active-task branch specifically.
    assert 'PASS A3_idle_conv_model_adopted' in output, output

    with open(conv_js, encoding='utf-8') as f:
        assert f.read() == src, 'harness mutated the shipped conversations.js'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_NC_cache_mirror_resolution_is_load_bearing(tmp_path):
    """NEUTER B: revert the cache mirror to persisting only the flat
    ``conv.model`` → the preset/effort shapes cache as model-LESS → red."""
    idb_js = os.path.join(JS_DIR, 'idb-cache.js')
    with open(idb_js, encoding='utf-8') as f:
        src = f.read()

    needle = '      model: conv.model || conv.preset || conv.effort,'
    assert src.count(needle) == 1, (
        'cache-mirror model resolution drifted — update the neuter target')
    copy = tmp_path / 'idb_cache_neutered.js'
    copy.write_text(src.replace(needle, '      model: conv.model,', 1),
                    encoding='utf-8')

    output = _run_harness(
        'b_nc', _HARNESS_B,
        os.path.join(JS_DIR, 'core', 'conv_apply_settings.js'), str(copy))
    assert 'FAIL B2_preset_only_round_trips' in output, (
        'NEUTER did not bite: preset shape survived a model-only mirror.\n'
        + output)
    assert 'FAIL B3_effort_only_round_trips' in output, output
    # The flat-model control must stay green.
    assert 'PASS B1_flat_model_round_trips' in output, output

    with open(idb_js, encoding='utf-8') as f:
        assert f.read() == src, 'harness mutated the shipped idb-cache.js'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_NC_provisional_guard_is_load_bearing(tmp_path):
    """NEUTER C: restore the old unconditional
    ``conv.model = config.model || serverModel`` write-back → the paint-time
    default is laundered into stored truth again → red."""
    main_js = os.path.join(JS_DIR, 'main.js')
    with open(main_js, encoding='utf-8') as f:
        src = f.read()

    needle = """  if (!config._modelIsProvisional && config.model) {
    conv.model = config.model;
  }"""
    assert src.count(needle) == 1, (
        'provisional write-back guard drifted — update the neuter target')
    copy = tmp_path / 'main_neutered_provisional.js'
    copy.write_text(
        src.replace(needle, '  conv.model = config.model || serverModel;', 1),
        encoding='utf-8')

    output = _run_harness(
        'c_nc', _HARNESS_C,
        os.path.join(JS_DIR, 'main', 'main_toolbar_ui.js'), str(copy))
    assert 'FAIL C1_default_paint_not_persisted' in output, (
        'NEUTER did not bite: the default paint was not persisted even '
        'without the guard.\n' + output)
    assert 'FAIL C4_new_chat_default_not_persisted' in output, (
        'NEUTER did not bite on the new-chat entry point.\n' + output)
    assert 'FAIL C5_provisional_survives_repaint' in output, (
        'NEUTER did not bite on the config-load repaint.\n' + output)
    # The explicit-choice paths must stay green either way — they are what
    # prove the guard is narrow (it blocks defaults, not all writes) rather
    # than a blanket "never write conv.model".
    assert 'PASS C2_explicit_choice_persisted' in output, output
    assert 'PASS C3_stored_model_not_downgraded' in output, output

    with open(main_js, encoding='utf-8') as f:
        assert f.read() == src, 'harness mutated the shipped main.js'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_NC_single_resolver_is_load_bearing(tmp_path):
    """NEUTER D: restore the pre-resolved fallback at the conversation-open
    call site (``_applyModelUI(conv.model || ... || serverModel)``) → the
    value arrives truthy, provenance is lost, and the default is laundered
    again → red.

    This is the neuter that the ORIGINAL layer-3 guard lacked. The provisional
    flag is only meaningful if the fallback is resolved INSIDE
    ``_applyModelUI``; a caller that pre-resolves it silently disables the
    whole mechanism while every assertion downstream still passes. Charter's
    third guard-failure mode ("a green guard testing code that never
    existed") is exactly what this prevents from recurring.
    """
    main_js = os.path.join(JS_DIR, 'main.js')
    with open(main_js, encoding='utf-8') as f:
        src = f.read()

    needle = '  _applyModelUI(conv.model || conv.preset || conv.effort);'
    assert src.count(needle) == 1, (
        'conversation-open model call drifted — update the neuter target')
    copy = tmp_path / 'main_neutered_resolver.js'
    copy.write_text(
        src.replace(
            needle,
            '  _applyModelUI(conv.model || conv.preset || conv.effort '
            '|| serverModel);', 1),
        encoding='utf-8')

    output = _run_harness(
        'd_nc', _HARNESS_C,
        os.path.join(JS_DIR, 'main', 'main_toolbar_ui.js'), str(copy))
    assert 'FAIL C1_default_paint_not_persisted' in output, (
        'NEUTER did not bite: pre-resolving the fallback at the call site no '
        'longer defeats the provisional guard — the mechanism may have '
        'changed shape.\n' + output)
    # Explicit choice unaffected: this neuter only breaks provenance for the
    # no-stored-model case.
    assert 'PASS C2_explicit_choice_persisted' in output, output
    assert 'PASS C3_stored_model_not_downgraded' in output, output

    with open(main_js, encoding='utf-8') as f:
        assert f.read() == src, 'harness mutated the shipped main.js'
