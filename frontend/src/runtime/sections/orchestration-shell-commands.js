/* ===== migrated source: orchestration-shell-commands.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-shell-commands.js — Studio toolbar command adapter

   The Shell view speaks one stable command vocabulary. This adapter maps it
   to focused Studio controllers and validates those ports up front, keeping
   toolbar wiring out of the composition root and preventing silent dead
   buttons when a controller is refactored.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationStudioShellCommands(options) {
  options = options || {};
  var requirements = {
    studio: [
      'toggleMobilePalette', 'toggleMobileInspector',
      'dismissMobileSheet', 'close',
    ],
    document: ['showIssues'],
    workspace: [
      'toggleTemplateMenu', 'chooseBuiltin', 'openLoadMenu', 'tidy', 'save',
      'saveAndUse',
    ],
    history: ['undoAndApply', 'redoAndApply'],
    viewport: ['fit', 'zoomOut', 'reset', 'zoomIn'],
    panels: [
      'toggle', 'togglePalette', 'toggleInspector',
      'toggleComposer', 'openRun',
    ],
    composer: ['clear', 'handleKey', 'send'],
    run: ['close', 'plan', 'run', 'runAsTask', 'abort'],
    exporter: ['exportCurrent'],
  };

  Object.keys(requirements).forEach(function (portName) {
    var port = options[portName];
    requirements[portName].forEach(function (methodName) {
      if (!port || typeof port[methodName] !== 'function') {
        throw new TypeError(
          'invalid Studio shell command port: '
          + portName + '.' + methodName
        );
      }
    });
  });
  if (typeof options.rename !== 'function') {
    throw new TypeError('invalid Studio shell command port: rename');
  }

  function invoke(portName, methodName) {
    return function () {
      var port = options[portName];
      return port[methodName].apply(
        port, Array.prototype.slice.call(arguments));
    };
  }

  return Object.freeze({
    rename: options.rename,
    showDocIssues: invoke('document', 'showIssues'),
    toggleMobilePalette: invoke('studio', 'toggleMobilePalette'),
    toggleMobileInspector: invoke('studio', 'toggleMobileInspector'),
    dismissMobileSheet: invoke('studio', 'dismissMobileSheet'),
    toggleTemplateMenu: invoke('workspace', 'toggleTemplateMenu'),
    chooseBuiltin: invoke('workspace', 'chooseBuiltin'),
    openLoadMenu: invoke('workspace', 'openLoadMenu'),
    undo: invoke('history', 'undoAndApply'),
    redo: invoke('history', 'redoAndApply'),
    fitView: invoke('viewport', 'fit'),
    zoomOut: invoke('viewport', 'zoomOut'),
    zoomReset: invoke('viewport', 'reset'),
    zoomIn: invoke('viewport', 'zoomIn'),
    tidy: invoke('workspace', 'tidy'),
    togglePaletteRail: invoke('panels', 'togglePalette'),
    toggleCanvasFocus: invoke('panels', 'toggle'),
    toggleInspectorRail: invoke('panels', 'toggleInspector'),
    toggleAi: invoke('panels', 'toggleComposer'),
    openRun: invoke('panels', 'openRun'),
    exportDefinition: invoke('exporter', 'exportCurrent'),
    save: invoke('workspace', 'save'),
    saveAndUse: invoke('workspace', 'saveAndUse'),
    close: invoke('studio', 'close'),
    aiClear: invoke('composer', 'clear'),
    aiKey: invoke('composer', 'handleKey'),
    aiSend: invoke('composer', 'send'),
    closeRun: invoke('run', 'close'),
    plan: invoke('run', 'plan'),
    run: invoke('run', 'run'),
    runAsTask: invoke('run', 'runAsTask'),
    abortRun: invoke('run', 'abort'),
  });
}
