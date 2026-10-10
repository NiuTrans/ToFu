/* ===== migrated source: orchestration-canvas.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-canvas.js — pure canvas coordinates + edge routing

   Centralizes the geometry shared by palette drops, touch-to-add, node
   dragging, connection previews and SVG edge rendering. It owns no DOM
   listeners or graph mutations; orchestration.js supplies viewport/port
   elements and consumes deterministic points and paths.

   MUST load before orchestration.js.
   ═══════════════════════════════════════════════════════════════════ */


function createOrchestrationCanvasGeometry(options) {
  options = options || {};
  var cardWidth = options.cardWidth || 188;

  function viewportTransform() {
    var value = typeof options.viewport === 'function'
      ? options.viewport() : null;
    return {
      scale: value && Number(value.scale) > 0 ? Number(value.scale) : 1,
      offsetX: value && Number(value.offsetX) || 0,
      offsetY: value && Number(value.offsetY) || 0,
    };
  }

  function canvasPoint(canvas, clientX, clientY) {
    var rect = canvas.getBoundingClientRect();
    var viewport = viewportTransform();
    return {
      x: (clientX - rect.left + canvas.scrollLeft - viewport.offsetX)
        / viewport.scale,
      y: (clientY - rect.top + canvas.scrollTop - viewport.offsetY)
        / viewport.scale,
    };
  }

  function clampNode(point, minimum) {
    minimum = minimum == null ? 4 : minimum;
    return {
      x: Math.max(minimum, point.x),
      y: Math.max(minimum, point.y),
    };
  }

  function dropNode(canvas, clientX, clientY, headerOffset) {
    var point = canvasPoint(canvas, clientX, clientY);
    return clampNode({
      x: point.x - cardWidth / 2,
      y: point.y - (headerOffset == null ? 20 : headerOffset),
    }, 8);
  }

  function centeredNode(canvas, cardHeight) {
    var viewport = viewportTransform();
    return clampNode({
      x: (canvas.scrollLeft + canvas.clientWidth / 2 - viewport.offsetX)
        / viewport.scale - cardWidth / 2,
      y: (canvas.scrollTop + canvas.clientHeight / 2 - viewport.offsetY)
        / viewport.scale
        - (cardHeight == null ? 80 : cardHeight) / 2,
    }, 8);
  }

  function portCenter(canvas, port) {
    if (!canvas || !port) return null;
    var canvasRect = canvas.getBoundingClientRect();
    var portRect = port.getBoundingClientRect();
    var viewport = viewportTransform();
    return {
      x: (portRect.left - canvasRect.left + canvas.scrollLeft
        + portRect.width / 2 - viewport.offsetX) / viewport.scale,
      y: (portRect.top - canvasRect.top + canvas.scrollTop
        + portRect.height / 2 - viewport.offsetY) / viewport.scale,
    };
  }

  function fanOffset(index, count) {
    if (count <= 1) return 0;
    var step = Math.min(26, (cardWidth * 0.66) / (count - 1));
    return (index - (count - 1) / 2) * step;
  }

  function bezier(from, to) {
    // Ports exit the source bottom and enter the target top, so the normal
    // route is a vertical S. Near-level/back edges bow sideways to avoid
    // folding across their node cards.
    var dx = to.x - from.x;
    var dy = to.y - from.y;
    if (dy >= 30) {
      var vertical = dy * 0.5;
      return 'M ' + from.x + ' ' + from.y
        + ' C ' + from.x + ' ' + (from.y + vertical) + ' '
        + to.x + ' ' + (to.y - vertical) + ' '
        + to.x + ' ' + to.y;
    }
    var side = dx >= 0 ? 1 : -1;
    var horizontal = Math.max(70, Math.abs(dx) * 0.5);
    var backVertical = Math.max(40, Math.abs(dy) * 0.5);
    return 'M ' + from.x + ' ' + from.y
      + ' C ' + (from.x + side * horizontal) + ' '
      + (from.y + backVertical) + ' '
      + (to.x + side * horizontal) + ' '
      + (to.y - backVertical) + ' '
      + to.x + ' ' + to.y;
  }

  function edgeRoutes(edges, getPortCenter) {
    var incoming = {};
    var outgoing = {};
    var incomingSeen = {};
    var outgoingSeen = {};
    edges.forEach(function (edge) {
      incoming[edge.to] = (incoming[edge.to] || 0) + 1;
      outgoing[edge.from] = (outgoing[edge.from] || 0) + 1;
    });

    var routes = [];
    edges.forEach(function (edge) {
      var from = getPortCenter(edge.from, 'out');
      var to = getPortCenter(edge.to, 'in');
      if (!from || !to) return;
      var outIndex = outgoingSeen[edge.from] || 0;
      var inIndex = incomingSeen[edge.to] || 0;
      outgoingSeen[edge.from] = outIndex + 1;
      incomingSeen[edge.to] = inIndex + 1;
      var routedFrom = {
        x: from.x + fanOffset(outIndex, outgoing[edge.from]),
        y: from.y,
      };
      var routedTo = {
        x: to.x + fanOffset(inIndex, incoming[edge.to]),
        y: to.y,
      };
      routes.push({
        edge: edge,
        from: routedFrom,
        to: routedTo,
        path: bezier(routedFrom, routedTo),
      });
    });
    return routes;
  }

  return {
    canvasPoint: canvasPoint,
    clampNode: clampNode,
    dropNode: dropNode,
    centeredNode: centeredNode,
    portCenter: portCenter,
    fanOffset: fanOffset,
    bezier: bezier,
    edgeRoutes: edgeRoutes,
  };
}

