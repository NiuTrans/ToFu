/* ===== migrated source: orchestration-run-overlay.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-run-overlay.js — Studio canvas runtime projection

   Projects shared run events onto node-card status attributes and refreshes
   the selected-node trace. Execution transport remains in orchestration-run;
   this controller owns only the canvas/Inspector presentation seam.

   MUST load before orchestration.js.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationRunOverlay(options) {
  options = options || {};

  function _document() {
    return options.document || document;
  }

  function _selectedNodeId() {
    return typeof options.selectedNodeId === 'function'
      ? options.selectedNodeId() : null;
  }

  function _definition() {
    return typeof options.definition === 'function'
      ? options.definition() : null;
  }

  function startSeed(definition) {
    var snapshot = definition || _definition() || {};
    var nodes = Array.isArray(snapshot.nodes) ? snapshot.nodes : [];
    var start = nodes.filter(function (node) {
      return node && node.kind === 'start';
    })[0];
    var seed = start && start.params && start.params.seed;
    return seed == null || seed === '' ? '' : String(seed);
  }

  function reset() {
    var elements = _document().querySelectorAll('.orch-node[data-run-status]');
    Array.prototype.forEach.call(elements, function (element) {
      element.removeAttribute('data-run-status');
    });
  }

  function setNodeStatus(nodeId, status) {
    if (!nodeId) return false;
    var element = _document().getElementById('orch-node-' + nodeId);
    if (!element) return false;
    element.setAttribute('data-run-status', status);
    return true;
  }

  function applyChange(state, change) {
    state = state || {};
    change = change || {};
    if (change.nodeId && change.nodeStatus) {
      setNodeStatus(change.nodeId, change.nodeStatus);
    }
    var selected = _selectedNodeId();
    if (!selected || (selected !== change.nodeId && !change.terminal)) {
      return false;
    }
    if (typeof options.renderInspector === 'function') {
      options.renderInspector();
    }
    return true;
  }

  return {
    startSeed: startSeed,
    reset: reset,
    setNodeStatus: setNodeStatus,
    applyChange: applyChange,
  };
}

