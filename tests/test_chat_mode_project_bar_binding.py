"""Chat-mode dial ⟺ project-bar strong binding on conversation switch.

Studio IS "a project is attached": the tier dial and the project bar are two
projections of ONE state and must never diverge. They diverged on the switch
path because the two projections were restored by two INDEPENDENT code paths
with different gating (frontend/src/runtime/app-runtime.js):

  * the project bar  — ``_restoreConvProject`` ran UNCONDITIONALLY, and
  * the tier dial    — ``restoreConversationSettingsToComposer`` was wrapped in a
                       ``!hasInput`` gate, so any draft in the input box (it is
                       only cleared on send, so it survives a switch) left the
                       dial painted on the OUTGOING conv's tier.

Net effect (the reported bug): switch Studio→plain with a draft → "Studio dial
+ no project bar". The divergence also survived into persistence: the next
``captureActiveConversationSettings`` laundered ``chatMode:'studio'`` into the project-less
conv's stored settings.

Two sibling holes in ``_restoreConvProject`` made it flaky rather than merely
draft-dependent:

  * the failure/invalid-path branches cleared the bar but never demoted the
    dial (``onProjectCleared`` was not called), and
  * nothing re-checked WHO was active after the ``await Api.project.setPaths``
    round-trip, so a late response painted/persisted onto whichever conv the
    user had switched to by then.

All three are pinned here by driving the REAL extracted functions under node
with a stubbed DOM/Api, plus poisoned-NC controls that resurrect each bug and
prove every positive assertion is load-bearing.

The BOOT twin ``loadProjectStatus`` (page reload/refresh) carried the same
three holes and got the same treatment (owner follow-up ruling): ownership
guards after each await zone — including the background RO re-hydrate
``.then`` — and guarded dial demotion in all three failure exits.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest
from tests._runtime_sections import runtime_section

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))


def _retained_runtime_source() -> str:
    """Compose only the model-readable owners exercised by this harness."""
    return '\n'.join(runtime_section(name, scope_prelude=False) for name in (
        'main.js',
        'main/main_conv_lifecycle.js',
        'main/main_toolbar_ui.js',
        'project_state.js',
    ))

# The post-await stale-switch guard (exact source text, reused by poisons).
_GUARD_LINE = (
    "    if (activeConvId !== conv.id"
    " || !conversations.some(x => x && x.id === conv.id)) return;\n"
)
_CATCH_GUARD = (
    "    if (activeConvId === conv.id"
    " && conversations.some(x => x && x.id === conv.id)\n"
    "        && typeof onProjectCleared === 'function') {"
)


def _brace_match(src: str, open_pos: int) -> int:
    depth = 0
    j = open_pos
    while j < len(src):
        if src[j] == '{':
            depth += 1
        elif src[j] == '}':
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    raise AssertionError('unbalanced braces')


def _extract_fn(src: str, fn_name: str) -> str:
    m = re.search(r'(?:async\s+)?function\s+' + re.escape(fn_name) + r'\s*\(', src)
    assert m, f'{fn_name} not found'
    i = src.find('{', m.end())
    return src[m.start():_brace_match(src, i)]


def _extract_chat_mode_defaults(src: str) -> str:
    m = re.search(r'_CHAT_MODE_DEFAULTS\s*=\s*\{', src)
    assert m, '_CHAT_MODE_DEFAULTS not found'
    brace = m.end() - 1
    return 'var _CHAT_MODE_DEFAULTS = ' + src[brace:_brace_match(src, brace)] + ';'


# ─────────────────────────── harness ───────────────────────────

_PRELUDE = r'''
// ── module-level state the extracted runtime fns read/write ──
var runtimeScope = globalThis;
runtimeScope.requestAuthoritativeConversationRender = function () {};
var conversations = [];
var activeConvId = null;
var chatMode = 'chat';
var projectState = __inactiveProject();
var _projectBarFolders = [];
var pendingImages = [], pendingPdfTexts = [], pendingVideos = [];
var searchMode = 'multi', fetchEnabled = true, codeExecEnabled = true;
var browserEnabled = false, desktopEnabled = false, memoryEnabled = true;
var schedulerEnabled = false;
var autopilotEnabled = false, activeFlow = '', imageGenEnabled = false;
var imageGenMode = false, humanGuidanceEnabled = false, autoTranslate = false;
var planMode = false;

var autoApplyWrites = true;
var _igSelectedModel = null, _igSelectedProviderId = '', _igSelectedCount = 1;
var _igSelectedAspect = '1:1', _igSelectedResolution = '1K';
var config = { model: null, thinkingDepth: null, _modelIsProvisional: true,
               defaultThinkingDepth: null };
var activeStreams = new Map();
var _sendGeneration = 0;
var _editingMsgIdx = null;
var _lastRenderedFingerprint = '';
var __saves = [];
var __setPathsCalls = [];
var __setPathsResult = null;
var __setPathsMode = 'result';
var __statusCalls = [];
var __statusResult = null;
var __statusMode = 'result';
var __deferredResolve = null;
var __deferred = new Promise(function (r) { __deferredResolve = r; });

function mk(id, extra) {
  var c = { id: id, title: id, chatMode: 'chat' };
  for (var k in extra) c[k] = extra[k];
  return c;
}
function __activeProject(path) {
  return { active: true, path: path, fileCount: 1, dirCount: 1, totalSize: 1,
           languages: {}, scanning: false, scanProgress: '', scanDetail: '',
           scannedAt: 1, extraRoots: [] };
}
function __inactiveProject() {
  return { active: false, path: '', fileCount: 0, dirCount: 0, totalSize: 0,
           languages: {}, scanning: false, scanProgress: '', scanDetail: '',
           scannedAt: 0, extraRoots: [] };
}

// ── stub DOM ──
var _els = {};
function _el(id) {
  if (!_els[id]) {
    _els[id] = {
      id: id, style: {}, dataset: {}, _inner: '', textContent: '', value: '',
      set innerHTML(v) { this._inner = String(v); },
      get innerHTML() { return this._inner; },
      classList: {
        _set: {},
        add: function (c) { this._set[c] = 1; },
        remove: function (c) { delete this._set[c]; },
        toggle: function () {},
        contains: function (c) { return !!this._set[c]; },
      },
      setAttribute: function () {}, appendChild: function () {},
      insertBefore: function () {},
    };
  }
  return _els[id];
}
var document = {
  getElementById: function (id) { return _el(id); },
  querySelector: function () { return null; },
  querySelectorAll: function () { return []; },
  addEventListener: function () {},
};
// index.html ships #projectBar with style="display:none" — mirror that, both
// for fidelity and because JSON.stringify drops undefined-valued keys (a
// scenario that never repaints the bar must report 'none', not lose the key).
_el('projectBar').style.display = 'none';

// ── stubs for runtime deps the extracted fns call ──
// Keep the harness output JSON-only even if diagnostics are added later.
console.log = function () {};
function debugLog() {}
function escapeHtml(s) { return String(s == null ? '' : s); }
function t(key) { return key; }
function _conversationDisplayTitle(title, fallback) {
  return String(title || fallback || '');
}
function reconcileConversationCatalogMetadata() {
  __saves.push(Array.from(arguments));
}
function persistConversationSettings() {}
function scheduleConversationSettingsPersist() {}
function getActiveConv() {
  return conversations.find(function (c) { return c.id === activeConvId; }) || null;
}
function _purgeEmptyConvs() {}
function _swapActiveConvItem() { return true; }
function renderConversationList() {}
function renderPendingQueueUI() {}
function _refreshServerQueue() {}
function updateSendButton() {}
function _resumePendingTranslations() {}
function showStreamingUIForConv() {}
async function hydrateConversationRuntime() {}
function _applyRemoteProjectState() {}
function _applyModelUI() {}
function _applySearchModeUI() {}
function _applyFetchEnabledUI() {}
function _applyCodeExecUI() {}
function _applyBrowserUI() {}
function _applyDesktopUI() {}
function _applyMemoryUI() {}
function normalizeConversationInteractionModes() {
  return { agentMode: 'standard', activeFlow: '' };
}
function _applyAgentModeUI(mode) {
  planMode = mode === 'plan';
  autopilotEnabled = mode === 'autopilot';
}
function _applyFlowUI() {}
function _applyImageGenToolUI() {}
function _applyImageGenUI() {}
function _applyHumanGuidanceUI() {}
function _applyAutoTranslateUI() {}

function _updateAutoApplyUI() {}
function convAutoTranslate() { return false; }
function _scheduleReflow() {}
function updateSubmenuCounts() {}
var sessionStorage = { setItem: function () {},
                       getItem: function () { return null; },
                       removeItem: function () {} };
var Api = {
  project: {
    status: async function (convId) {
      __statusCalls.push(convId);
      if (__statusMode === 'deferred') await __deferred;
      if (__statusMode === 'reject') throw new Error('boot network down');
      return __statusResult;
    },
    setPaths: async function (paths, ro, recent) {
      __setPathsCalls.push([
        paths.slice(), ro.slice(), Array.isArray(recent) ? recent.slice() : [],
      ]);
      if (__setPathsMode === 'deferred:result'
          || __setPathsMode === 'deferred:reject') await __deferred;
      if (__setPathsMode === 'reject' || __setPathsMode === 'deferred:reject') {
        throw new Error('cross-DC timeout');
      }
      return __setPathsResult;
    },
  },
};
'''

_WRAPPER = r'''
var __restoredCalls = 0;
(function () {
  var orig = restoreConversationSettingsToComposer;
  restoreConversationSettingsToComposer = function (c) { __restoredCalls++; return orig(c); };
})();
'''

_DRIVERS = {
    # A draft is in the input; switch from a Studio conv (project attached) to
    # a conv with a POISONED stored tier 'studio' and NO project.
    'switch_plain_with_draft': r'''
var convA = mk('convA', { chatMode: 'studio', projectPath: '/repo/chatui',
                          projectPaths: ['/repo/chatui'] });
var convB = mk('convB', { chatMode: 'studio' });
conversations = [convA, convB];
activeConvId = 'convA';
projectState = __activeProject('/repo/chatui');
chatMode = 'studio';
_el('userInput').value = 'half-typed draft';
loadConversation('convB');
var dialAfterSwitch = chatMode;
var barAfterSwitch = _el('projectBar').style.display;
captureActiveConversationSettings();
process.stdout.write(JSON.stringify({
  dial: dialAfterSwitch, bar: barAfterSwitch,
  persisted: convB.chatMode, restored: __restoredCalls,
  setPaths: __setPathsCalls.length,
}));
''',
    # Mirror direction: a stored NON-studio tier WITH a project must heal up.
    'switch_studio_conv_with_draft': r'''
var convA = mk('convA', { chatMode: 'chat' });
var convS = mk('convS', { chatMode: 'chat', projectPath: '/repo/other',
                          projectPaths: ['/repo/other'] });
conversations = [convA, convS];
activeConvId = 'convA';
projectState = __inactiveProject();
chatMode = 'chat';
_el('userInput').value = 'half-typed draft';
__setPathsMode = 'result';
__setPathsResult = { ok: true,
                     json: async function () {
                       return { path: '/repo/other', extraRoots: [] };
                     } };
loadConversation('convS');
var dialImmediate = chatMode;
await new Promise(function (r) { setTimeout(r, 0); });
process.stdout.write(JSON.stringify({
  dialImmediate: dialImmediate, dialSettled: chatMode,
  bar: _el('projectBar').style.display, setPaths: __setPathsCalls,
}));
''',
    'restore_invalid_path': r'''
var convX = mk('convX', { chatMode: 'studio', projectPath: '/repo/gone',
                          projectPaths: ['/repo/gone'] });
conversations = [convX];
activeConvId = 'convX';
projectState = __inactiveProject();
chatMode = 'studio';
__setPathsMode = 'result';
__setPathsResult = { ok: false, json: async function () { return {}; } };
await _restoreConvProject(convX);
process.stdout.write(JSON.stringify({
  dial: chatMode, convChatMode: convX.chatMode,
  convPath: convX.projectPath, bar: _el('projectBar').style.display,
  saves: __saves.length,
}));
''',
    'restore_transient_failure': r'''
var convX = mk('convX', { chatMode: 'studio', projectPath: '/repo/flaky',
                          projectPaths: ['/repo/flaky'] });
conversations = [convX];
activeConvId = 'convX';
projectState = __inactiveProject();
chatMode = 'studio';
__setPathsMode = 'reject';
await _restoreConvProject(convX);
process.stdout.write(JSON.stringify({
  dial: chatMode, convChatMode: convX.chatMode,
  convPath: convX.projectPath, bar: _el('projectBar').style.display,
}));
''',
    # Slow setPaths whose response lands AFTER the user switched away (or the
    # issuing conv was deleted mid-flight). convY is a Studio conv with its OWN
    # project whose restore already ran (the realistic mid-switch window: the
    # dial still shows studio because the incoming conv IS studio) — so a stale
    # demotion persisting onto convY is OBSERVABLE pollution (chatMode 'chat'
    # written into a project-attached conv). __STALE_MODE__ / result kind are
    # substituted by the python side.
    'stale_response': r'''
var convX = mk('convX', { chatMode: 'studio', projectPath: '/repo/x',
                          projectPaths: ['/repo/x'] });
var convY = mk('convY', { chatMode: 'studio', projectPath: '/repo/y',
                          projectPaths: ['/repo/y'] });
conversations = [convX, convY];
activeConvId = 'convX';
projectState = __inactiveProject();
chatMode = 'studio';
__setPathsMode = '__SET_PATHS_MODE__';
__setPathsResult = __RESULT__;
var inflight = _restoreConvProject(convX);
if ('__STALE_MODE__' === 'switched') { activeConvId = 'convY'; }
else { conversations = [convY]; }
__deferredResolve();
await inflight;
await new Promise(function (r) { setTimeout(r, 0); });
process.stdout.write(JSON.stringify({
  dial: chatMode, convXChatMode: convX.chatMode,
  convXPath: convX.projectPath, convYChatMode: convY.chatMode,
  bar: _el('projectBar').style.display,
  projectActive: !!projectState.active,
}));
''',
    # ── The boot twin: loadProjectStatus ──
    'boot_invalid_path': r'''
var convX = mk('convX', { chatMode: 'studio', projectPath: '/repo/gone',
                          projectPaths: ['/repo/gone'] });
conversations = [convX];
activeConvId = 'convX';
projectState = __inactiveProject();
chatMode = 'studio';
__statusMode = 'result';
__statusResult = { path: null };   // server holds nothing → restore branch
__setPathsMode = 'result';
__setPathsResult = { ok: false, json: async function () { return {}; } };
await loadProjectStatus();
process.stdout.write(JSON.stringify({
  dial: chatMode, convChatMode: convX.chatMode,
  convPath: convX.projectPath, bar: _el('projectBar').style.display,
}));
''',
    'boot_stale': r'''
var convX = mk('convX', { chatMode: 'studio', projectPath: '/repo/x',
                          projectPaths: ['/repo/x'] });
var convY = mk('convY', { chatMode: 'studio', projectPath: '/repo/y',
                          projectPaths: ['/repo/y'] });
conversations = [convX, convY];
activeConvId = 'convX';
projectState = __inactiveProject();
chatMode = 'studio';
__statusMode = 'deferred';
__statusResult = { path: '/repo/x' };   // same path → match branch would paint
var inflight = loadProjectStatus();
activeConvId = 'convY';   // user switches away before the boot status lands
__deferredResolve();
await inflight;
await new Promise(function (r) { setTimeout(r, 0); });
process.stdout.write(JSON.stringify({
  dial: chatMode, convXChatMode: convX.chatMode,
  convYChatMode: convY.chatMode, convXPath: convX.projectPath,
  bar: _el('projectBar').style.display,
  projectActive: !!projectState.active,
  setPaths: __setPathsCalls.length,
}));
''',
    'boot_status_failure': r'''
var convX = mk('convX', { chatMode: 'studio', projectPath: '/repo/flaky',
                          projectPaths: ['/repo/flaky'] });
conversations = [convX];
activeConvId = 'convX';
projectState = __inactiveProject();
chatMode = 'studio';
__statusMode = 'reject';
await loadProjectStatus();
process.stdout.write(JSON.stringify({
  dial: chatMode, convChatMode: convX.chatMode,
  convPath: convX.projectPath, bar: _el('projectBar').style.display,
}));
''',
}


def _run(scenario: str, *, poison: str = '',
         stale_mode: str = 'switched', result_kind: str = 'fail') -> dict:
    """Eval the REAL runtime functions under node with stubbed DOM/Api.

    ``poison`` selects a neuter that resurrects one of the original bugs:
      * ``draft_gate``  — re-introduce the !hasInput gate in loadConversation;
      * ``no_demote``   — neuter onProjectCleared (failure branch keeps dial);
      * ``no_guard``    — remove the post-await stale-switch guard;
      * ``no_catch_guard`` — remove the catch branch's own guard.
    """
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available for extraction-and-eval')

    src = _retained_runtime_source()
    fns = {name: _extract_fn(src, name) for name in (
        '_getConvProjectPath', '_isRemotePath',
        '_updateProjectUI', '_clearProjectStateLocal', '_applyProjectData',
        '_restoreConvProject', 'onProjectCleared', '_applyChatModeUI',
        '_deriveChatModeFromFlags', 'restoreConversationSettingsToComposer',
        'captureActiveConversationSettings', 'loadConversation', 'loadProjectStatus',
    )}

    if poison == 'draft_gate':
        old = 'if (!c._turnSnapshotRequired) restoreConversationSettingsToComposer(c);'
        new = ('if (!c._turnSnapshotRequired && !(document.getElementById("userInput")'
               ' && document.getElementById("userInput").value.trim()))'
               ' restoreConversationSettingsToComposer(c);')
        assert old in fns['loadConversation'], 'poison did not apply'
        fns['loadConversation'] = fns['loadConversation'].replace(old, new)
    elif poison == 'no_demote':
        fns['onProjectCleared'] = 'function onProjectCleared() {}'
    elif poison == 'no_guard':
        assert _GUARD_LINE in fns['_restoreConvProject'], 'poison did not apply'
        fns['_restoreConvProject'] = fns['_restoreConvProject'].replace(
            _GUARD_LINE, '')
    elif poison == 'no_boot_guard':
        assert _GUARD_LINE in fns['loadProjectStatus'], 'poison did not apply'
        fns['loadProjectStatus'] = fns['loadProjectStatus'].replace(
            _GUARD_LINE, '', 1)
    elif poison == 'no_catch_guard':
        assert _CATCH_GUARD in fns['_restoreConvProject'], 'poison did not apply'
        fns['_restoreConvProject'] = fns['_restoreConvProject'].replace(
            _CATCH_GUARD,
            "    if (typeof onProjectCleared === 'function') {")

    driver = _DRIVERS[scenario]
    if scenario == 'stale_response':
        if result_kind == 'fail':
            result_js = '{ ok: false, json: async function () { return {}; } }'
            mode = 'deferred:result'
        elif result_kind == 'ok':
            result_js = ('{ ok: true, json: async function () {'
                         ' return { path: "/repo/x", extraRoots: [] }; } }')
            mode = 'deferred:result'
        else:  # 'throw'
            result_js = 'null'
            mode = 'deferred:reject'
        driver = (driver
                  .replace('__SET_PATHS_MODE__', mode)
                  .replace('__RESULT__', result_js)
                  .replace('__STALE_MODE__', stale_mode))

    extracted = '\n'.join([_extract_chat_mode_defaults(src)]
                          + list(fns.values()))
    harness = (_PRELUDE + extracted + _WRAPPER + driver)

    with tempfile.NamedTemporaryFile('w', suffix='.mjs', delete=False) as f:
        f.write(harness)
        tmp = f.name
    try:
        out = subprocess.run([node, tmp], capture_output=True, text=True,
                             timeout=20)
        assert out.returncode == 0, f'node eval failed: {out.stderr}'
        return json.loads(out.stdout)
    finally:
        os.unlink(tmp)


# ────────────────── A. the draft must not gate the tier restore ──────────────────

def test_switch_with_draft_restores_dial_and_bar():
    """THE reported bug: switch Studio→plain with a draft in the input. The
    dial must follow the incoming conv (chat) and the bar must hide — and the
    next captureActiveConversationSettings must persist 'chat', not launder 'studio'."""
    r = _run('switch_plain_with_draft')
    assert r['dial'] == 'chat', 'dial kept the outgoing conv\'s Studio tier'
    assert r['bar'] == 'none', 'project bar visible on a project-less conv'
    assert r['persisted'] == 'chat', 'studio laundered into stored settings'
    assert r['restored'] >= 1, 'tool-state restore never ran on the switch'
    assert r['setPaths'] == 0, 'setPaths must not fire for a project-less conv'


def test_nc_draft_gate_resurrects_divergence():
    """POISONED-NC: re-introduce the !hasInput gate → the dial stays 'studio'
    over a hidden bar. Proves the positive test exercises the real gate."""
    r = _run('switch_plain_with_draft', poison='draft_gate')
    assert r['dial'] == 'studio', 'poison did not resurrect the divergence'
    assert r['bar'] == 'none'
    assert r['persisted'] == 'studio', 'the old gate launders studio on save'


def test_switch_to_studio_conv_with_draft_paints_studio_and_bar():
    """Mirror direction: a conv WITH a project must heal the dial UP to studio
    (immediately, paint-only) and the bar must appear once setPaths lands."""
    r = _run('switch_studio_conv_with_draft')
    assert r['dialImmediate'] == 'studio'
    assert r['dialSettled'] == 'studio'
    assert r['bar'] == 'flex'
    assert r['setPaths'] == [[
        ['/repo/other'], [], ['/repo/other'],
    ]]


def test_nc_draft_gate_mirror_keeps_chat_dial():
    """POISONED-NC (mirror): under the old gate a stored-chat conv with a
    project keeps the plain tier while the bar appears — the inverse
    divergence. Proves the mirror test is load-bearing too."""
    r = _run('switch_studio_conv_with_draft', poison='draft_gate')
    assert r['dialImmediate'] == 'chat', 'poison did not resurrect the mirror'
    assert r['bar'] == 'flex'


# ────────────────── B. failed restore demotes the dial (persisted) ──────────────────

def test_invalid_path_demotes_and_persists():
    """setPaths says the saved path is gone → conv.projectPath cleared, dial
    demoted AND the demotion persisted (conv.chatMode='chat'), bar hidden."""
    r = _run('restore_invalid_path')
    assert r['dial'] == 'chat'
    assert r['convChatMode'] == 'chat', 'demotion not persisted onto the conv'
    assert r['convPath'] == '', 'invalid path must be cleared'
    assert r['bar'] == 'none'
    assert r['saves'] >= 1


def test_nc_neutered_demotion_keeps_studio_dial():
    """POISONED-NC: neuter onProjectCleared → a failed restore leaves the dial
    on Studio over a hidden bar (the pre-fix failure-branch shape)."""
    r = _run('restore_invalid_path', poison='no_demote')
    assert r['dial'] == 'studio', 'poison did not resurrect the failed restore'
    assert r['convChatMode'] == 'studio'


def test_transient_failure_demotes_and_retains_path():
    """A transient setPaths throw demotes the dial (persisted) but RETAINS
    conv.projectPath so the next switch retries the restore."""
    r = _run('restore_transient_failure')
    assert r['dial'] == 'chat'
    assert r['convChatMode'] == 'chat'
    assert r['convPath'] == '/repo/flaky', 'transient failure must keep the path'
    assert r['bar'] == 'none'


# ────────────────── C. stale responses never paint or persist cross-conv ──────────────────

@pytest.mark.parametrize('stale_mode', ['switched', 'deleted'])
@pytest.mark.parametrize('result_kind', ['fail', 'ok'])
def test_stale_response_cannot_demote_persist_or_paint(stale_mode, result_kind):
    """A slow setPaths response landing after a rapid A→B switch (or after the
    issuing conv was deleted) must be a complete no-op: no dial change, no
    persist onto the now-ACTIVE conv (a studio conv with its own project), no
    bar paint, no mutation of the issuing conv."""
    r = _run('stale_response', stale_mode=stale_mode, result_kind=result_kind)
    assert r['dial'] == 'studio', 'stale response demoted the dial'
    assert r['convXChatMode'] == 'studio', 'stale response demoted convX'
    assert r['convXPath'] == '/repo/x', 'stale response mutated convX path'
    assert r['convYChatMode'] == 'studio', 'stale response polluted the active conv'
    assert r['projectActive'] is False, 'stale ok response painted the bar'
    assert r['bar'] == 'none'


@pytest.mark.parametrize('result_kind', ['fail', 'ok'])
def test_nc_missing_guard_pollutes(result_kind):
    """POISONED-NC: remove the post-await guard → the stale failure demotes the
    dial and PERSISTS 'chat' onto the wrong (now-active, project-attached) conv,
    and the stale ok paints the bar. Proves the guard is load-bearing in both
    directions."""
    r = _run('stale_response', poison='no_guard', stale_mode='switched',
             result_kind=result_kind)
    if result_kind == 'fail':
        assert r['dial'] == 'chat', 'expected the stale failure to demote'
        assert r['convYChatMode'] == 'chat', 'expected cross-conv pollution'
        assert r['convXPath'] == '', 'stale failure mutated the issuing conv'
    else:
        assert r['projectActive'] is True, 'expected a stale bar paint'
        assert r['convXPath'] == '/repo/x'


def test_stale_throw_is_guarded():
    """The catch branch carries its OWN guard: a setPaths that throws long
    after a switch must not demote the dial or touch either conv."""
    r = _run('stale_response', result_kind='throw')
    assert r['dial'] == 'studio', 'stale throw demoted the dial'
    assert r['convXChatMode'] == 'studio', 'stale throw demoted convX'
    assert r['convYChatMode'] == 'studio', 'stale throw polluted the active conv'
    assert r['convXPath'] == '/repo/x'


def test_nc_missing_catch_guard_pollutes_via_throw():
    """POISONED-NC: neuter the catch guard → the late throw demotes the dial
    and persists studio→chat onto the WRONG (project-attached) conv."""
    r = _run('stale_response', poison='no_catch_guard', stale_mode='switched',
             result_kind='throw')
    assert r['convYChatMode'] == 'chat', 'expected cross-conv pollution'
    assert r['dial'] == 'chat', 'the throw still demotes the global dial'


# ────────────────── E. the boot twin: loadProjectStatus ──────────────────

def test_boot_invalid_path_demotes_and_persists():
    """Boot/reload twin of the reported bug: the server no longer holds the
    saved project → the invalid-path branch must clear the path, hide the bar,
    demote the dial AND persist the demotion (the pre-fix boot shape left a
    Studio dial painted over a hidden bar after a failed boot restore)."""
    r = _run('boot_invalid_path')
    assert r['dial'] == 'chat'
    assert r['convChatMode'] == 'chat', 'demotion not persisted onto the conv'
    assert r['convPath'] == '', 'invalid path must be cleared'
    assert r['bar'] == 'none'


def test_nc_boot_neutered_demotion_keeps_studio_dial():
    """POISONED-NC: neuter onProjectCleared → the boot failure branch keeps the
    Studio dial over a hidden bar."""
    r = _run('boot_invalid_path', poison='no_demote')
    assert r['dial'] == 'studio', 'poison did not resurrect the boot shape'
    assert r['convChatMode'] == 'studio'


def test_boot_stale_status_is_silent():
    """A boot status response landing after the user switched away must be a
    complete no-op: no bar paint, no persist, no setPaths even fired."""
    r = _run('boot_stale')
    assert r['dial'] == 'studio', 'stale boot status demoted the dial'
    assert r['convXChatMode'] == 'studio', 'stale boot status demoted convX'
    assert r['convYChatMode'] == 'studio', 'stale boot status polluted convY'
    assert r['convXPath'] == '/repo/x'
    assert r['projectActive'] is False, 'stale boot status painted the bar'
    assert r['bar'] == 'none'
    assert r['setPaths'] == 0


def test_nc_boot_missing_guard_paints_stale_bar():
    """POISONED-NC: drop loadProjectStatus's status-zone guard → the stale
    boot response paints the bar for a conv the user already left."""
    r = _run('boot_stale', poison='no_boot_guard')
    assert r['projectActive'] is True, 'expected a stale boot bar paint'


def test_boot_status_failure_demotes_and_retains_path():
    """The status probe itself failing (network down at boot) must demote the
    dial while RETAINING conv.projectPath for the next boot/switch retry."""
    r = _run('boot_status_failure')
    assert r['dial'] == 'chat'
    assert r['convChatMode'] == 'chat'
    assert r['convPath'] == '/repo/flaky', 'transient failure must keep the path'
    assert r['bar'] == 'none'


# ────────────────── D. source invariants (ratchet) ──────────────────

def test_source_pinned_invariants():
    from tests._source_scan import strip_comments

    src = _retained_runtime_source()
    load_conv = _extract_fn(src, 'loadConversation')
    # The draft gate is gone from the switch path's EXECUTABLE code (the fix's
    # own explanatory comment legitimately mentions the old gate); the
    # _turnSnapshotRequired gate stays.
    assert 'hasInput' not in strip_comments(load_conv, lang='js', inline=True)
    assert '_restoreConvProject(c);' in load_conv
    assert re.search(r'if \(!c\._turnSnapshotRequired\) restoreConversationSettingsToComposer\(c\);',
                     load_conv)
    # newChat keeps its own (internally consistent) gate — untouched.
    assert 'hasInput' in _extract_fn(src, 'newChat')
    # _restoreConvProject: guard BEFORE demote, and both failure branches
    # demote through the persisting seam. Ordering is asserted on
    # comment-stripped code — the guard's own comment mentions onProjectCleared.
    restore = _extract_fn(src, '_restoreConvProject')
    assert _GUARD_LINE.strip() in restore
    restore_code = strip_comments(restore, lang='js', inline=True)
    guard_pos = restore_code.index('activeConvId !== conv.id')
    assert restore_code.index('onProjectCleared') > guard_pos, \
        'demotion must come after the stale-switch guard'
    assert restore_code.count('onProjectCleared();') == 2, (
        'both the invalid-path branch and the catch branch must demote')
    # The boot twin loadProjectStatus carries the same contract: both await
    # zones guarded (status + setPaths), the .then re-hydrate guarded, and all
    # THREE failure exits (invalid-path + both catches) demote after the
    # guard, with the path retained in the catch branches.
    boot = _extract_fn(src, 'loadProjectStatus')
    assert 'activeConvId !== conv.id' in boot
    boot_code = strip_comments(boot, lang='js', inline=True)
    boot_guard_pos = boot_code.index('activeConvId !== conv.id')
    assert boot_code.index('onProjectCleared') > boot_guard_pos, (
        'boot demotion must come after the ownership guard')
    assert boot_code.count('onProjectCleared();') == 3, (
        'invalid-path + both catches must demote in loadProjectStatus')
    assert boot_code.count('activeConvId !== conv.id') >= 2, (
        'both await zones (status + setPaths) must be guarded')
    assert boot_code.count('activeConvId === conv.id') >= 3, (
        'the .then re-hydrate + both catch guards must be guarded')


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
