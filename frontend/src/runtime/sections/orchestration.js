/* ===== migrated source: orchestration.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration.js — Orchestration Studio (frontend authoring canvas)
   A visual, drag-and-drop builder where users compose "orchestration
   definitions" — iterative loops, fan-out/synthesize flows, etc. —
   by wiring together ROLE agents (tofu mascots) and CONTROL nodes
   (start / loop / parallel / barrier / route / stop).
   ── Scope (this phase) ──────────────────────────────────────────────
   This is the AUTHORING layer only.  It produces a declarative
   definition object (see _orchToDefinition) that a backend engine will
   later interpret.  Per CLAUDE.md §3.2.0 the frontend stays a thin
   renderer/editor: it emits JSON, it does NOT run orchestration logic.
   Definitions are persisted through `/api/v1/orchestrations`; validation,
   built-in graphs, role schemas, layout and execution semantics are all
   owned by the backend service boundary.
   Catalogues, templates, defaults and layout are backend-owned or external
   assets; this composition module coordinates canvas state and Inspector
   controllers. Stable shell, Canvas and presentation adapters live in the
   ordered orchestration-*-bridge.js siblings; palette, node cards,
   document/run/event lifecycle and schema/I/O rendering have focused owners.
   ═══════════════════════════════════════════════════════════════════ */
// ── Editor state ────────────────────────────────────────────────────
// The state controller is authoritative. Accessor-backed legacy globals keep
// old extensions and diagnostic harnesses on that same state, never a copy.
// Typed orchestration owners intentionally consume this finite compatibility
// port while the retained controller composition is migrated. ESM module
// bindings do not become properties of ``window`` (classic ``var`` did), so
// publish the declared dependencies explicitly before constructing any typed
// controller. Keep this list aligned with the *Window contracts in
// frontend/src/features/orchestration.
Object.assign(orchestrationRegistry, {
  ORCHESTRATION_AUTHORING_VALIDATION_METADATA,
  ORCHESTRATION_REQUEST_LIMIT_FIELDS,
  ORCHESTRATION_RUNTIME_CONTRACT_SECTIONS,
  _ORCH_CONTROLS,
  _ORCH_GLYPHS,
  _ORCH_ICONS,
  _ORCH_ROLES,
  _orchIconSrc,
  _validateNodeRuntimeDefaultsAuthoringSection,
  createOrchestrationBreadcrumbView,
  createOrchestrationGraphActionContext,
  createOrchestrationGraphMutationActions,
  createOrchestrationGraphTopology,
  createOrchestrationGraphWorkspace,
  createOrchestrationNavigationController,
  escapeHtml,
  orchestrationConnections,
  orchestrationNodePosition,
  projectOrchestrationLayoutPositions,
  showConfirm,
  showToast,
  t,
});
var _orchEditorState = createOrchestrationEditorState();
_orchEditorState.installLegacyGlobals(window);
var _orchCanvasInteraction = null;  // transient drag/connect controller
var _orchCanvasView = null;   // ordered canvas DOM composition
var _orchSession = null;      // active definition id/version + adoption policy
var _orchEditLifecycle = null; // document/history/save-checkpoint boundary
var _orchIssueNavigator = null; // backend diagnostic list + field navigation

// The frozen, late-bound browser/API/presentation service port is created by
// orchestration-studio-services.js before this controller graph is composed.
var _orchRequestLimits = createOrchestrationRequestLimits({
  source: function () {
    return _orchAuthoring ? _orchAuthoring.requestLimits() : {};
  },
});
var _orchRuntimeContracts = createOrchestrationRuntimeContractPort({
  source: function () {
    return _orchAuthoring ? _orchAuthoring.snapshot() : {};
  },
});
var _orchFeedback = createOrchestrationFeedback({
  document: _orchServices.document,
  translate: _orchServices.translate,
  issueMessages: orchestrationIssueMessages,
});
var _orchExporter = createOrchestrationExportController({
  document: _orchServices.document,
  snapshot: function () { return _orchRootDefinitionSnapshot(); },
  translate: _orchServices.translate,
  toast: _orchServices.toast,
  onError: _orchServices.reporter('OrchestrationExport'),
});
var _orchPopupMenus = createOrchestrationPopupMenuController({
  document: _orchServices.document,
});
var _orchWriteRecovery = createOrchestrationWriteRecoveryController({
  currentId: function () {
    return _orchSession ? _orchSession.currentId() : null;
  },
  isCurrent: function (conflict) {
    var current = _orchDocument && _orchDocument.state.writeConflict;
    return !!current
      && current.expectedUpdatedAt === conflict.expectedUpdatedAt
      && current.currentUpdatedAt === conflict.currentUpdatedAt;
  },
  choose: _orchServices.choose,
  exportDraft: function () { return _orchExport(); },
  loadLatest: function (id) {
    return _orchWorkspaceController
      ? _orchWorkspaceController.loadFromStore(id, { skipConfirm: true })
      : Promise.resolve(null);
  },
  translate: _orchServices.translate,
});
// Document lifecycle lives in orchestration-document.js. The canvas supplies
// only adapters, so dirty/validation/save semantics can be tested and evolved
// without loading or reaching into the graph editor itself.
var _orchDocument = createOrchestrationDocumentController({
  document: _orchServices.document,
  normalizeInspection: normalizeOrchestrationInspection,
  normalizeValidationRead: normalizeOrchestrationValidationRead,
  api: _orchServices.api, inspectionContract: function () { return _orchAuthoring ? _orchAuthoring.inspectionContract() : null; },
  snapshot: function () { return _orchRootDefinitionSnapshot(); },
  nodeCount: _orchEditorState.nodeCount,
  translate: _orchServices.translate,
  toast: _orchServices.toast,
  warn: _orchServices.warn,
  showIssues: function (state) {
    return _orchIssueNavigator ? _orchIssueNavigator.show(state) : null;
  },
  syncIssues: function (state) {
    if (_orchIssueNavigator) _orchIssueNavigator.sync(state);
  },
  onInspectionChange: function () {
    if (_orchDiagnosticIndex) _orchDiagnosticIndex.invalidate();
    if (_orchCanvasView) {
      _orchCanvasView.renderNodes();
      _orchCanvasView.renderEdges();
    }
  },
  confirm: _orchServices.confirm,
  onWriteConflict: function (conflict) {
    return _orchWriteRecovery.open(conflict);
  },
  onError: _orchServices.reporter('OrchestrationDocument'),
});
// Compatibility alias for older extensions/tests that only read lifecycle
// state. All mutations in shipped code go through _orchDocument methods.
var _orchDocState = _orchDocument.state;
// Run Drawer lifecycle lives in orchestration-run.js. The editor supplies
// graph snapshots and small canvas callbacks; transport/polling/gates remain
// isolated from authoring state.
var _orchTaskModeHandoff = createOrchestrationSurfaceHandoff({
  closeSource: function () { return closeOrchestration(null, true); },
  openTarget: function (runId) {
    return typeof openTaskMode === 'function' ? openTaskMode(runId) : false;
  },
  closeTarget: function () {
    return typeof closeTaskMode === 'function' ? closeTaskMode() : false;
  },
  reopenSource: function () { return openOrchestration(); },
  report: _orchServices.reporter('OrchestrationSurfaceHandoff'),
});
var _orchRunOverlay = createOrchestrationRunOverlay({
  document: _orchServices.document,
  definition: function () { return _orchRootDefinitionSnapshot(); },
  selectedNodeId: _orchEditorState.selectedNodeId,
  renderInspector: function () { _orchRenderInspector(); },
});
function _orchSyncMobileSurfaceState() { if (_orchStudio) _orchStudio.syncMobileSheets(); }
function _orchHandleRunSurfaceState() { if (_orchPanelLayout) _orchPanelLayout.setRunDrawerOpen(); _orchSyncMobileSurfaceState(); }
var _orchRunController = createOrchestrationRunController({
  document: _orchServices.document,
  api: _orchServices.api,
  definition: function () { return _orchRootDefinitionSnapshot(); },
  currentId: function () {
    return _orchSession ? (_orchSession.currentId() || '') : '';
  },
  requireValid: function (action) { return _orchDocument.requireValid(action); },
  startSeed: function (definition) { return _orchStartSeed(definition); },
  translate: _orchServices.translate,
  escape: _orchServices.escape,
  limitPolicy: _orchRequestLimits,
  contractPort: _orchRuntimeContracts,
  icon: function (name) { return _ORCH_ICONS[name] || ''; },
  toast: _orchServices.toast,
  onError: _orchServices.reporter('OrchestrationRun'),
  handoffTaskMode: _orchTaskModeHandoff.transfer,
  onResetTrace: _orchResetNodeRunStatus,
  onStateChange: _orchHandleRunStateChange,
  onSurfaceChange: _orchHandleRunSurfaceState,
});
var _orchInspectorFields = createOrchestrationInspectorRenderer({
  escape: _orchServices.escape,
  translate: _orchServices.translate,
});
var _ORCH_CARD_W = 188;       // must match .orch-node width in CSS
var _orchViewport = null;
var _orchCanvasGeometry = createOrchestrationCanvasGeometry({
  cardWidth: _ORCH_CARD_W,
  viewport: function () { return _orchViewport ? _orchViewport.transform() : null; },
});
_orchViewport = createOrchestrationViewportController({
  document: _orchServices.document,
  nodes: _orchEditorState.nodes,
  cardWidth: _ORCH_CARD_W,
  fitMinScale: function () { return orchestrationFitMinScale(_orchServices.window); },
  onChange: function () { _orchRenderEdges(); },
});
var _orchWorkSurfaces = createOrchestrationWorkSurfaceController({surfaces: {
  composer: function () { return _orchComposer; }, run: function () { return _orchRunController; },
}, admitOpen: function () {
  return !_orchStudio || _orchStudio.releaseMobileSheet();
}});
var _orchPanelLayout = createOrchestrationPanelLayoutController({
  document: _orchServices.document,
  window: _orchServices.window,
  translate: _orchServices.translate,
  onChange: function () {
    if (_orchPanelResize) _orchPanelResize.sync();
    _orchViewport.sync();
    _orchRenderEdges();
  },
  workSurfaces: _orchWorkSurfaces,
});
var _orchPanelResize = createOrchestrationPanelResizeController({
  document: _orchServices.document,
  window: _orchServices.window,
  isExpanded: function (name) {
    return name === 'palette'
      ? _orchPanelLayout.paletteExpanded()
      : _orchPanelLayout.inspectorExpanded();
  },
  onChange: function () { _orchViewport.sync(); _orchRenderEdges(); },
});
var _orchDiagnosticIndex = createOrchestrationDiagnosticIndex({
  diagnostics: function () { return _orchDocument.state.diagnostics; },
  definition: function () { return _orchRootDefinitionSnapshot(); },
  workspaceGroups: function () {
    return _orchEditorState.stack().map(function (frame) {
      return frame && frame.groupId || '';
    }).filter(Boolean);
  },
});
var _orchEdgeView = createOrchestrationEdgeView({
  geometry: _orchCanvasGeometry,
  edges: _orchEditorState.edges,
  selectedEdgeId: _orchEditorState.selectedEdgeId,
  connection: function () {
    return _orchCanvasInteraction ? _orchCanvasInteraction.connection() : null;
  },
  portCenter: function (id, side) { return _orchPortCenter(id, side); }, nodeLabel: _orchNodeLabelById,
  issueSummary: function (_edge, index) {
    return index >= 0 ? _orchDiagnosticIndex.edge(index) : null;
  },
  onSelect: function (id) { _orchSelectEdge(id); },
  translate: _orchServices.translate,
  escape: _orchServices.escape,
});
var _orchGraph = createOrchestrationGraphTools();
var _orchDefinitionSnapshot = createOrchestrationDefinitionSnapshotPort({
  graph: _orchGraph,
  workspace: _orchEditorState.workspace,
  stack: _orchEditorState.stack,
});
var _orchEditorControllers = createOrchestrationEditorControllerHub({
  document: _orchServices.document,
  graph: _orchGraph,
  editorState: _orchEditorState,
  controls: function () { return _ORCH_CONTROLS; },
  limitPolicy: _orchRequestLimits,
  defaultParams: function (payload) { return _orchDefaultParams(payload); },
  isDragging: function () {
    return !!(_orchCanvasInteraction && _orchCanvasInteraction.isDragging());
  },
  markDirty: function () { _orchMarkDirty(); },
  render: function () { _orchRender(); },
  renderNodes: function () { _orchRenderNodes(); },
  renderEdges: function () { _orchRenderEdges(); },
  renderInspector: function () { _orchRenderInspector(); },
  translate: _orchServices.translate,
  toast: _orchServices.toast,
  blankGroupDefinition: function () { return _orchBlankGroupDefinition(); },
  fallbackName: function () { return t('orch.group.defaultLabel'); },
  nodeLabel: _orchNodeLabel,
  onNavigate: function () {
    if (_orchEditLifecycle) _orchEditLifecycle.syncHistory();
    if (_orchViewport) _orchViewport.fit();
  },
  tidy: function (opts) { return _orchTidy(opts); },
});
var _orchSelectionFocus = _orchEditorControllers.selectionFocus;
var _orchGraphActions = _orchEditorControllers.graphActions;
var _orchBreadcrumb = _orchEditorControllers.breadcrumb;
var _orchNavigation = _orchEditorControllers.navigation;
_orchIssueNavigator = createOrchestrationIssueNavigator({
  document: _orchServices.document,
  definition: function () { return _orchRootDefinitionSnapshot(); },
  navigateGroups: function (groupIds) {
    return _orchNavigation.navigateToGroups(groupIds);
  },
  selectNode: function (id) { return _orchGraphActions.selectNode(id); },
  selectEdgeAt: function (index) {
    var edge = _orchEditorState.edges()[index];
    return edge ? _orchGraphActions.selectEdge(edge.id) : false;
  },
  focusSelection: _orchSelectionFocus.focus,
  focusDiagnostic: function (target, diagnostic, scrollBehavior, descriptionId) {
    return _orchInspectorView && _orchInspectorView.focusDiagnostic(
      target, diagnostic, scrollBehavior, descriptionId);
  },
  showInspector: function () { return _orchPanelLayout.showInspector(); },
  translate: _orchServices.translate,
});
var _orchHistory = createOrchestrationHistoryController({
  limit: 100,
  coalesceWindow: 700,
  capture: function () {
    return {
      workspace: _orchWorkspaceState(),
      stack: _orchEditorState.stack(),
    };
  },
  fingerprint: function (snapshot) {
    snapshot = snapshot || {};
    var workspace = snapshot.workspace || {
      name: 'Untitled Flow', nodes: [], edges: [],
    };
    return _orchGraph.rootSnapshot(
      workspace.name, workspace.nodes || [], workspace.edges || [],
      snapshot.stack || []
    );
  },
  apply: function (snapshot) {
    if (!snapshot || !snapshot.workspace) return false;
    _orchEditorState.setStack(snapshot.stack);
    _orchAdoptWorkspace(snapshot.workspace);
    _orchEditorState.setSelectedEdgeId(null);
    _orchEditLifecycle.restoreHistory();
    _orchRender();
    return true;
  },
  onChange: function (state) { _orchRenderHistoryState(state); },
});
_orchEditLifecycle = createOrchestrationEditLifecycle({
  documentLifecycle: _orchDocument,
  history: _orchHistory,
  scope: function () {
    return _orchSession ? _orchSession.documentToken() : null;
  },
});
_orchEditLifecycle.resetHistory({ persisted: false });
_orchSession = createOrchestrationSessionController({
  lifecycle: _orchEditLifecycle,
  resetStack: function () { _orchEditorState.setStack([]); },
  workspaceFromDefinition: function (definition) {
    return _orchGraph.workspaceFromDefinition(definition, 'Untitled Flow');
  },
  workspaceFromDefinitionResult: function (definition) {
    return _orchGraph.workspaceFromDefinitionResult(
      definition, 'Untitled Flow');
  },
  adoptWorkspace: function (workspace) { _orchAdoptWorkspace(workspace); },
  render: function () { _orchRender(); },
  fitView: function () { _orchViewport.fit(); },
  tidy: function (opts) { return _orchTidy(opts); },
  nodeCount: _orchEditorState.nodeCount,
});
_orchCanvasInteraction = createOrchestrationCanvasInteractionController({
  document: _orchServices.document,
  window: _orchServices.window,
  geometry: _orchCanvasGeometry,
  findNode: function (id) { return _orchFind(id); },
  addNode: function (payload, x, y) { _orchAddNode(payload, x, y); },
  connectNodes: function (from, to) { _orchConnectNodes(from, to); },
  portCenter: function (id, side) { return _orchPortCenter(id, side); },
  selectForDrag: function (id) { _orchGraphActions.selectNodeForDrag(id); },
  deselect: function () { _orchGraphActions.clearSelection(); },
  markDirty: function () { _orchMarkDirty(); },
  syncViewport: function () { _orchViewport.sync(); },
  render: function () { _orchRender(); },
  renderNodes: function () { _orchRenderNodes(); },
  renderEdges: function () { _orchRenderEdges(); },
  renderInspector: function () { _orchRenderInspector(); },
});
var _orchIoTools = createOrchestrationIoTools();
var _orchFieldValidity = createOrchestrationFieldValidity();
var _orchAuthoring = createOrchestrationAuthoringContractController({
  roles: _ORCH_ROLES,
  controls: _ORCH_CONTROLS,
  ioTools: _orchIoTools,
  api: _orchServices.api,
  translate: _orchServices.translate,
  onChange: function (contract) {
    _ORCH_ROLES = contract.roles; _ORCH_CONTROLS = contract.controls;
    _orchRequestLimits.applyStudio(document);
    if (_orchStudio && _orchStudio.isReady()) {
      _orchRenderPalette();
      _orchRenderNodes();
      if (_orchEditorState.selectedNodeId()) _orchRenderInspector();
    }
  },
  onError: function (error) {
    _orchServices.reportError(
      'OrchestrationAuthoring', 'contract fetch', error);
    _orchToast(t('orch.contract.loadFailed'), true);
  },
});
var _orchIoEditor = createOrchestrationIoEditor({
  ioTools: _orchIoTools,
  fieldValidity: _orchFieldValidity,
  nodes: _orchEditorState.nodes,
  edges: _orchEditorState.edges,
  selectedNode: function () {
    return _orchEditorState.findNode(_orchEditorState.selectedNodeId());
  },
  findNode: _orchEditorState.findNode,
  nodeLabel: _orchNodeLabel,
  escape: _orchServices.escape,
  translate: _orchServices.translate,
  icons: _ORCH_ICONS,
  toast: _orchServices.toast,
  onChange: function (change) {
    _orchMarkDirty(change.historyGroup || '');
    if (change.renderInspector) _orchRenderInspector();
    if (change.renderNodes) _orchRenderNodes();
  },
});
var _orchPaletteView = createOrchestrationPaletteView({
  roles: function () { return _ORCH_ROLES; },
  controls: function () { return _ORCH_CONTROLS; },
  contractState: function () { return _orchAuthoring.snapshot(); },
  icons: _ORCH_ICONS,
  glyphs: _ORCH_GLYPHS,
  iconSrc: function (icon) { return _orchIconSrc(icon); },
  translate: _orchServices.translate,
  escape: _orchServices.escape,
  onAdd: function (payload) { _orchAddNodeAtCenter(payload); },
  onRetry: function () { return _orchFetchAuthoringContract(); },
  isMobile: function () { return _orchIsMobile(); },
  closeMobile: function () { _orchCloseMobilePalette(); },
});
var _orchNodeCatalogue = createOrchestrationNodeCatalogue({
  roles: function () { return _ORCH_ROLES; },
  controls: function () { return _ORCH_CONTROLS; },
  nodeDefaults: function () { return _orchAuthoring.snapshot().nodeDefaults; },
  nodeRuntimeDefaults: function () {
    return _orchAuthoring.snapshot().nodeRuntimeDefaults;
  },
});
var _orchNodeView = createOrchestrationNodeView({
  document: _orchServices.document,
  nodes: _orchEditorState.nodes,
  edges: _orchEditorState.edges,
  selectedId: _orchEditorState.selectedNodeId,
  connectingFrom: function () {
    var connection = _orchCanvasInteraction.connection();
    return connection && connection.from;
  },
  catalogue: _orchNodeCatalogue,
  icons: _ORCH_ICONS,
  glyphs: _ORCH_GLYPHS,
  iconSrc: function (icon) { return _orchIconSrc(icon); },
  defaultEmits: function (role) { return _orchDefaultEmits(role); },
  issueSummary: function (id) { return _orchDiagnosticIndex.node(id); },
  translate: _orchServices.translate,
  escape: _orchServices.escape,
  onSelect: function (id) { _orchSelectNode(id); },
  onNodeKeyDown: function (event, id) { _orchNodeKeyDown(event, id); },
  onHeaderPointerDown: function (event, id) { _orchNodeHeaderDown(event, id); },
  onPortDown: function (event, id) { _orchPortDown(event, id); },
  onPortUp: function (event, id) { _orchPortUp(event, id); },
  onPortKeyDown: function (event, id, side) { _orchPortKeyDown(event, id, side); },
  onEnterGroup: function (id) { _orchEnterGroup(id); },
  onDelete: function (id) { _orchDeleteNode(id); },
});
var _orchNodeEditor = createOrchestrationNodeEditor({
  findNode: _orchEditorState.findNode,
  selectedNodeId: _orchEditorState.selectedNodeId,
  fieldValueContract: _orchAuthoring.fieldValueContract,
  fieldSpec: function (node, key) {
    return _orchAuthoring.fieldSpec(
      node.type, node.role || node.kind || '', key
    );
  },
  markDirty: function (historyGroup) { _orchMarkDirty(historyGroup); },
  renderNodes: function () { _orchRenderNodes(); },
  renderInspector: function () { _orchRenderInspector(); },
});
var _orchInspectorContent = createOrchestrationInspectorContent({
  edges: _orchEditorState.edges,
  findNode: _orchEditorState.findNode,
  nodeLabel: _orchNodeLabel,
  kindLabel: function (node) { return _orchKindLabel(node); },
  blurb: function (node) { return _orchNodeBlurb(node); },
  avatar: function (node) { return _orchInspAvatar(node); },
  traceSnapshotFor: function (id) {
    return _orchRunController.traceSnapshotFor(id);
  },
  persona: function (role) { return _orchRolePersona(role); }, traceContract: _orchAuthoring.traceContract,
  translate: _orchServices.translate,
  escape: _orchServices.escape, richCopy: _orchServices.richCopy,
});
var _orchComposerView = createOrchestrationComposerView({
  document: _orchServices.document, translate: _orchServices.translate, richCopy: _orchServices.richCopy,
  icons: _ORCH_ICONS,
  onVisibilityChange: _orchSyncMobileSurfaceState,
});
var _orchComposer = createOrchestrationComposerController({
  view: _orchComposerView,
  normalizeInspection: normalizeOrchestrationInspection,
  normalizeComposeResult: normalizeOrchestrationComposeResult, inspectionContract: _orchAuthoring.inspectionContract,
  api: _orchServices.api,
  limitPolicy: _orchRequestLimits,
  revision: function () { return _orchDocument.revision(); },
  currentDefinition: function () {
    return _orchEditorState.hasNodes() ? _orchRootDefinitionSnapshot() : null;
  },
  currentId: function () { return _orchSession.currentId(); },
  applyDefinition: function (definition, id, opts) {
    return _orchSession.applyDefinition(definition, id, opts);
  },
  applyDefinitionResult: function (definition, id, opts) {
    return _orchSession.applyDefinitionResult(definition, id, opts);
  },
  translate: _orchServices.translate,
  toast: _orchServices.toast,
  warn: _orchServices.warn,
  onError: _orchServices.reporter('OrchestrationComposer', 'request'),
});
var _orchWorkspaceController = createOrchestrationWorkspaceController({
  document: _orchServices.document,
  normalizeInspection: normalizeOrchestrationInspection,
  normalizeBuiltin: normalizeOrchestrationBuiltinRead,
  normalizeLayout: normalizeOrchestrationLayoutRead,
  normalizeList: normalizeOrchestrationDefinitionListRead,
  normalizeRead: normalizeOrchestrationDefinitionRead,
  normalizeSave: normalizeOrchestrationDefinitionSave,
  normalizeDelete: normalizeOrchestrationDefinitionDelete,
  definitionWriteContract: _orchAuthoring.definitionWriteContract, definitionListContract: _orchAuthoring.definitionListContract, definitionEntryContract: _orchAuthoring.definitionEntryContract, inspectionContract: _orchAuthoring.inspectionContract,
  popupMenus: _orchPopupMenus,
  api: _orchServices.api,
  lifecycle: _orchEditLifecycle,
  session: _orchSession,
  currentName: _orchEditorState.name,
  nodeCount: _orchEditorState.nodeCount,
  currentLevelDefinition: function () { return _orchToDefinition(); },
  workspaceToken: _orchEditorState.workspaceToken,
  rootDefinition: function () { return _orchRootDefinitionSnapshot(); },
  blankDefinition: function () {
    return _orchGraph.definitionFromState('Untitled Flow', [], []);
  },
  applyPositions: _orchEditorState.applyPositions,
  fitView: function () { _orchViewport.fit(); },
  render: function () { _orchRender(); },
  confirmReplace: function () { return _orchConfirmReplace(); },
  confirmDelete: function () {
    return _orchServices.confirm(
      t('orch.store.deleteConfirm'), { danger: true }, false);
  },
  translate: _orchServices.translate,
  escape: _orchServices.escape,
  icons: _ORCH_ICONS,
  toast: _orchServices.toast,
  warn: _orchServices.warn,
  onDefinitionsChanged: function () {
    if (typeof _orchestrationFlowCatalog !== 'undefined'
        && _orchestrationFlowCatalog) {
      _orchestrationFlowCatalog.invalidate();
      _orchestrationFlowCatalog.refresh();
    }
  },
  onUseDefinition: async function (id) {
    if (!(typeof _featureFlags !== 'undefined'
        && _featureFlags.debug_mode === true)) return false;
    if (typeof setActiveFlow !== 'function'
        || typeof _agentInteractionChangeBlocked === 'function'
          && _agentInteractionChangeBlocked()) return false;
    var closed = await _orchStudio.close(null, true);
    if (!closed) return false;
    if (!setActiveFlow(String(id || ''))) {
      _orchStudio.open({ skipInitial: true });
      return false;
    }
    var composer = _orchServices.document
      && _orchServices.document.getElementById('userInput');
    if (composer && typeof composer.focus === 'function') composer.focus();
    return true;
  },
  onError: _orchServices.reporter('OrchestrationWorkspace'),
});
var _orchInspectorView = createOrchestrationInspectorView({
  document: _orchServices.document,
  fieldValidity: _orchFieldValidity,
  nodes: _orchEditorState.nodes,
  edges: _orchEditorState.edges,
  selectedNodeId: _orchEditorState.selectedNodeId,
  selectedEdgeId: _orchEditorState.selectedEdgeId,
  workspaceToken: _orchEditorState.workspaceToken,
  clearSelectedEdge: function () { _orchGraphActions.clearSelectedEdge(); },
  findNode: function (id) { return _orchFind(id); },
  roles: function () { return _ORCH_ROLES; },
  executionOptions: function () { return _orchAuthoring.executionOptions(); },
  controlFields: function (kind) { return _orchAuthoring.controlFields(kind); },
  nodeParam: _orchNodeCatalogue.runtimeParam,
  autoLabel: function (node) { return _orchAutoLabel(node); }, nodeLabel: _orchNodeLabelById,
  kindLabel: function (node) { return _orchKindLabel(node); },
  header: function (node) { return _orchInspHeader(node); },
  section: function (key, icon, open, inner, hint) {
    return _orchSec(key, icon, open, inner, hint);
  },
  labelField: function (node) { return _orchLabelField(node); },
  selectField: function (label, key, value, choices) {
    return _orchSelectFld(label, key, value, choices);
  },
  controlSchemaSection: function (node, fields) {
    return _orchInspectorFields.schemaSection(
      node, fields, _orchNodeCatalogue.runtimeParam);
  },
  roleTaskBody: function (node) { return _orchRoleTaskSectionBody(node); },
  runTraceBody: function (node) { return _orchRunTraceBody(node); },
  personaBody: function (node) { return _orchPersonaSectionBody(node); },
  flowSummaryBody: function (node) { return _orchFlowSummaryBody(node); },
  ioSectionBody: function (node) { return _orchIoSectionBody(node); },
  defaultEmits: function (role) { return _orchDefaultEmits(role); },
  nodeInputs: function (node) { return _orchNodeInputs(node); },
  nodeOutputs: function (node) { return _orchNodeOutputs(node); },
  outputRef: function (nodeId, outputs, output) {
    return _orchIoTools.outputRef(nodeId, outputs, output);
  },
  setParam: function (nodeId, key, value, kind, coalesce) {
    return _orchSetParam(key, value, false, kind, nodeId, coalesce);
  },
  setParamResult: _orchSetParamResult,
  bindIoSection: function (element, nodeId) {
    _orchIoEditor.bindSection(element, nodeId);
  },
  bindEdgeInput: function (targetId, index, ref) {
    _orchBindEdgeInput(targetId, index, ref);
  },
  reverseEdge: function (id) { _orchReverseEdge(id); },
  deleteEdge: function (id) { _orchDeleteEdge(id); },
  enterGroup: function (id) { _orchEnterGroup(id); },
  deleteNode: function (id) { _orchDeleteNode(id); },
  isMobile: function () { return _orchIsMobile(); },
  setMobileOpen: function (open) {
    return _orchStudio.setMobileInspectorOpen(open);
  },
  closeMobile: function () { _orchCloseMobileInspector(); },
  translate: _orchServices.translate,
  escape: _orchServices.escape,
  icons: _ORCH_ICONS,
});
_orchCanvasView = createOrchestrationCanvasView({
  document: _orchServices.document,
  name: _orchEditorState.name,
  nodeCount: _orchEditorState.nodeCount,
  nodeView: _orchNodeView,
  edgeView: _orchEdgeView,
  inspectorView: _orchInspectorView,
  navigation: _orchNavigation,
  viewport: _orchViewport,
  translate: _orchServices.translate, richCopy: _orchServices.richCopy,
  icons: _ORCH_ICONS,
});
var _orchStudio = createOrchestrationStudioController({
  document: _orchServices.document,
  window: _orchServices.window,
  workSurfaces: _orchWorkSurfaces,
  createShell: function (options) {
    return createOrchestrationStudioShell(options);
  },
  shellOptions: function () {
    return {
      document: _orchServices.document,
      popupMenus: _orchPopupMenus,
      icons: _ORCH_ICONS,
      logoUrl: _orchIconBase() + '/tofu-planner.svg',
      translate: _orchServices.translate,
      escape: _orchServices.escape, richCopy: _orchServices.richCopy, limitPolicy: _orchRequestLimits,
      onBackdrop: function (event) { _orchStudio.close(event); },
      commands: createOrchestrationStudioShellCommands({
        studio: _orchStudio,
        document: _orchDocument,
        workspace: _orchWorkspaceController,
        history: _orchHistory,
        viewport: _orchViewport,
        panels: _orchPanelLayout,
        composer: _orchComposer,
        run: _orchRunController,
        exporter: _orchExporter,
        rename: _orchOnRename,
      }),
    };
  },
  onMount: function () {
    _orchPanelResize.bind();
    _orchRenderPalette();
    _orchWireCanvas();
    _orchViewport.wire();
    _orchRenderDocState();
    _orchRenderHistoryState();
  },
  installUnloadGuard: function (target) {
    _orchDocument.installUnloadGuard(target);
  },
  hasNodes: _orchEditorState.hasNodes,
  loadInitial: function () { _orchLoadBuiltin('blank', { initial: true }); },
  render: function () { _orchRender(); },
  refreshContract: function () { _orchFetchAuthoringContract(); },
  syncDesktopPanels: function () { return _orchPanelLayout.sync(); },
  confirmDiscard: function () { return _orchDocument.confirmDiscard(
    'orch.doc.closeConfirm'); },
  cancelGesture: function () { return _orchCanvasInteraction.cancelGesture(); },
  closePopups: function () {
    var issueClosed = !!(_orchIssueNavigator
      && _orchIssueNavigator.close(true));
    var menusClosed = _orchPopupMenus.closeAll();
    return issueClosed || menusClosed;
  },
  dismissTransient: function () { return _orchPanelLayout.dismissTransient(); },
  selectedEdgeId: _orchEditorState.selectedEdgeId,
  selectedNodeId: _orchEditorState.selectedNodeId,
  save: function () { return _orchWorkspaceController.save(); },
  undo: function () { _orchUndo(); },
  redo: function () { _orchRedo(); },
  zoomIn: function () { _orchViewport.zoomIn(); },
  zoomOut: function () { _orchViewport.zoomOut(); },
  zoomReset: function () { _orchViewport.reset(); },
  deleteEdge: function (id) { _orchDeleteEdge(id); },
  deleteNode: function (id) { _orchDeleteNode(id); },
});
var _orchStudioApi = createOrchestrationStudioApi({
  open: function (options) { return _orchStudio.open(options); },
  close: function (event, force) { return _orchStudio.close(event, force); },
  refreshAuthoringContract: function () {
    return _orchAuthoring.load().then(function (contract) {
      // Render both success and unavailable states; only a settled contract
      // or explicit legacy decision releases the palette gate.
      if (_orchStudio.isReady() && !contract.ready) _orchRenderPalette();
      return contract;
    });
  },
  loadDefinition: function (id) {
    return _orchWorkspaceController.loadFromStore(id);
  },
  toast: _orchServices.toast,
});
runtimeScope._orchServices = _orchServices;
runtimeScope._orchStudioApi = _orchStudioApi;
