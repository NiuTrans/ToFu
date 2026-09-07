/* ===== migrated source: orchestration-inspector-view.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-inspector-view.js — selected subject DOM lifecycle

   Owns selection, replacement, focus, scroll and interaction binding. Pure
   node/edge/empty HTML lives in orchestration-inspector-projection.js.
   ═══════════════════════════════════════════════════════════════════ */


function createOrchestrationInspectorView(options) {
  options = options || {};
  var projection = options.projection
    || createOrchestrationInspectorProjection(options);
  var scroll = options.scrollState || createOrchestrationScrollState();
  var interaction = options.interaction
    || createOrchestrationInspectorInteraction(options);

  function doc() { return options.document || document; }

  function render(element) {
    element = element || doc().getElementById('orchInspector');
    if (!element) return;
    var focused = interaction.captureFocus(element);
    scroll.capture(element);
    var selectedEdgeId = typeof options.selectedEdgeId === 'function'
      ? options.selectedEdgeId() : null;
    if (selectedEdgeId) {
      var edge = projection.findEdge(selectedEdgeId);
      if (edge) {
        interaction.setMobileOpen(element, true);
        element.innerHTML = projection.edgeHtml(edge);
        interaction.bind(element, { edge: edge });
        interaction.restoreFocus(element, focused);
        scroll.restore(element, interaction.scope({ edge: edge }));
        return;
      }
      if (typeof options.clearSelectedEdge === 'function') {
        options.clearSelectedEdge();
      }
    }

    var selectedNodeId = typeof options.selectedNodeId === 'function'
      ? options.selectedNodeId() : null;
    var node = selectedNodeId ? projection.findNode(selectedNodeId) : null;
    interaction.setMobileOpen(element, !!node);
    if (!node) {
      element.innerHTML = projection.emptyHtml();
      scroll.restore(element, interaction.scope({}));
      return;
    }
    element.innerHTML = projection.nodeHtml(node);
    interaction.bind(element, { node: node });
    interaction.restoreFocus(element, focused);
    scroll.restore(element, interaction.scope({ node: node }));
  }

  function focusDiagnostic(target, diagnostic, scrollBehavior, descriptionId) {
    var element = doc().getElementById('orchInspector');
    return interaction.focusDiagnostic(
      element, target, diagnostic, scrollBehavior, descriptionId);
  }

  return {
    render: render,
    focusDiagnostic: focusDiagnostic,
    edgeHtml: projection.edgeHtml,
    nodeHtml: projection.nodeHtml,
  };
}

