/* ===== migrated source: orchestration-canvas-surface-interaction.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-canvas-surface-interaction.js — drop/deselect wiring
   ═══════════════════════════════════════════════════════════════════ */
function createOrchestrationCanvasSurfaceInteraction(context) {
  var wired = false;

  function wire() {
    var canvas = context.canvas();
    if (!canvas || wired) return false;
    wired = true;
    canvas.addEventListener('dragover', function (event) {
      var transfer = event.dataTransfer;
      if (transfer && Array.prototype.indexOf.call(
        transfer.types || [], 'text/orch') !== -1) {
        event.preventDefault();
        transfer.dropEffect = 'copy';
      }
    });
    canvas.addEventListener('drop', function (event) {
      var transfer = event.dataTransfer;
      var raw = transfer && transfer.getData('text/orch');
      if (!raw) return;
      event.preventDefault();
      var payload;
      try { payload = JSON.parse(raw); } catch (_) { return; }
      var point = context.options.geometry.dropNode(
        canvas, event.clientX, event.clientY, 20);
      if (typeof context.options.addNode === 'function') {
        context.options.addNode(payload, point.x, point.y);
      }
    });
    canvas.addEventListener('pointerdown', function (event) {
      if (!context.primary(event)) return;
      if (event.target === canvas || event.target.id === 'orchNodes'
          || event.target.id === 'orchEdges') {
        if (typeof context.options.deselect === 'function') {
          context.options.deselect();
        }
      }
    });
    return true;
  }

  return Object.freeze({ wire: wire });
}

