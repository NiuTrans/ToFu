/* Real retained sidebar -> loadConversation -> composer adapters, with UI/network ports only. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const {test} = require('node:test');
const base = 'frontend/src/runtime/sections/';
function functions(path, names) {
  const text = fs.readFileSync(base + path, 'utf8');
  const ast = ts.createSourceFile(path, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
  const found = [];
  function visit(node) {
    if (ts.isFunctionDeclaration(node) && names.includes(node.name?.text)) found.push(node.getText(ast));
    ts.forEachChild(node, visit);
  }
  visit(ast);
  assert.equal(found.length, names.length);
  return found.join('\n');
}
function typed(name, directory = 'frontend/src/conversation/application') {
  const module = {exports: {}};
  const source = fs.readFileSync(`${directory}/${name}.ts`, 'utf8');
  new Function('module', 'exports', ts.transpileModule(source, {compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS,
  }}).outputText)(module, module.exports);
  return module.exports;
}
function harness({cold = false, text = '', attachment = false, staleCleanup = false,
  outgoing = null, busy = false, turns = 1} = {}) {
  const input = {value: text};
  const timers = [], writes = [], reconciliations = [];
  const settings = {searchMode: 'multi', fetchEnabled: false, codeExecEnabled: false,
    browserEnabled: false, desktopEnabled: false, memoryEnabled: false,
    schedulerEnabled: false, autopilotEnabled: false, activeFlow: '',
    imageGenEnabled: false, imageGenMode: false, humanGuidanceEnabled: false,
    autoTranslate: false};
  const ctx = {console, Promise, setTimeout: (fn, delay) => timers.push({fn, delay}), Date,
    ...settings, _igSelectedModel: '', _igSelectedProviderId: '', _igSelectedCount: 1,
    _igSelectedAspect: '1:1', _igSelectedResolution: '1K',
    convIsBusy: () => busy,
    persistConversationSettings: conv => writes.push({...conv}),
    reconcileConversationCatalogMetadata: id => reconciliations.push(id),
    config: {model: 'gpt-6-astra'}, activeConvId: null,
    conversations: [{id: 'apk', title: 'APK', model: 'kimi-k3', projectPath: '/ruanjunhao04/apk',
      _turnSnapshotRequired: cold}],
    projectState: {active: true, path: '/ruanjunhao04/tofu'},
    pendingImages: attachment ? [{}] : [], pendingPdfTexts: [], pendingVideos: [],
    _pendingLogClean: staleCleanup ? {originalText: 'old pasted logs'} : null,
    document: {getElementById: id => id === 'userInput' ? input : {textContent: ''},
      querySelectorAll: () => [], querySelector: () => null},
    sessionStorage: {setItem() {}}, runtimeScope: {requestAuthoritativeConversationRender() {},
      ConversationTurnStore: {wakeConversation: async () => {}},
      ConversationTurnRead: {ordered: () => Array.from({length: turns}, () => ({}))}},
    getActiveConv: () => ctx.conversations.find(c => c.id === ctx.activeConvId) || null, _purgeEmptyConvs() {}, _swapActiveConvItem: () => true,
    _conversationDisplayTitle: x => x, t: x => x,
    _resumePendingTranslations() {}, renderPendingQueueUI() {}, _refreshServerQueue() {},
    updateSendButton() {}, debugLog() {}, _updateProjectUI() {},

    renderComposerModelNotice() {},

    composerTranslationStatus: {refresh() {}},
    normalizeConversationInteractionModes: () => ({agentMode: 'standard', activeFlow: ''}),
    convAutoTranslate: () => false, _scheduleReflow() {},
    _applyModelUI: model => {ctx.config.model = model;},
    Api: {project: {setPaths: async paths => ({ok: true, json: async () => ({path: paths[0]})})}},
    ...typed('conversation-model'), ...typed('conversation-project'),
  };
  if (outgoing) {
    ctx.conversations.push({id: 'outgoing', title: 'Outgoing', updatedAt: 123,
      model: 'saved-model', projectPath: '/saved-project', ...settings, ...outgoing});
    ctx.activeConvId = 'outgoing';
  }
  for (const name of ['SearchMode', 'FetchEnabled', 'CodeExec', 'Browser', 'Desktop', 'Memory',
    'AgentMode', 'Flow', 'ImageGenTool', 'ImageGen', 'HumanGuidance', 'AutoTranslate']) ctx[`_apply${name}UI`] = () => {};
  ctx._updateAutoApplyUI = () => {};
  let release;
  ctx.hydrateConversationRuntime = async () => {
    await new Promise(resolve => {release = resolve;});
    ctx.conversations[0]._turnSnapshotRequired = false;
  };
  vm.createContext(ctx);
  vm.runInContext(functions('project_state.js', ['_hasProjectComposerDraft', '_clearProjectStateLocal',
    '_applyProjectData', '_applyRemoteProjectState', '_restoreConvProject']) + `
    const _conversationModelController = createConversationModelController({
      hasDraft: _hasProjectComposerDraft, selection: () => captureComposerModel(config)});
    const _conversationProjectController = createConversationProjectController({
      activeId: () => activeConvId, exists: id => conversations.some(c => c.id === id),
      hasDraft: _hasProjectComposerDraft, selection: () => projectState, clear: _clearProjectStateLocal,
      remote: _applyRemoteProjectState, apply: _applyProjectData,
      setPaths: (...args) => Api.project.setPaths(...args), warn: debugLog});
    ` + functions('main.js', ['restoreConversationSettingsToComposer'])
      + functions('main/main_conv_lifecycle.js', ['loadConversation']), ctx);
  const listeners = new Map();
  typed('sidebar', 'frontend/src/application-shell/interactions').installSidebarInteractions(
    {add() {}, listen: (_host, type, listener) => listeners.set(type, listener)},
    {performance: {now: () => 0}}, {}, {activate: id => ctx.loadConversation(id)},
  );
  return {ctx, input, timers, writes, reconciliations,
    flushTimers() { for (const timer of timers.splice(0)) timer.fn(); }, open() {
    listeners.get('pointerdown')({pointerType: 'mouse', button: 0, target: {
      nodeType: 1,
      closest: selector => selector === '.conv-item' ? {dataset: {convId: 'apk'}} : null,
    }});
  }, release: () => release?.()};
}
const settle = () => new Promise(resolve => setImmediate(resolve));
for (const cold of [false, true]) {
  test(`empty composer restores both saved selections despite stale cleanup (cold=${cold})`, async () => {
    const h = harness({cold, staleCleanup: true});
    h.open(); h.release(); await settle();
    assert.equal(h.ctx.config.model, 'kimi-k3');
    assert.equal(h.ctx.projectState.path, '/ruanjunhao04/apk');
  });
  for (const draft of [{text: 'unsent'}, {attachment: true}]) {
    test(`real sidebar preserves actual draft ${JSON.stringify(draft)} (cold=${cold})`, async () => {
      const h = harness({cold, ...draft});
      h.open(); h.release(); await settle();
      assert.equal(h.ctx.config.model, 'gpt-6-astra');
      assert.equal(h.ctx.projectState.path, '/ruanjunhao04/tofu');
    });
  }
}

for (const cold of [false, true]) {
  test(`sidebar selection uses snapshot/stream queue without another full read (cold=${cold})`, async () => {
    const h = harness({cold});
    let duplicateReads = 0;
    h.ctx._refreshServerQueue = () => { duplicateReads += 1; };
    h.open(); h.release(); await settle();
    assert.equal(duplicateReads, 0);
  });
}

for (const cold of [false, true]) {
  test(`unchanged outgoing settings skip all deferred persistence work (cold=${cold})`, async () => {
    const h = harness({cold, outgoing: {}, text: 'unsent'});
    const outgoing = h.ctx.getActiveConv();
    const before = {...outgoing};
    h.open(); h.release(); await settle();
    assert.deepEqual(outgoing, before);
    assert.equal(h.timers.length, 0);
    assert.deepEqual(h.writes, []);
    assert.deepEqual(h.reconciliations, []);
    assert.equal(h.ctx.activeConvId, 'apk');
    assert.equal(h.ctx.config.model, 'gpt-6-astra');
    assert.equal(h.ctx.projectState.path, '/ruanjunhao04/tofu');
  });
  test(`changed outgoing tools persist only after switch without activity bump (cold=${cold})`, async () => {
    const h = harness({cold, outgoing: {fetchEnabled: true}, attachment: true});
    const outgoing = h.ctx.getActiveConv();
    h.open(); h.release(); await settle();
    assert.equal(outgoing.fetchEnabled, false);
    assert.equal(outgoing.model, 'saved-model');
    assert.equal(outgoing.projectPath, '/saved-project');
    assert.equal(outgoing.updatedAt, 123);
    assert.equal(h.ctx.activeConvId, 'apk');
    assert.equal(h.timers.length, 1);
    assert.equal(h.timers[0].delay, 0);
    assert.deepEqual(h.writes, []);
    h.flushTimers();
    assert.deepEqual(h.reconciliations, [null]);
    assert.equal(h.writes.length, 1);
    assert.equal(h.writes[0].id, 'outgoing');
    assert.equal(h.writes[0].fetchEnabled, false);
    assert.equal(h.writes[0].updatedAt, 123);
  });
}

test('busy outgoing translation remains frozen and does not create a write', () => {
  const h = harness({outgoing: {autoTranslate: true}, busy: true});
  const outgoing = h.ctx.getActiveConv();
  h.open();
  assert.equal(outgoing.autoTranslate, true);
  assert.equal(h.timers.length, 0);
});

test('idle outgoing translation change is persisted', () => {
  const h = harness({outgoing: {autoTranslate: true}});
  h.open(); h.flushTimers();
  assert.equal(h.writes.length, 1);
  assert.equal(h.writes[0].autoTranslate, false);
});

test('disabled image generation preserves saved image settings without a write', () => {
  const h = harness({outgoing: {imageGenModel: 'saved-image', imageGenCount: 4}});
  const outgoing = h.ctx.getActiveConv();
  h.open();
  assert.equal(outgoing.imageGenModel, 'saved-image');
  assert.equal(outgoing.imageGenCount, 4);
  assert.equal(h.timers.length, 0);
});

test('enabled image generation captures changed selection through deferred persistence', () => {
  const h = harness({outgoing: {imageGenMode: true, imageGenModel: 'saved-image'}});
  h.ctx.imageGenMode = true;
  h.ctx._igSelectedModel = 'new-image';
  h.ctx._igSelectedCount = 3;
  h.open();
  assert.deepEqual(h.writes, []);
  h.flushTimers();
  assert.equal(h.writes.length, 1);
  assert.equal(h.writes[0].imageGenModel, 'new-image');
  assert.equal(h.writes[0].imageGenCount, 3);
});

test('changed empty outgoing shell reconciles metadata but does not upload settings', () => {
  const h = harness({outgoing: {desktopEnabled: true}, turns: 0});
  h.open(); h.flushTimers();
  assert.deepEqual(h.reconciliations, [null]);
  assert.deepEqual(h.writes, []);
});
