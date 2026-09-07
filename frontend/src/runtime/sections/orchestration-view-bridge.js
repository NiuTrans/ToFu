/* ===== migrated source: orchestration-view-bridge.js ===== */
/* Orchestration Studio canvas/inspector view bridge.
 *
 * Stable render and editor adapters over the focused node, graph, I/O and
 * Inspector ports. No rendering policy lives here. Load before
 * orchestration.js; referenced controller variables are resolved on call.
 */

function _orchRender() {
  return _orchCanvasView ? _orchCanvasView.render() : null;
}
function _orchRenderNodes() {
  return _orchCanvasView ? _orchCanvasView.renderNodes() : null;
}
function _orchAutoLabel(node) { return _orchNodeView.autoLabel(node); }
function _orchNodeLabel(node) {
  return node ? (node.name || _orchAutoLabel(node)) : '';
}
function _orchNodeLabelById(id) {
  var node = _orchEditorState.findNode(id);
  return _orchNodeLabel(node) || id;
}
function _orchKindLabel(node) { return _orchNodeView.kindLabel(node); }
function _orchNodeBlurb(node) { return _orchNodeView.nodeBlurb(node); }
function _orchInspAvatar(node) {
  return _orchNodeView.inspectorAvatar(node);
}
function _orchInspHeader(node) {
  return _orchInspectorContent.header(node);
}
function _orchSec(titleKey, icon, open, inner, hintKey) {
  return _orchInspectorContent.section(
    titleKey, icon, open, inner, hintKey);
}

function _orchSelectNode(id) { return _orchGraphActions.selectNode(id); }
function _orchNodeKeyDown(event, id) {
  return _orchGraphActions.nodeKeyDown(event, id);
}
function _orchSelectEdge(id) { return _orchGraphActions.selectEdge(id); }
function _orchReverseEdge(id) { return _orchGraphActions.reverseEdge(id); }

function _orchBindEdgeInput(targetId, index, reference) {
  return _orchIoEditor.bindInput(targetId, index, reference);
}
function _orchNodeInputs(node) { return _orchIoTools.nodeInputs(node); }
function _orchNodeOutputs(node) { return _orchIoTools.nodeOutputs(node); }
function _orchIoSectionBody(node) { return _orchIoEditor.sectionBody(node); }

function _orchRenderInspector() {
  return _orchCanvasView ? _orchCanvasView.renderInspector() : null;
}
function _orchLabelField(node) {
  return _orchInspectorFields.labelField(node, _orchAutoLabel(node));
}
function _orchSelectFld(label, key, value, options) {
  return _orchInspectorFields.selectField(label, key, value, options);
}
function _orchSetParam(key, value, isNumber, kind, nodeId, coalesce) {
  return _orchNodeEditor.setParam(
    nodeId, key, value,
    kind || (isNumber ? 'int'
      : (typeof value === 'boolean' ? 'bool' : 'text')),
    coalesce
  );
}
function _orchSetParamResult(nodeId, key, value, kind, coalesce) {
  return _orchNodeEditor.setParamResult(
    nodeId, key, value,
    kind || (typeof value === 'boolean' ? 'bool' : 'text'), coalesce
  );
}

function _orchDefaultEmits(role) {
  return _orchAuthoring.defaultEmits(role);
}
function _orchRoleTaskSectionBody(node) {
  return _orchInspectorFields.roleTaskSection(
    node, null, _orchAuthoring.roleFields(node.role),
    _orchNodeCatalogue.runtimeParam);
}
function _orchRolePersona(role) { return _orchAuthoring.persona(role); }
function _orchRunTraceBody(node) {
  return _orchInspectorContent.runTraceBody(node);
}
function _orchPersonaSectionBody(node) {
  return _orchInspectorContent.personaSectionBody(node);
}
function _orchFlowSummaryBody(node) {
  return _orchInspectorContent.flowSummaryBody(node);
}
function _orchFetchAuthoringContract() {
  return _orchStudioApi.refreshAuthoringContract();
}

