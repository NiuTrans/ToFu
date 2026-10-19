"""Retained composer/project adapters exercise the real typed restore owner.

Navigation restores destination project bindings locally, including with a
text draft. Only explicit selection/clear changes saved context; send captures
the destination binding regardless of ambient bar state. Race policy itself lives in the sibling
conversation-project owner tests; these tests pin the retained wiring.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from tests._runtime_sections import _require_frontend_bundler, runtime_section

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def _brace_match(src: str, open_pos: int) -> int:
    depth = 0
    for index in range(open_pos, len(src)):
        if src[index] == '{':
            depth += 1
        elif src[index] == '}':
            depth -= 1
            if depth == 0:
                return index + 1
    raise AssertionError('unbalanced braces')


def _extract_fn(src: str, name: str) -> str:
    match = re.search(r'(?:async\s+)?function\s+' + re.escape(name) + r'\s*\(', src)
    assert match, f'{name} not found'
    brace = src.index('{', match.end())
    return src[match.start():_brace_match(src, brace)]


_PRELUDE = r'''
const assert = require('node:assert/strict');
const ts = require('typescript'), fs = require('node:fs');
const owner = {exports: {}};
new Function('module', 'exports', ts.transpileModule(fs.readFileSync(
  'frontend/src/conversation/application/conversation-project.ts', 'utf8'),
  {compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS}}
).outputText)(owner, owner.exports);
const {createConversationProjectController, projectStatusMatchesWorkspace, noteConversationProjectChoice, captureConversationProject, captureProjectStatus, withProjectHydration} = owner.exports;
const attachmentOwner = {exports: {}};
new Function('exports', ts.transpileModule(fs.readFileSync(
  'frontend/src/conversation/ui/optional-project-attachment.ts', 'utf8'),
  {compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS}}
).outputText)(attachmentOwner.exports);
const {paintOptionalProjectAttachment} = attachmentOwner.exports;
const modelOwner = {exports: {}};
new Function('module', 'exports', ts.transpileModule(fs.readFileSync(
  'frontend/src/conversation/application/conversation-model.ts', 'utf8'),
  {compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS}}
).outputText)(modelOwner, modelOwner.exports);
const {createConversationModelController, captureComposerModel, commitComposerModel} = modelOwner.exports;
const _conversationModelController = createConversationModelController({
  hasDraft: () => _hasProjectComposerDraft(), selection: () => captureComposerModel(config),
});
var runtimeScope = globalThis;
var composerTranslationStatus = {refresh() {}};
function renderComposerModelNotice() {}
var conversations = [], activeConvId = null, chatMode = 'chat';
var pendingImages = [], pendingPdfTexts = [], pendingVideos = [], _pendingLogClean = null;
var searchMode = 'multi', fetchEnabled = true, codeExecEnabled = true;
var browserEnabled = false, desktopEnabled = false, memoryEnabled = true;
var schedulerEnabled = false, autopilotEnabled = false, activeFlow = '';
var imageGenEnabled = false, imageGenMode = false, humanGuidanceEnabled = false;
var autoTranslate = false, planMode = false, autoApplyWrites = true;
var _igSelectedModel = null, _igSelectedProviderId = '', _igSelectedCount = 1;
var _igSelectedAspect = '1:1', _igSelectedResolution = '1K';
var config = {model: null, thinkingDepth: null, _modelIsProvisional: true};
var serverModel = null, thinkingEnabled = false;
var projectState = {active: false, path: '', extraRoots: []};
var calls = [], saves = [], warnings = [], clears = 0;
var request = async paths => ({ok: true, json: async () => ({path: paths[0]})});
var Api = {project: {
  setPaths: async (...args) => {calls.push(args); return request(...args);},
  clear: async () => {clears++;},
}};
const {JSDOM} = require('jsdom');
const dom = new JSDOM(fs.readFileSync('index.html', 'utf8'));
var document = dom.window.document;
function el(id) {
  let node = document.getElementById(id);
  if (!node) { node = document.createElement('div'); node.id = id; document.body.append(node); }
  return node;
}
function loadBar(name) {
  const exports = {};
  const source = ts.transpileModule(fs.readFileSync('frontend/src/features/conversation-bar/'+name+'.ts','utf8'),
    {compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS}}).outputText;
  new Function('exports','require',source)(exports,id=>loadBar(id.replace('./','')));
  return exports;
}
const {createProjectContext} = loadBar('project-context');
var sessionStorage = {setItem() {}, removeItem() {}};
function debugLog(message, level) {if (level === 'warn') warnings.push(message);}
function escapeHtml(value) {return String(value ?? '');}
function t(key) {return key;}
function getActiveConv() {return conversations.find(c => c.id === activeConvId) || null;}
function reconcileConversationCatalogMetadata() {}
function persistConversationSettings(c) {saves.push(c.id);}
function scheduleConversationSettingsPersist(c) {saves.push(c.id);}
function _conversationDisplayTitle(title) {return title;}
function _purgeEmptyConvs() {}
function _swapActiveConvItem() {return true;}
function renderConversationList() {}
function renderPendingQueueUI() {}
function _refreshServerQueue() {}
function updateSendButton() {}
function _resumePendingTranslations() {}
var hydrateConversationRuntime = async () => {};
runtimeScope.requestAuthoritativeConversationRender = () => {};
runtimeScope.ConversationTurnStore = {wakeConversation: async () => {}};
function _applyModelUI() {}
function _applySearchModeUI(value) {searchMode = value;}
function _applyFetchEnabledUI(value) {fetchEnabled = value;}
function _applyCodeExecUI(value) {codeExecEnabled = value;}
function _applyBrowserUI(value) {browserEnabled = value;}
function _applyDesktopUI(value) {desktopEnabled = value;}
function _applyMemoryUI(value) {memoryEnabled = value;}
function normalizeConversationInteractionModes() {return {agentMode: 'standard', activeFlow: ''};}
function _applyAgentModeUI() {}
function _applyFlowUI() {}
function _applyImageGenToolUI() {}
function _applyImageGenUI() {}
function _applyHumanGuidanceUI() {}
function _applyAutoTranslateUI() {}
function convAutoTranslate() {return false;}
function updateSubmenuCounts() {}

function _scheduleReflow() {}
function binding(c) {return {path: c.projectPath, paths: c.projectPaths, ro: c.readOnlyPaths};}
function saved(id, path = '/saved') {
  return {id, title: id, chatMode: 'studio', projectPath: path,
    projectPaths: [path, '/extra'], readOnlyPaths: [path, '/extra']};
}
function setup(target) {
  conversations = [saved('outgoing', '/outgoing'), target];
  activeConvId = 'outgoing';
  projectState = {active: true, path: '/shown', readOnly: true,
    extraRoots: [{path: '/shown-extra', readOnly: true}]};
  _updateProjectUI();
  _applyChatModeUI('studio');
}
const flush = () => new Promise(resolve => setTimeout(resolve, 0));
function defer() {let resolve; const promise = new Promise(r => resolve = r); return {promise, resolve};}
'''


def _run(driver: str) -> None:
    # The harness script does require('typescript'): node alone is not enough —
    # honor the shared dep guard (skips on dep-less lanes, fails under
    # TOFU_REQUIRE_FRONTEND=1) instead of this file's former node-only check.
    _require_frontend_bundler()
    node = shutil.which('node')
    if not node:
        pytest.skip('node unavailable')
    src = '\n'.join(runtime_section(name, scope_prelude=False) for name in (
        'main.js', 'main/main_conv_lifecycle.js', 'main/main_toolbar_ui.js',
        'main/main_send_pipeline.js',
    ))
    match = re.search(r'_CHAT_MODE_DEFAULTS\s*=\s*\{', src)
    assert match
    brace = match.end() - 1
    defaults = 'const _CHAT_MODE_DEFAULTS = ' + src[brace:_brace_match(src, brace)] + ';'
    functions = '\n'.join(_extract_fn(src, name) for name in (
        'loadConversation', 'captureActiveConversationSettings',
        'restoreConversationSettingsToComposer', '_applyChatModeUI',
        '_deriveChatModeFromFlags', 'onProjectCleared', '_submitComposerDraft',
        '_buildToolbarOverrides',
    ))
    # Run the complete retained project adapter, including its production
    # constructor ports, rather than recreating a parallel controller wiring.
    script = (_PRELUDE + defaults + functions
              + runtime_section('project_state.js', scope_prelude=False)
              + '\n(async () => {\n' + driver
              + '\n})().catch(error => {console.error(error); process.exitCode = 1;});')
    result = subprocess.run([node, '-e', script], cwd=ROOT, capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('has_project', [False, True])
def test_empty_composer_switch_restores_saved_project_and_dial(has_project):
    _run(r'''
const target = HAS_PROJECT ? saved('target') : {id: 'target', chatMode: 'studio'};
setup(target);
const before = JSON.stringify(binding(target));
loadConversation('target');
await flush();
assert.equal(projectState.path, HAS_PROJECT ? '/saved' : '');
assert.equal(el('projectBar').dataset.active, String(HAS_PROJECT));
assert.equal(chatMode, 'chat');
assert.equal(el('chatModeLabel').textContent, 'conversationBar.attachProject');
assert.equal(JSON.stringify(binding(target)), before);
assert.deepEqual(calls, []);
if (HAS_PROJECT) assert.deepEqual(_projectBarFolders, [
  {path: '/saved', readOnly: true}, {path: '/extra', readOnly: true}]);
'''.replace('HAS_PROJECT', json.dumps(has_project)))


@pytest.mark.parametrize('draft', ['text', 'image', 'pdf', 'video'])
@pytest.mark.parametrize('has_project', [False, True])
def test_draft_switch_restores_destination_without_rebinding(draft, has_project):
    drafts = {
        'text': "el('userInput').value = 'unsent draft';",
        'image': "pendingImages = [{id: 'image'}];",
        'pdf': "pendingPdfTexts = [{text: 'document'}];",
        'video': "pendingVideos = [{id: 'video'}];",
    }
    _run(r'''
const target = HAS_PROJECT ? saved('target') : {id: 'target', chatMode: 'chat'};
setup(target);
DRAFT
const shown = projectState, before = JSON.stringify(binding(target));
loadConversation('target');
await flush();
assert.equal(projectState.path, HAS_PROJECT ? '/saved' : '');
assert.equal(el('projectBar').dataset.active, String(HAS_PROJECT));
assert.equal(calls.length, 0);
captureActiveConversationSettings();
assert.equal(JSON.stringify(binding(target)), before);
assert.equal(conversations[0].projectPath, '/outgoing');
'''.replace('HAS_PROJECT', json.dumps(has_project)).replace('DRAFT', drafts[draft]))


def test_stale_log_cleanup_suggestion_does_not_block_project_restore():
    _run(r'''
const target = saved('target'); setup(target);
_pendingLogClean = {originalText:'old paste'};
loadConversation('target'); await flush();
assert.equal(projectState.path, '/saved');
assert.equal(target.projectPath, '/saved');
assert.equal(calls.length, 0);
''')


@pytest.mark.parametrize('draft', [False, True])
def test_cold_open_waits_for_authoritative_hydration(draft):
    _run(r'''
const target = {...saved('target', '/stale-shell'), _turnSnapshotRequired: true};
setup(target);
if (DRAFT) el('userInput').value = 'keep this draft';
const shown = projectState, gate = defer();
hydrateConversationRuntime = async id => {
  assert.equal(id, 'target'); await gate.promise;
  Object.assign(target, saved('target', '/hydrated'), {_turnSnapshotRequired: false});
};
loadConversation('target');
await loadProjectStatus();
assert.equal(calls.length, 0);
assert.equal(el('projectBar').dataset.active, 'true');
assert.equal(projectState.path, '/stale-shell');
gate.resolve(); await flush();
assert.equal(projectState.path, '/hydrated');
assert.equal(calls.length, 0);
assert.equal(target.projectPath, '/hydrated');
'''.replace('DRAFT', json.dumps(draft)))


@pytest.mark.parametrize('change', ['draft', 'selection', 'switch'])
def test_cold_hydration_cannot_overwrite_new_composer_intent(change):
    actions = {
        'draft': "el('userInput').value = 'new draft';",
        'selection': "_saveConvProjectPath('/chosen', [], []);",
        'switch': "loadConversation('outgoing'); await flush();",
    }
    _run(r'''
const target = {...saved('target'), _turnSnapshotRequired: true};
setup(target);
const gate = defer();
hydrateConversationRuntime = async () => {await gate.promise; target._turnSnapshotRequired = false;};
loadConversation('target');
ACTION
const shown = projectState, count = calls.length;
gate.resolve(); await flush();
assert.deepEqual(projectState, shown);
assert.equal(calls.length, count);
assert.equal(target.projectPath, 'CHANGE' === 'selection' ? '/chosen' : '/saved');
'''.replace('ACTION', actions[change]).replace('CHANGE', change))


@pytest.mark.parametrize('failure', ['invalid', 'offline'])
@pytest.mark.parametrize('entry', ['restore', 'boot'])
def test_local_restore_is_independent_of_server_availability(failure, entry):
    _run(r'''
const target = saved('target'); setup(target); activeConvId = target.id;
const before = JSON.stringify(target);
request = async () => { FAILURE };
await ENTRY;
assert.equal(JSON.stringify(target), before);
assert.equal(projectState.path, '/saved');
assert.equal(el('projectBar').dataset.active, 'true');
assert.equal(warnings.length, 0);
assert.equal(calls.length, 0);
assert.equal(saves.length, 0);
'''.replace('FAILURE', "throw Error('offline');" if failure == 'offline'
            else 'return {ok: false, json: async () => ({})};')
         .replace('ENTRY', 'loadProjectStatus()' if entry == 'boot' else '_restoreConvProject(target)'))


def test_explicit_clear_removes_saved_paths_and_demotes_dial():
    _run(r'''
const target = saved('target'); setup(target); activeConvId = target.id;
await clearProject();
assert.deepEqual(binding(target), {path: '', paths: [], ro: []});
assert.equal(projectState.active, false);
assert.equal(el('projectBar').dataset.active, 'false');
assert.equal(el('projectBarFolders').textContent.trim(), 'conversationBar.attachProject');
assert.equal(chatMode, 'chat');
assert.equal(clears, 0);
assert.ok(saves.includes('target'));
''')


def test_generic_settings_capture_keeps_binding_and_nonprovisional_settings():
    _run(r'''
const target = saved('target'); setup(target); activeConvId = target.id;
const before = JSON.stringify(binding(target));
config.model = 'chosen-model'; config._modelIsProvisional = false;
config.thinkingDepth = 'high'; browserEnabled = true;
captureActiveConversationSettings();
assert.equal(JSON.stringify(binding(target)), before);
assert.equal(target.model, undefined);
assert.equal(target.thinkingDepth, undefined);
assert.equal(target.browserEnabled, true);
assert.deepEqual(saves, ['target']);
''')


@pytest.mark.parametrize('shown', [False, True])
def test_send_captures_destination_binding_despite_ambient_project_bar(shown):
    _run(r'''
const target = saved('target'); setup(target); activeConvId = target.id;
if (!SHOWN) {projectState = {active:false,path:'',extraRoots:[]}; _updateProjectUI();}
el('userInput').value = 'send this';
const expected = binding(target);
// Stop at the send-startup port: the real submission adapter must commit
// context before a lease/transport can observe the conversation. No model or
// optimistic transcript dependencies are involved in this project test.
const boundary = Error('observed send startup');
globalThis._composerCommandId = () => 'test-command';
globalThis.createSendStartupLease = conv => {
  assert.equal(conv, target);
  assert.deepEqual(binding(conv), expected);
  throw boundary;
};
await assert.rejects(_submitComposerDraft(), error => error === boundary);
assert.deepEqual(binding(target), expected);
'''.replace('SHOWN', json.dumps(shown)))


def test_text_draft_cannot_clear_destination_project_on_send():
    _run(r'''
const target = saved('target'); setup(target);
projectState = {active:false,path:'',extraRoots:[]}; _updateProjectUI();
el('userInput').value = 'projectless draft';
loadConversation(target.id);
const boundary = Error('observed send startup');
globalThis._composerCommandId = () => 'test-command';
globalThis.createSendStartupLease = conv => {
  assert.deepEqual(binding(conv), binding(target));
  assert.equal(conv.projectPath, '/saved');
  throw boundary;
};
await assert.rejects(_submitComposerDraft(), error => error === boundary);
''')


def test_remote_restore_preserves_extra_roots_and_read_only():
    _run(r'''
const target = saved('target'); setup(target); activeConvId = target.id;
target.projectPath = 'remote:device:/root';
target.projectPaths = [target.projectPath, 'remote:device:/extra'];
target.readOnlyPaths = [...target.projectPaths];
await _restoreConvProject(target);
assert.equal(projectState.path, target.projectPath);
assert.equal(projectState.readOnly, true);
assert.deepEqual(projectState.extraRoots, [{path: 'remote:device:/extra', readOnly: true}]);
assert.equal(calls.length, 0);
''')


@pytest.mark.parametrize('draft', [False, True])
def test_model_preset_follows_conversation_unless_draft(draft):
    _run(r'''
const target = saved('target'); target.preset = 'preset-model';
target.modelRef = {creator_id: 'target', model_id: 'preset-model'};
target.preferredProviderId = 'target-provider'; target.thinkingDepth = 'high';
setup(target); config.model = 'draft-model'; config.modelRef = {model_id:'draft-model'};
config.preferredProviderId = 'draft-provider'; config.thinkingDepth = 'low';
config._modelIsProvisional = false;
_applyModelUI = model => {config.model = model;};
if (DRAFT) el('userInput').value = 'draft';
loadConversation('target');
assert.equal(config.model, DRAFT ? 'draft-model' : 'preset-model');
assert.equal(config.preferredProviderId, DRAFT ? 'draft-provider' : 'target-provider');
assert.equal(config.thinkingDepth, DRAFT ? 'low' : 'high');
assert.equal(conversations[0].model, undefined);
assert.equal(target.model, undefined);
'''.replace('DRAFT', json.dumps(draft)))


@pytest.mark.parametrize('provisional', [False, True])
def test_build_toolbar_overrides_carries_provider_chip_pick(provisional):
    # A fresh provider-chip pick lives on `config` (preferredProviderId/modelRef)
    # and commitComposerModel skips an in-flight conv, so the conv's stored
    # preference stays blank. _buildToolbarOverrides must forward the live pick
    # into the submitted overrides, or the backend resolver falls back to the
    # blank conv value and auto-routes away from the provider the user pinned
    # (the "catpaw selected but dispatched to sankuai" bug).
    _run(r'''
const target = saved('target'); setup(target); activeConvId = target.id;
serverModel = 'kimi-k3';
config.model = 'kimi-k3';
config._modelIsProvisional = PROVISIONAL;
config.preferredProviderId = 'catpaw-dep';
config.modelRef = {deploymentId: 'dep-1', modelId: 'kimi-k3', providerId: 'catpaw-dep'};
config.routing = {preferred_provider_id: 'catpaw-dep'};
const ov = _buildToolbarOverrides();
if (PROVISIONAL) {
  // A never-picked provisional model must not launder a preference.
  assert.equal(ov.preferredProviderId, '');
  assert.equal(ov.modelRef, null);
  assert.deepEqual(ov.routing, {});
} else {
  assert.equal(ov.preferredProviderId, 'catpaw-dep');
  assert.deepEqual(ov.modelRef,
    {deploymentId: 'dep-1', modelId: 'kimi-k3', providerId: 'catpaw-dep'});
  assert.deepEqual(ov.routing, {preferred_provider_id: 'catpaw-dep'});
  // modelRef/routing are copies, not live references into config.
  assert.notEqual(ov.modelRef, config.modelRef);
}
'''.replace('PROVISIONAL', json.dumps(provisional)))

def test_model_selection_during_cold_hydration_wins():
    _run(r'''
const target = saved('target'); target.model = 'stored'; target._turnSnapshotRequired = true;
setup(target); const gate = defer(); hydrateConversationRuntime = () => gate.promise;
_applyModelUI = model => {config.model = model;};
loadConversation('target'); config.model = 'new-selection'; config._modelIsProvisional = false;
gate.resolve(); await flush(); assert.equal(config.model, 'new-selection');
''')


@pytest.mark.parametrize('action', ['toggle', 'clear'])
@pytest.mark.parametrize('navigate', ['other', 'roundtrip'])
def test_project_command_completion_cannot_overwrite_new_selection(action, navigate):
    _run(r'''
const target = saved('target'); setup(target);
const origin = getActiveConv();
const gate = defer();
if ('ACTION' === 'toggle') request = () => gate.promise;
else Api.project.clear = () => gate.promise;
const pending = 'ACTION' === 'toggle' ? toggleProjectBarReadOnly(0) : clearProject();
activeConvId = target.id;
_saveConvProjectPath('/chosen', [], []);
_conversationProjectController.begin();
if ('NAVIGATE' === 'roundtrip') activeConvId = origin.id;
const chosen = projectState;
const before = JSON.stringify(conversations), savedCount = saves.length;
gate.resolve({ok:true,json:async()=>({path:'/stale',extraRoots:[],readOnly:true})});
await pending;
assert.equal(projectState, chosen);
assert.equal(JSON.stringify(conversations), before);
assert.equal(saves.length,savedCount);
'''.replace('ACTION', action).replace('NAVIGATE', navigate))


def test_project_permission_settles_from_server_and_rolls_back_current_failure():
    _run(r'''
const target = saved('target'); setup(target);
request = async () => ({ok:true,json:async()=>({path:'/shown',readOnly:false,
 extraRoots:[{path:'/shown-extra',readOnly:true}]})});
await toggleProjectBarReadOnly(0);
assert.equal(projectState.readOnly,false);
assert.deepEqual(getActiveConv().readOnlyPaths,['/shown-extra']);
const before = JSON.stringify(captureProjectStatus(projectState));
request = async () => {throw Error('offline');};
await toggleProjectBarReadOnly(0);
assert.equal(JSON.stringify(captureProjectStatus(projectState)),before);
''')
