/* ===== migrated source: orchestration-studio-keyboard.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-studio-keyboard.js — document-level Studio key policy

   Routes focus trapping, document commands, transient dismissal and graph
   deletion through injected ports. It owns no modal or sheet lifecycle.
   ═══════════════════════════════════════════════════════════════════ */
function createOrchestrationStudioKeyboardController(options) {
  options = options || {};

  function call(name) {
    var args = Array.prototype.slice.call(arguments, 1);
    return typeof options[name] === 'function'
      ? options[name].apply(null, args) : undefined;
  }

  function keyDown(event) {
    var element = call('modal');
    if (!element || element.style.display === 'none') return;
    var dialog = element.querySelector('[role="dialog"]');
    if (event.key === 'Tab' && dialog) {
      call('trapTab', event, call('activePanel') || dialog);
      return;
    }
    var target = event.target || {};
    var tag = String(target.tagName || '').toLowerCase();
    var editing = tag === 'input' || tag === 'textarea' || tag === 'select'
      || target.isContentEditable;
    var modified = !!(event.ctrlKey || event.metaKey) && !event.altKey;
    var key = String(event.key || '').toLowerCase();

    // Save remains available while a field is focused and suppresses the
    // browser's save-page dialog.
    if (modified && key === 's') {
      event.preventDefault();
      call('save');
      return;
    }
    if (modified && !editing && !call('commandsBlocked') && key === 'z') {
      event.preventDefault();
      call(event.shiftKey ? 'redo' : 'undo');
      return;
    }
    if (modified && !editing && !call('commandsBlocked')
        && !event.shiftKey && key === 'y') {
      event.preventDefault();
      call('redo');
      return;
    }
    if (modified && !editing && !call('commandsBlocked')) {
      if (key === '+' || key === '=' || key === 'add') {
        event.preventDefault(); call('zoomIn'); return;
      }
      if (key === '-' || key === '_' || key === 'subtract') {
        event.preventDefault(); call('zoomOut'); return;
      }
      if (key === '0') {
        event.preventDefault(); call('zoomReset'); return;
      }
    }
    if (event.key === 'Escape') {
      for (var index = 0; index < 4; index += 1) {
        var action = [
          'cancelGesture', 'closePopups', 'dismissTransient',
          'dismissMobileSheet',
        ][index];
        if (call(action)) {
          event.preventDefault();
          return;
        }
      }
      return;
    }
    if (event.key !== 'Delete' && event.key !== 'Backspace') return;
    if (editing || call('commandsBlocked')) return;
    var edgeId = call('selectedEdgeId');
    var nodeId = call('selectedNodeId');
    if (edgeId) {
      event.preventDefault(); call('deleteEdge', edgeId);
    } else if (nodeId) {
      event.preventDefault(); call('deleteNode', nodeId);
    }
  }

  return Object.freeze({ keyDown: keyDown });
}

