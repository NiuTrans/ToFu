/* ===== migrated source: orchestration-panel-layout.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-panel-layout.js — Studio work-surface presentation state

   Owns desktop rails, canvas focus and transient work-surface exclusivity.
   Graph state stays untouched; callbacks resync the available canvas width.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationPanelLayoutController(options) {
  options = options || {};
  var doc = options.document || document;
  var win = options.window || window;
  var paletteExpanded = true;
  var inspectorExpanded = true;
  var lastExpanded = { palette: true, inspector: true };
  var media = typeof win.orchestrationSheetMedia === 'function'
    ? win.orchestrationSheetMedia(win) : null;
  var workSurfaces = options.workSurfaces || (
    typeof createOrchestrationWorkSurfaceController === 'function'
      ? createOrchestrationWorkSurfaceController({surfaces: {
        composer: options.composer, run: options.run,
      }}) : null);
  function desktop() { return !media || !media.matches; }
  function focused() { return !paletteExpanded && !inspectorExpanded; }
  function runOpen() {
    return !!(workSurfaces && typeof workSurfaces.isOpen === 'function'
      && workSurfaces.isOpen('run'));
  }
  function _label(key) {
    return typeof options.translate === 'function'
      ? options.translate(key) : key;
  }

  function _syncRailButton(button, expanded, hideKey, showKey) {
    if (!button) return;
    var label = _label(expanded ? hideKey : showKey);
    button.setAttribute('aria-label', label);
    button.title = label;
  }

  function sync() {
    var shell = doc.querySelector('.orch-shell');
    var button = doc.getElementById('orchFocusCanvasBtn');
    var paletteButton = doc.getElementById('orchPaletteRailBtn');
    var inspectorButton = doc.getElementById('orchInspectorRailBtn');
    var runButton = doc.getElementById('orchOpenRunBtn');
    var palette = doc.getElementById('orchPalette');
    var inspector = doc.getElementById('orchInspector');
    var isDesktop = desktop();
    var runDrawerOpen = runOpen();
    var active = focused() && isDesktop;
    var showPalette = !isDesktop || paletteExpanded;
    var showInspector = !isDesktop || (inspectorExpanded && !runDrawerOpen);
    if (shell) shell.classList.toggle('orch-focus-canvas', active);
    if (shell) {
      shell.classList.toggle(
        'orch-palette-collapsed', isDesktop && !showPalette);
      shell.classList.toggle(
        'orch-inspector-collapsed', isDesktop && !showInspector);
    }
    // Mobile sheet visibility belongs to the Studio controller. Writing it
    // here would let a focus-mode sync reopen a closed mobile sheet.
    if (isDesktop) {
      setOrchestrationPanelState(palette, showPalette, {
        document: doc,
        focusTarget: active ? button : paletteButton,
        trigger: paletteButton,
      });
      setOrchestrationPanelState(inspector, showInspector, {
        document: doc,
        focusTarget: runDrawerOpen ? runButton
          : (active ? button : inspectorButton),
        trigger: inspectorButton,
      });
    }
    _syncRailButton(
      paletteButton, showPalette,
      'orch.toolbar.hideNodes', 'orch.toolbar.showNodes');
    _syncRailButton(
      inspectorButton, showInspector,
      'orch.toolbar.hideInspector', 'orch.toolbar.showInspector');
    if (inspectorButton) {
      if (isDesktop && runDrawerOpen
          && doc.activeElement === inspectorButton) {
        var runFocusTarget = runButton || button;
        if (runFocusTarget && typeof runFocusTarget.focus === 'function')
          runFocusTarget.focus();
      }
      inspectorButton.disabled = isDesktop && runDrawerOpen;
      inspectorButton.setAttribute(
        'aria-disabled', inspectorButton.disabled ? 'true' : 'false');
    }
    if (button) {
      var key = active
        ? 'orch.toolbar.showPanels' : 'orch.toolbar.focusCanvas';
      var label = _label(key);
      button.setAttribute('aria-pressed', active ? 'true' : 'false');
      button.setAttribute('aria-label', label);
      button.title = label;
    }
    if (typeof options.onChange === 'function') options.onChange(active);
    return active;
  }

  function toggle() {
    if (focused()) {
      paletteExpanded = !!lastExpanded.palette;
      inspectorExpanded = !!lastExpanded.inspector;
      if (!paletteExpanded && !inspectorExpanded) {
        paletteExpanded = true;
        inspectorExpanded = true;
      }
    } else {
      lastExpanded = {
        palette: paletteExpanded,
        inspector: inspectorExpanded,
      };
      paletteExpanded = false;
      inspectorExpanded = false;
    }
    return sync();
  }

  function _toggleRail(name) {
    var wasFocused = focused();
    if (name === 'palette') paletteExpanded = !paletteExpanded;
    else inspectorExpanded = !inspectorExpanded;
    if (!focused()) {
      lastExpanded = {
        palette: paletteExpanded,
        inspector: inspectorExpanded,
      };
    } else if (!wasFocused) {
      // The last visible rail was closed manually. Preserve that previous
      // one-rail layout so the canvas-focus control has a useful restore.
      lastExpanded = name === 'palette'
        ? { palette: true, inspector: false }
        : { palette: false, inspector: true };
    }
    sync();
    return name === 'palette' ? paletteExpanded : inspectorExpanded;
  }

  function togglePalette() { return _toggleRail('palette'); }
  function toggleInspector() { return _toggleRail('inspector'); }
  function showInspector() {
    if (runOpen() && !workSurfaces.close('run')) return false;
    if (!inspectorExpanded) {
      inspectorExpanded = true; lastExpanded.inspector = true;
    }
    sync();
    return true;
  }
  function setRunDrawerOpen() {
    sync(); return runOpen();
  }
  function toggleComposer() {
    return workSurfaces ? workSurfaces.toggle('composer') : false;
  }
  function openRun() {
    return workSurfaces ? workSurfaces.open('run') : false;
  }

  function dismissTransient() {
    if (workSurfaces && workSurfaces.dismiss()) return true;
    if (focused() && desktop()) {
      toggle();
      return true;
    }
    return false;
  }

  if (media && typeof media.addEventListener === 'function') {
    media.addEventListener('change', sync);
  }
  return {
    focused: focused,
    paletteExpanded: function () { return paletteExpanded; },
    inspectorExpanded: function () { return inspectorExpanded; },
    sync: sync,
    toggle: toggle,
    togglePalette: togglePalette,
    toggleInspector: toggleInspector,
    showInspector: showInspector,
    setRunDrawerOpen: setRunDrawerOpen,
    toggleComposer: toggleComposer,
    openRun: openRun,
    dismissTransient: dismissTransient,
  };
}

