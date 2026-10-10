/* ===== migrated source: orchestration-canvas-node-drag.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-canvas-node-drag.js — transient node drag gesture
   ═══════════════════════════════════════════════════════════════════ */
function createOrchestrationCanvasNodeDrag(context) {
  var drag = null;

  function start(event, id) {
    if (!context.primary(event)) return false;
    event.stopPropagation();
    var focusSelection = event.target
      && typeof event.target.closest === 'function'
      && !!event.target.closest('.orch-node-select');
    var node = context.findNode(id);
    if (!node) return false;
    var options = context.options;
    if (typeof options.selectForDrag === 'function') options.selectForDrag(id);
    var point = options.geometry.canvasPoint(
      context.canvas(), event.clientX, event.clientY);
    drag = {
      id: id,
      dx: point.x - node.x,
      dy: point.y - node.y,
      startX: node.x,
      startY: node.y,
      moved: false,
    };
    context.startPointer(event);
    var element = context.document().getElementById('orch-node-' + id);
    if (element) element.classList.add('is-dragging');
    context.renderNodes();
    context.renderEdges();
    context.renderInspector();
    if (focusSelection) {
      var refreshed = context.document().getElementById('orch-node-' + id);
      var select = refreshed && refreshed.querySelector('.orch-node-select');
      if (select) select.focus({ preventScroll: true });
    }
    return true;
  }

  function move(event) {
    if (!drag) return false;
    var options = context.options;
    var point = options.geometry.canvasPoint(
      context.canvas(), event.clientX, event.clientY);
    var node = context.findNode(drag.id);
    if (!node) return false;
    drag.moved = true;
    var next = options.geometry.clampNode({
      x: point.x - drag.dx,
      y: point.y - drag.dy,
    }, 4);
    node.x = next.x;
    node.y = next.y;
    var element = context.document().getElementById('orch-node-' + node.id);
    if (element) {
      element.style.left = node.x + 'px';
      element.style.top = node.y + 'px';
    }
    if (typeof options.syncViewport === 'function') options.syncViewport();
    context.renderEdges();
    return true;
  }

  function finish() {
    if (!drag) return false;
    var moved = !!drag.moved;
    var element = context.document().getElementById('orch-node-' + drag.id);
    if (element) element.classList.remove('is-dragging');
    drag = null;
    if (moved && typeof context.options.markDirty === 'function') {
      context.options.markDirty();
    }
    return true;
  }

  function cancel() {
    if (!drag) return false;
    var node = context.findNode(drag.id);
    if (node) {
      node.x = drag.startX;
      node.y = drag.startY;
    }
    var element = context.document().getElementById('orch-node-' + drag.id);
    if (element) element.classList.remove('is-dragging');
    drag = null;
    return true;
  }

  return Object.freeze({
    start: start,
    move: move,
    finish: finish,
    cancel: cancel,
    active: function () { return !!drag; },
  });
}

