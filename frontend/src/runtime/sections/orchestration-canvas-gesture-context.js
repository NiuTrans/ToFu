/* ===== migrated source: orchestration-canvas-gesture-context.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-canvas-gesture-context.js — shared Canvas gesture ports
   ═══════════════════════════════════════════════════════════════════ */
function createOrchestrationCanvasGestureContext(options) {
  options = options || {};
  function doc() { return options.document || document; }
  function win() { return options.window || window; }
  function canvas() { return doc().getElementById('orchCanvas'); }
  function call(name) {
    var args = Array.prototype.slice.call(arguments, 1);
    return typeof options[name] === 'function'
      ? options[name].apply(null, args) : undefined;
  }
  return Object.freeze({
    options: options,
    document: doc,
    window: win,
    canvas: canvas,
    primary: function (event) {
      return typeof event.button !== 'number' || event.button === 0;
    },
    findNode: function (id) { return call('findNode', id) || null; },
    render: function () { return call('render'); },
    renderNodes: function () { return call('renderNodes'); },
    renderEdges: function () { return call('renderEdges'); },
    renderInspector: function () { return call('renderInspector'); },
    startPointer: function (event) { return call('startPointer', event); },
    stopPointer: function () { return call('stopPointer'); },
  });
}

