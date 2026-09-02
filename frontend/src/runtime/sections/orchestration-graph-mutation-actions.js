/* ===== migrated source: orchestration-graph-mutation-actions.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-graph-mutation-actions.js — structural graph commands
   ═══════════════════════════════════════════════════════════════════ */
function createOrchestrationGraphMutationActions(context) {
  function findNode(id) {
    return context.graph.findNode(context.nodes(), id);
  }

  function addNode(payload, x, y) {
    payload = payload || {};
    if (payload.ptype === 'control') {
      var control = context.controls().filter(function (item) {
        return item.kind === payload.kind;
      })[0];
      if (control && control.single && context.nodes().some(function (node) {
        return node.kind === payload.kind;
      })) {
        context.toast('orch.toast.singleNode', { name: control.label });
        return null;
      }
    }
    var limitPolicy = context.limitPolicy;
    var nodeLimit = limitPolicy
      && typeof limitPolicy.definitionNodeLimit === 'function'
      ? limitPolicy.definitionNodeLimit() : context.nodeLimit();
    if (Number.isSafeInteger(nodeLimit) && nodeLimit > 0
        && context.nodes().length >= nodeLimit) {
      context.toast('orch.toast.nodeLimit', { n: nodeLimit });
      return null;
    }
    var depth = context.subflowDepth();
    var depthLimit = limitPolicy
      && typeof limitPolicy.subflowDepthLimit === 'function'
      ? limitPolicy.subflowDepthLimit() : context.subflowDepthLimit();
    if (payload.ptype === 'subflow' && Number.isSafeInteger(depthLimit)
        && depthLimit > 0 && depth >= depthLimit) {
      context.toast('orch.toast.subflowDepthLimit', { n: depthLimit });
      return null;
    }

    var prefix = payload.ptype === 'role' ? payload.role
      : payload.ptype === 'subflow' ? 'group' : payload.kind;
    var node = {
      id: context.nextId(prefix),
      type: payload.ptype,
      role: payload.role || '',
      kind: payload.kind || '',
      x: x,
      y: y,
      name: '',
      params: context.defaultParams(payload),
    };
    context.setGraph(context.nodes().concat([node]), context.edges());
    context.setSelection(node.id, null);
    context.markDirty();
    context.render('render');
    return node;
  }

  function connectNodes(from, to) {
    var result = context.graph.connect(
      context.nodes(), context.edges(), from, to, function () {
        return context.nextId('e');
      });
    if (!result.ok || !result.changed) {
      context.topologyError(result.reason);
      return result;
    }
    context.setGraph(context.nodes(), result.edges);
    context.markDirty();
    return result;
  }

  function deleteNode(id) {
    var next = context.graph.deleteNode(context.nodes(), context.edges(), id);
    context.setGraph(next.nodes, next.edges);
    var selected = context.selectedNodeId();
    context.setSelection(selected === id ? null : selected, null);
    context.markDirty();
    context.render('render');
    return next;
  }

  function deleteEdge(id) {
    var next = context.graph.deleteEdge(context.edges(), id);
    context.setGraph(context.nodes(), next);
    var selectedEdge = context.selectedEdgeId();
    context.setSelection(
      context.selectedNodeId(), selectedEdge === id ? null : selectedEdge);
    context.markDirty();
    context.render('renderEdges');
    context.render('renderInspector');
    return next;
  }

  function reverseEdge(id) {
    var result = context.graph.reverseEdge(
      context.nodes(), context.edges(), id);
    if (!result.ok) {
      context.topologyError(result.reason, 'orch.toast.dupEdge');
      return result;
    }
    context.setGraph(context.nodes(), result.edges);
    context.markDirty();
    context.render('renderEdges');
    context.render('renderInspector');
    return result;
  }

  return Object.freeze({
    findNode: findNode,
    addNode: addNode,
    connectNodes: connectNodes,
    deleteNode: deleteNode,
    deleteEdge: deleteEdge,
    reverseEdge: reverseEdge,
  });
}

