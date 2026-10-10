/* ===== migrated source: orchestration-canvas-interaction.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-canvas-interaction.js — stable Canvas gesture facade

   Routes one shared Pointer Session across surface, node-drag and connection
   collaborators while retaining the existing editor-facing interface.
   ═══════════════════════════════════════════════════════════════════ */
function createOrchestrationCanvasInteractionController(options) {
  options = options || {};
  var unbindPointer = null;

  function stopPointer() {
    if (unbindPointer) unbindPointer();
    unbindPointer = null;
  }
  function startPointer(event) {
    stopPointer();
    unbindPointer = bindOrchestrationPointerSession({
      pointerId: event && event.pointerId,
      moveTarget: context.document(),
      pointerTarget: context.window(),
      window: context.window(),
      onMove: onPointerMove,
      onEnd: onPointerUp,
    });
  }

  var context = createOrchestrationCanvasGestureContext(Object.assign(
    {}, options, { startPointer: startPointer, stopPointer: stopPointer }));
  var surface = createOrchestrationCanvasSurfaceInteraction(context);
  var nodeDrag = createOrchestrationCanvasNodeDrag(context);
  var connection = createOrchestrationCanvasConnection(context);

  function onPointerMove(event) {
    if (nodeDrag.active()) return nodeDrag.move(event);
    return connection.move(event);
  }

  function onPointerUp() {
    nodeDrag.finish();
    connection.finish();
    stopPointer();
  }

  function cancelGesture() {
    var consumed = nodeDrag.cancel();
    consumed = connection.cancel() || consumed;
    if (!consumed) return false;
    stopPointer();
    context.render();
    return true;
  }

  return Object.freeze({
    wireCanvas: surface.wire,
    nodeHeaderDown: nodeDrag.start,
    portDown: connection.startPointer,
    portUp: connection.completePointer,
    portKeyDown: connection.keyDown,
    onPointerMove: onPointerMove,
    onPointerUp: onPointerUp,
    cancelGesture: cancelGesture,
    connection: connection.value,
    isDragging: nodeDrag.active,
  });
}

