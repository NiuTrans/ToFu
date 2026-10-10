/* ===== migrated source: orchestration-mobile-surface-projection.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-mobile-surface-projection.js — mobile modal semantics

   Projects the shared accessibility boundary for Studio sheets and
   fullscreen work surfaces. Feature controllers still own open/close state;
   this module alone owns mobile roles, background isolation and focus entry.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationMobileSurfaceProjection(options) {
  options = options || {};
  var doc = options.document || document;
  var surfaces = Object.freeze({
    palette: Object.freeze({
      panelId: 'orchPalette', className: 'orch-m-pal',
      action: 'toggleMobilePalette', initialFocus: '[data-palette-close]',
      desktopRole: null,
    }),
    inspector: Object.freeze({
      panelId: 'orchInspector', className: 'orch-m-insp',
      action: 'toggleMobileInspector', initialFocus: '.orch-inspector-close',
      desktopRole: null,
    }),
    composer: Object.freeze({
      panelId: 'orchAi', initialFocus: '#orchAiText', desktopRole: null,
    }),
    run: Object.freeze({
      panelId: 'orchRunDrawer', initialFocus: '#orchRunInput',
      desktopRole: 'region',
    }),
  });

  function sheet(name) {
    return name === 'palette' || name === 'inspector'
      ? surfaces[name] : null;
  }

  function activeName(state) {
    if (!state || !state.mobile) return null;
    var name = state.active || state.workSurface || null;
    return surfaces[name] ? name : null;
  }

  function activePanel(state) {
    var spec = surfaces[activeName(state)];
    return spec ? doc.getElementById(spec.panelId) : null;
  }

  function _projectRole(name, active) {
    var spec = surfaces[name];
    var panel = doc.getElementById(spec.panelId);
    if (!panel) return;
    if (active) {
      panel.setAttribute('role', 'dialog');
      panel.setAttribute('aria-modal', 'true');
    } else {
      if (spec.desktopRole) panel.setAttribute('role', spec.desktopRole);
      else panel.removeAttribute('role');
      panel.removeAttribute('aria-modal');
    }
  }

  function sync(state) {
    var name = activeName(state);
    Object.keys(surfaces).forEach(function (candidate) {
      _projectRole(candidate, candidate === name);
    });
    var header = doc.querySelector('.orch-shell > .orch-top');
    var panel = activePanel(state);
    if (panel && !panel.contains(doc.activeElement)) {
      focusOrchestrationPanel(panel, surfaces[name].initialFocus);
    }
    setOrchestrationPanelState(header, !panel, {
      document: doc, focusTarget: panel,
    });
    return panel;
  }

  return Object.freeze({
    activePanel: activePanel,
    sheet: sheet,
    sync: sync,
  });
}

