/* ===== migrated source: orchestration-canvas-command-bridge.js ===== */
/* Orchestration Studio canvas command bridge.
 *
 * Stable global commands for palette/mobile actions, graph mutations, nested
 * navigation and edge geometry. Focused controllers own the behavior; this
 * file preserves the extension/shell surface without bloating the composition
 * root. Load before orchestration.js so its controller factories can receive
 * these functions as callbacks.
 */

function _orchRenderPalette() {
  _orchPaletteView.render(document.getElementById('orchPalette'));
}

function _orchIsMobile() {
  return _orchStudio.isMobile();
}

function _orchAddNodeAtCenter(payload) {
  var canvas = document.getElementById('orchCanvas');
  if (!canvas) return;
  var point = _orchCanvasGeometry.centeredNode(canvas, 80);
  _orchAddNode(payload, point.x, point.y);
}
function _orchCloseMobilePalette() {
  return _orchStudio.closeMobilePalette();
}
function _orchCloseMobileInspector() {
  return _orchStudio.closeMobileInspector();
}

function _orchWireCanvas() {
  return _orchCanvasInteraction.wireCanvas();
}

function _orchAddNode(payload, x, y) {
  return _orchGraphActions.addNode(payload, x, y);
}

function _orchBlankGroupDefinition() {
  return _orchAuthoring.blankSubflowDefinition();
}

function _orchDefaultParams(payload) {
  return _orchAuthoring.nodeParams(payload);
}

function _orchNodeHeaderDown(event, id) {
  return _orchCanvasInteraction.nodeHeaderDown(event, id);
}

function _orchPortDown(event, id) {
  return _orchCanvasInteraction.portDown(event, id);
}

function _orchPortUp(event, id) {
  return _orchCanvasInteraction.portUp(event, id);
}

function _orchPortKeyDown(event, id, side) {
  return _orchCanvasInteraction.portKeyDown(event, id, side);
}

function _orchConnectNodes(from, to) {
  return _orchGraphActions.connectNodes(from, to);
}

function _orchFind(id) {
  return _orchGraphActions.findNode(id);
}

function _orchDeleteNode(id) {
  return _orchGraphActions.deleteNode(id);
}

function _orchDeleteEdge(id) {
  return _orchGraphActions.deleteEdge(id);
}

function _orchWorkspaceState() {
  return _orchNavigation.workspaceState();
}

function _orchAdoptWorkspace(workspace) {
  return _orchNavigation.adoptWorkspace(workspace);
}

function _orchEnterGroup(id) {
  return _orchNavigation.enterGroup(id);
}

function _orchPortCenter(id, side) {
  var canvas = document.getElementById('orchCanvas');
  var card = document.getElementById('orch-node-' + id);
  var port = card && card.querySelector('.orch-port-' + side);
  return _orchCanvasGeometry.portCenter(canvas, port);
}

function _orchRenderEdges() {
  return _orchCanvasView ? _orchCanvasView.renderEdges() : null;
}

