/* ===== migrated source: orchestration-shell.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-shell.js — Orchestration Studio shell view

   Builds the stable modal/panel DOM. Focused toolbar and work-surface markup
   owners must load first. This view owns no graph state and no transport:
   orchestration.js mounts it, wires the canvas and supplies an explicit
   command interface. All DOM handlers stay local to this view.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationStudioShell(options) {
  options = options || {};
  var doc = options.document || document;
  var icons = options.icons || {};
  var translate = options.translate || function (key) { return key; };
  var escape = options.escape || function (value) { return String(value || ''); };
  var richCopy = typeof options.richCopy === 'function'
    ? options.richCopy : function (value) { return escape(value); };
  var workSurfaceMarkup = createOrchestrationStudioWorkSurfaceMarkup({
    tx: tx, translate: translate, richCopy: richCopy, icons: icons,
  });
  var limitPolicy = orchestrationRequestLimitPolicy(
    options.limitPolicy || options.requestLimits);
  function tx(key, params) {
    return escape(translate(key, params));
  }

  var ov = doc.createElement('div');
  ov.className = 'orch-overlay';
  ov.id = 'orchModal';
  ov.style.display = 'none';
  if (typeof options.onBackdrop === 'function') {
    ov.addEventListener('click', options.onBackdrop);
  }

  ov.innerHTML = ''
    + '<div class="orch-shell" role="dialog" aria-modal="true" tabindex="-1" aria-label="' + tx('orch.shell.title') + '">'
    +   '<header class="orch-top">'
    +     '<div class="orch-top-left">'
    +       '<span class="orch-logo"><img src="' + escape(options.logoUrl || '') + '" alt="" width="22" height="22"></span>'
    +       '<input id="orchNameInput" class="orch-name-input" spellcheck="false" '
    +              'aria-label="' + tx('orch.shell.flowName') + '" data-orch-shell-input="rename" />'
    +       '<div class="orch-doc-state-wrap">'
    +         '<button type="button" id="orchDocState" class="orch-doc-state is-draft" data-orch-shell-action="showDocIssues" aria-live="polite" aria-haspopup="dialog" aria-controls="orchIssuePanel" aria-expanded="false"></button>'
    +         '<div class="orch-issues-panel" id="orchIssuePanel" role="dialog" aria-label="' + tx('orch.issues.title') + '" hidden></div>'
    +       '</div>'
    +     '</div>'
    +     orchestrationStudioToolbarHtml({ tx: tx, icons: icons })
    +   '</header>'
    +   '<div class="orch-body">'
    +     workSurfaceMarkup.composer()
    +     '<aside class="orch-palette" id="orchPalette" aria-label="' + tx('orch.toolbar.nodes') + '"></aside>'
    +     '<div class="orch-panel-resizer orch-panel-resizer-palette" id="orchPaletteResize" role="separator" tabindex="0" aria-orientation="vertical" aria-controls="orchPalette" aria-label="' + tx('orch.panel.resizeNodes') + '"></div>'
    +     '<main class="orch-canvas-wrap">'
    +       '<nav class="orch-crumb" id="orchCrumb" aria-label="' + tx('orch.crumb.label') + '" hidden></nav>'
    +       '<div class="orch-canvas" id="orchCanvas" role="group" tabindex="-1" aria-label="' + tx('orch.canvas.title') + '">'
    +         '<div class="orch-viewport-extent" id="orchViewportExtent">'
    +           '<div class="orch-viewport-scene" id="orchViewportScene">'
    +             '<svg class="orch-edges" id="orchEdges"></svg>'
    +             '<div class="orch-nodes" id="orchNodes"></div>'
    +           '</div>'
    +         '</div>'
    +         '<div class="orch-hint" id="orchHint"></div>'
    +       '</div>'
    +       '<div class="orch-viewport-tools" role="group" aria-label="' + tx('orch.viewport.controls') + '">'
    +           '<button type="button" class="orch-viewport-btn" data-orch-shell-action="fitView" title="' + tx('orch.viewport.fit') + '" aria-label="' + tx('orch.viewport.fit') + '">' + icons.fit + '</button>'
    +           '<button type="button" class="orch-viewport-btn" id="orchZoomOutBtn" data-orch-shell-action="zoomOut" title="' + tx('orch.viewport.zoomOut') + '" aria-label="' + tx('orch.viewport.zoomOut') + '">' + icons.minus + '</button>'
    +           '<button type="button" class="orch-viewport-level" id="orchZoomResetBtn" data-orch-shell-action="zoomReset" title="' + tx('orch.viewport.reset') + '" aria-label="' + tx('orch.viewport.reset') + '">100%</button>'
    +           '<button type="button" class="orch-viewport-btn" id="orchZoomInBtn" data-orch-shell-action="zoomIn" title="' + tx('orch.viewport.zoomIn') + '" aria-label="' + tx('orch.viewport.zoomIn') + '">' + icons.plus + '</button>'
    +       '</div>'
    +     '</main>'
    +     '<button type="button" class="orch-sheet-scrim" id="orchSheetScrim" data-orch-shell-action="dismissMobileSheet" aria-label="' + tx('orch.tip.close') + '" aria-hidden="true" inert></button>'
    +     '<div class="orch-panel-resizer orch-panel-resizer-inspector" id="orchInspectorResize" role="separator" tabindex="0" aria-orientation="vertical" aria-controls="orchInspector" aria-label="' + tx('orch.panel.resizeInspector') + '"></div>'
    +     '<aside class="orch-inspector" id="orchInspector" aria-label="' + tx('orch.toolbar.edit') + '"></aside>'
    +     workSurfaceMarkup.runDrawer()
    +   '</div>'
    + '</div>';

  limitPolicy.applyStudio(ov);

  var toolbar = ov.querySelector('.orch-top-actions');
  var view = doc.defaultView || null;
  var sheetMedia = view && typeof view.orchestrationSheetMedia === 'function'
    ? view.orchestrationSheetMedia(view) : null;
  var toolbarKeyboard = toolbar
    && typeof createOrchestrationRovingItemsController === 'function'
    ? createOrchestrationRovingItemsController({
      root: toolbar, selector: 'button[data-orch-shell-action]', wrap: true,
      available: function (item) {
        var mobile = !!(sheetMedia && sheetMedia.matches);
        if (item.closest('.orch-action-group-mobile')) return mobile;
        if (item.closest('.orch-rail-controls')) return !mobile;
        return true;
      },
    }) : null;
  if (toolbarKeyboard && sheetMedia
      && typeof sheetMedia.addEventListener === 'function') {
    sheetMedia.addEventListener('change', function () { toolbarKeyboard.sync(); });
  }
  if (toolbarKeyboard && view && typeof view.MutationObserver === 'function') {
    new view.MutationObserver(function () { toolbarKeyboard.sync(); }).observe(
      toolbar, { subtree: true, attributes: true,
        attributeFilter: ['disabled', 'hidden', 'aria-disabled'] });
  }

  bindOrchestrationStudioShellCommands(
    ov, options.commands, options.popupMenus);

  return ov;
}

