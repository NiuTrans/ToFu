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
function typed(name) {
  const module = {exports: {}};
  const source = fs.readFileSync(`frontend/src/conversation/application/${name}.ts`, 'utf8');
  new Function('module', 'exports', ts.transpileModule(source, {compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS,
  }}).outputText)(module, module.exports);
  return module.exports;
}
function harness({cold = false, text = '', attachment = false, staleCleanup = false} = {}) {
  const input = {value: text};
  const ctx = {console, Promise, setTimeout: () => {}, Date,
    config: {model: 'gpt-6-astra'}, activeConvId: null,
    conversations: [{id: 'apk', title: 'APK', model: 'kimi-k3', projectPath: '/ruanjunhao04/apk',
      _turnSnapshotRequired: cold}],
    projectState: {active: true, path: '/ruanjunhao04/tofu'},
    pendingImages: attachment ? [{}] : [], pendingPdfTexts: [], pendingVideos: [],
    _pendingLogClean: staleCleanup ? {originalText: 'old pasted logs'} : null,
    document: {getElementById: id => id === 'userInput' ? input : {textContent: ''},
      querySelectorAll: () => [], querySelector: () => null},
    sessionStorage: {setItem() {}}, runtimeScope: {requestAuthoritativeConversationRender() {},
      ConversationTurnStore: {wakeConversation: async () => {}}},
    getActiveConv: () => null, _purgeEmptyConvs() {}, _swapActiveConvItem: () => true,
    _conversationDisplayTitle: x => x, t: x => x,
    _resumePendingTranslations() {}, renderPendingQueueUI() {}, _refreshServerQueue() {},
    updateSendButton() {}, debugLog() {}, _updateProjectUI() {},
    normalizeConversationInteractionModes: () => ({agentMode: 'standard', activeFlow: ''}),
    convAutoTranslate: () => false, _scheduleReflow() {},
    _applyModelUI: model => {ctx.config.model = model;},
    Api: {project: {setPaths: async paths => ({ok: true, json: async () => ({path: paths[0]})})}},
    ...typed('conversation-model'), ...typed('conversation-project'),
  };
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
    let _lastConvPointerActivation = {id: null, t: 0};
    ` + functions('main.js', ['restoreConversationSettingsToComposer', '_handleConvPointerDown'])
      + functions('main/main_conv_lifecycle.js', ['loadConversation']), ctx);
  return {ctx, input, open() {
    ctx._handleConvPointerDown({pointerType: 'mouse', button: 0, target: {
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
