/* ===== migrated source: orchestration-studio.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-studio.js — Studio shell lifecycle + global UI policy

   Owns lazy modal mounting, open/close guards and exclusive-surface handoff.
   Document-level keyboard policy lives in orchestration-studio-keyboard.js;
   graph/document operations arrive as callbacks.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationStudioController(options) {
  options = options || {};
  var doc = options.document || document;
  var win = options.window || window;
  var sheetMedia = typeof win.orchestrationSheetMedia === 'function'
    ? win.orchestrationSheetMedia(win) : null;
  var ready = false;
  var focusManager = createOrchestrationDialogFocusManager({
    document: doc,
    window: win,
  });
  var mobileSheets = options.mobileSheets
    || createOrchestrationMobileSheetController({
      document: doc,
      isMobile: isMobile,
      activeWorkSurface: activeWorkSurface,
      admitOpen: releaseWorkSurface,
      syncDesktopPanels: options.syncDesktopPanels,
    });

  function modal() { return doc.getElementById('orchModal'); }
  function shell() { return doc.querySelector('.orch-shell'); }
  function isReady() { return ready; }

  function syncMobileSheets() {
    return mobileSheets.sync();
  }

  function dismissMobileSheet() {
    return mobileSheets.dismiss();
  }

  function activeWorkSurface() {
    return options.workSurfaces
      && typeof options.workSurfaces.active === 'function'
      ? options.workSurfaces.active() : null;
  }

  function releaseWorkSurface() {
    if (!activeWorkSurface()) return true;
    if (!options.workSurfaces
        || typeof options.workSurfaces.dismiss !== 'function') return false;
    options.workSurfaces.dismiss();
    return !activeWorkSurface();
  }

  function releaseMobileSheet() {
    if (!mobileSheets.active()) return true;
    mobileSheets.dismiss();
    return !mobileSheets.active();
  }

  function resetTransient() {
    if (!releaseWorkSurface() || !releaseMobileSheet()) return false;
    if (typeof options.cancelGesture === 'function') {
      options.cancelGesture();
    }
    if (typeof options.closePopups === 'function') options.closePopups();
    return true;
  }

  function ensure() {
    if (ready) return modal();
    if (typeof options.createShell !== 'function') return null;
    var element = options.createShell(
      typeof options.shellOptions === 'function' ? options.shellOptions() : {}
    );
    if (!element) return null;
    doc.body.appendChild(element);
    ready = true;
    if (typeof options.onMount === 'function') options.onMount(element);
    doc.addEventListener('keydown', keyDown);
    if (sheetMedia && typeof sheetMedia.addEventListener === 'function') {
      sheetMedia.addEventListener('change', syncMobileSheets);
    }
    if (typeof options.installUnloadGuard === 'function') {
      options.installUnloadGuard(win);
    }
    return element;
  }

  function open(openOptions) {
    openOptions = openOptions || {};
    var element = ensure();
    var becameVisible = !!(element && element.style.display === 'none');
    if (element) {
      focusManager.open(element);
    }
    if (becameVisible) syncMobileSheets();
    if (!openOptions.skipInitial
        && (typeof options.hasNodes !== 'function' || !options.hasNodes())) {
      if (typeof options.loadInitial === 'function') options.loadInitial();
    }
    if (typeof options.render === 'function') options.render();
    if (typeof options.refreshContract === 'function') options.refreshContract();
  }

  async function close(event, force) {
    var element = modal();
    if (!element) return false;
    if (event && event.target !== element) return false;
    if (!force && typeof options.confirmDiscard === 'function'
        && !await options.confirmDiscard()) return false;
    if (!resetTransient()) return false;
    focusManager.close(element);
    return true;
  }

  function isMobile() {
    return !!(sheetMedia && sheetMedia.matches);
  }

  function toggleMobilePalette() {
    return mobileSheets.toggle('palette');
  }

  function closeMobilePalette() {
    return mobileSheets.close('palette');
  }

  function toggleMobileInspector() {
    return mobileSheets.toggle('inspector');
  }

  function setMobileInspectorOpen(open) {
    return mobileSheets.setOpen('inspector', !!open);
  }

  function canvasCommandsBlocked() {
    return !!(activeWorkSurface() || mobileSheets.active());
  }

  function closeMobileInspector() {
    return mobileSheets.close('inspector');
  }

  var keyboard = createOrchestrationStudioKeyboardController({
    modal: modal,
    trapTab: function (event, panel) {
      return focusManager.trapTab(event, panel);
    },
    activePanel: mobileSheets.activePanel,
    commandsBlocked: canvasCommandsBlocked,
    dismissMobileSheet: dismissMobileSheet,
    save: options.save,
    undo: options.undo,
    redo: options.redo,
    zoomIn: options.zoomIn,
    zoomOut: options.zoomOut,
    zoomReset: options.zoomReset,
    cancelGesture: options.cancelGesture,
    closePopups: options.closePopups,
    dismissTransient: options.dismissTransient,
    selectedEdgeId: options.selectedEdgeId,
    selectedNodeId: options.selectedNodeId,
    deleteEdge: options.deleteEdge,
    deleteNode: options.deleteNode,
  });

  function keyDown(event) { return keyboard.keyDown(event); }

  return {
    isReady: isReady,
    ensure: ensure,
    open: open,
    close: close,
    keyDown: keyDown,
    isMobile: isMobile,
    toggleMobilePalette: toggleMobilePalette,
    closeMobilePalette: closeMobilePalette,
    toggleMobileInspector: toggleMobileInspector,
    closeMobileInspector: closeMobileInspector,
    setMobileInspectorOpen: setMobileInspectorOpen,
    syncMobileSheets: syncMobileSheets,
    dismissMobileSheet: dismissMobileSheet,
    releaseMobileSheet: releaseMobileSheet,
  };
}

