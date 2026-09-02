/* ===== migrated source: orchestration-canvas-connection.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-canvas-connection.js — pointer/keyboard edge gesture
   ═══════════════════════════════════════════════════════════════════ */
function createOrchestrationCanvasConnection(context) {
  var connection = null;

  function startPointer(event, id) {
    if (!context.primary(event)) return false;
    event.stopPropagation();
    var point = context.options.geometry.canvasPoint(
      context.canvas(), event.clientX, event.clientY);
    connection = { from: id, x: point.x, y: point.y };
    context.startPointer(event);
    return connection;
  }

  function completePointer(event, id) {
    if (!connection) return false;
    event.stopPropagation();
    if (connection.from && connection.from !== id
        && typeof context.options.connectNodes === 'function') {
      context.options.connectNodes(connection.from, id);
    }
    connection = null;
    context.stopPointer();
    context.render();
    return true;
  }

  function keyDown(event, id, side) {
    if (side === 'out') {
      var point = typeof context.options.portCenter === 'function'
        ? context.options.portCenter(id, 'out') : null;
      if (!point) return false;
      connection = { from: id, x: point.x, y: point.y };
      context.startPointer(event);
      context.renderNodes();
      context.renderEdges();
      var sourceCard = context.document().getElementById('orch-node-' + id);
      var source = sourceCard && sourceCard.querySelector('.orch-port-out');
      if (source) source.focus();
      return true;
    }
    if (!connection || !connection.from || connection.from === id) return false;
    if (typeof context.options.connectNodes === 'function') {
      context.options.connectNodes(connection.from, id);
    }
    connection = null;
    context.stopPointer();
    context.render();
    var targetCard = context.document().getElementById('orch-node-' + id);
    var target = targetCard && targetCard.querySelector('.orch-port-in');
    if (target) target.focus();
    return true;
  }

  function move(event) {
    if (!connection) return false;
    var point = context.options.geometry.canvasPoint(
      context.canvas(), event.clientX, event.clientY);
    connection.x = point.x;
    connection.y = point.y;
    context.renderEdges();
    return true;
  }

  function finish() {
    if (!connection) return false;
    connection = null;
    context.renderNodes();
    context.renderEdges();
    return true;
  }

  function cancel() {
    if (!connection) return false;
    connection = null;
    return true;
  }

  return Object.freeze({
    startPointer: startPointer,
    completePointer: completePointer,
    keyDown: keyDown,
    move: move,
    finish: finish,
    cancel: cancel,
    value: function () { return connection; },
  });
}

