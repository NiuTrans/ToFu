/* ===== migrated source: orchestration-mobile-sheets.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-mobile-sheets.js — exclusive mobile sheet state

   Owns the one active mobile sheet and projects it atomically into shell
   classes, panel/trigger accessibility, graph isolation, scrim visibility
   and focus restoration. Global surface admission and desktop rail visibility
   remain Studio and PanelLayout policy respectively.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationMobileSheetController(options) {
  options = options || {};
  var doc = options.document || document;
  var activeName = null;
  var focusOwner = 'palette';
  var projection = options.projection
    || createOrchestrationMobileSurfaceProjection({document: doc});

  function _shell() { return doc.querySelector('.orch-shell'); }
  function _mobile() {
    return typeof options.isMobile === 'function' && !!options.isMobile();
  }
  function _trigger(spec) {
    return doc.querySelector(
      '[data-orch-shell-action="' + spec.action + '"]');
  }
  function _activeWorkSurface() {
    return typeof options.activeWorkSurface === 'function'
      ? options.activeWorkSurface() : null;
  }

  function snapshot() {
    var mobile = _mobile();
    var active = mobile ? activeName : null;
    var workSurface = mobile ? _activeWorkSurface() : null;
    return {
      mobile: mobile,
      active: active,
      sheetOpen: !!active,
      workSurface: workSurface,
      workSurfaceOpen: !!workSurface,
      backgroundBlocked: !!active || !!workSurface,
    };
  }

  function _projectSurface(shell, name, state) {
    var spec = projection.sheet(name);
    var open = state.active === name;
    if (shell) shell.classList.toggle(spec.className, open);
    var trigger = _trigger(spec);
    // Desktop rail accessibility belongs to PanelLayout. Mobile triggers are
    // still reset so a hidden phone control never advertises a stale sheet.
    if (!state.mobile) {
      if (trigger) trigger.setAttribute('aria-expanded', 'false');
      return;
    }
    setOrchestrationPanelState(doc.getElementById(spec.panelId), open, {
      document: doc,
      trigger: trigger,
    });
  }

  function sync() {
    var shell = _shell();
    if (!_mobile()) activeName = null;
    var state = snapshot();
    // Restore the toolbar before a closing sheet tries to return focus to its
    // trigger. Opening surfaces are projected first, then isolate the toolbar.
    if (!state.backgroundBlocked) projection.sync(state);
    _projectSurface(shell, 'palette', state);
    _projectSurface(shell, 'inspector', state);

    var focusTarget = _trigger(projection.sheet(state.active || focusOwner));
    setOrchestrationPanelState(
      doc.querySelector('.orch-canvas-wrap'), !state.backgroundBlocked,
      {document: doc, focusTarget: state.sheetOpen ? focusTarget : null}
    );
    setOrchestrationPanelState(
      doc.getElementById('orchSheetScrim'), state.sheetOpen,
      {document: doc, openClass: 'is-open', focusTarget: focusTarget}
    );
    if (state.backgroundBlocked) projection.sync(state);
    if (!state.mobile && typeof options.syncDesktopPanels === 'function') {
      options.syncDesktopPanels();
    }
    if (typeof options.onChange === 'function') options.onChange(state);
    return state;
  }

  function setOpen(name, open) {
    var spec = projection.sheet(name);
    if (!spec || !_shell()) return false;
    var opening = !!open && activeName !== name;
    if (opening && _mobile() && typeof options.admitOpen === 'function'
        && options.admitOpen(name) === false) return false;
    if (open) {
      activeName = name;
      focusOwner = name;
    } else if (activeName === name) {
      activeName = null;
      focusOwner = name;
    }
    sync();
    if (opening && activeName === name) {
      focusOrchestrationPanel(
        doc.getElementById(spec.panelId), spec.initialFocus);
    }
    return activeName === name;
  }

  function toggle(name) {
    if (!projection.sheet(name) || !_shell()) return false;
    return setOpen(name, activeName !== name);
  }

  function isOpen(name) { return _mobile() && activeName === name; }

  function close(name) {
    if (!projection.sheet(name) || !_shell()) return false;
    if (activeName !== name) return true;
    setOpen(name, false);
    return activeName !== name;
  }

  function dismiss() {
    if (!_mobile() || !activeName) return false;
    return close(activeName);
  }

  return {
    active: function () { return snapshot().active; },
    activePanel: function () { return projection.activePanel(snapshot()); },
    isOpen: isOpen,
    close: close,
    dismiss: dismiss,
    setOpen: setOpen,
    snapshot: snapshot,
    sync: sync,
    toggle: toggle,
  };
}

