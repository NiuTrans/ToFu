/* ===== migrated source: orchestration-graph-action-context.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-graph-action-context.js — shared graph action ports

   Normalizes live graph/document/view callbacks once for mutation and
   selection collaborators. No graph policy or action sequencing lives here.
   ═══════════════════════════════════════════════════════════════════ */
function createOrchestrationGraphActionContext(options) {
  options = options || {};

  function call(name) {
    var args = Array.prototype.slice.call(arguments, 1);
    return typeof options[name] === 'function'
      ? options[name].apply(null, args) : undefined;
  }

  function nodes() { return call('nodes') || []; }
  function edges() { return call('edges') || []; }
  function setGraph(nextNodes, nextEdges) {
    return call('setGraph', nextNodes, nextEdges);
  }
  function setSelection(nodeId, edgeId) {
    return call('setSelection', nodeId || null, edgeId || null);
  }
  function render(name) { return call(name); }
  function translate(key, params) {
    return typeof options.translate === 'function'
      ? options.translate(key, params) : key;
  }
  function toast(key, params) {
    if (typeof options.toast === 'function') {
      options.toast(translate(key, params));
    }
  }
  function topologyError(reason, duplicateKey) {
    if (reason === 'start-input') toast('orch.toast.startNoIn');
    if (reason === 'stop-output') toast('orch.toast.stopNoOut');
    if (reason === 'self-loop') toast('orch.toast.selfLoop');
    if (reason === 'duplicate') {
      toast(duplicateKey || 'orch.toast.edgeExists');
    }
  }

  return Object.freeze({
    graph: options.graph,
    limitPolicy: options.limitPolicy || null,
    nodes: nodes,
    edges: edges,
    setGraph: setGraph,
    setSelection: setSelection,
    markDirty: function () { return call('markDirty'); },
    render: render,
    toast: toast,
    topologyError: topologyError,
    selectedNodeId: function () { return call('selectedNodeId') || null; },
    selectedEdgeId: function () { return call('selectedEdgeId') || null; },
    controls: function () { return call('controls') || []; },
    subflowDepth: function () { return call('subflowDepth') || 0; },
    nodeLimit: function () { return call('nodeLimit'); },
    subflowDepthLimit: function () { return call('subflowDepthLimit'); },
    nextId: function (prefix) { return call('nextId', prefix); },
    defaultParams: function (payload) {
      return call('defaultParams', payload) || {};
    },
    isDragging: function () { return !!call('isDragging'); },
    focusSelection: function () { return call('focusSelection'); },
  });
}

