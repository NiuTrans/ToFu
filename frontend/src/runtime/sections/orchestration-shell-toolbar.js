/* ===== migrated source: orchestration-shell-toolbar.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-shell-toolbar.js — Studio action hierarchy markup

   Groups definition, history, canvas and work-surface actions into named
   regions. The shell owns event binding; this module owns only stable,
   localized toolbar structure.
   ═══════════════════════════════════════════════════════════════════ */

function orchestrationStudioToolbarHtml(options) {
  options = options || {};
  var tx = options.tx || function (key) { return key; };
  var icons = options.icons || {};

  return ''
    + '<div class="orch-top-actions" role="toolbar" aria-orientation="horizontal" aria-label="' + tx('orch.toolbar.actions') + '">'
    +   '<div class="orch-top-actions-scroll">'
    +     '<div class="orch-action-group orch-action-group-mobile orch-m-only" role="group" aria-label="' + tx('orch.toolbar.mobilePanels') + '">'
    +       '<button type="button" class="orch-btn orch-btn-ghost orch-m-pal-btn" data-orch-shell-action="toggleMobilePalette" aria-controls="orchPalette" aria-expanded="false">' + icons.plus + ' ' + tx('orch.toolbar.nodes') + '</button>'
    +       '<button type="button" class="orch-btn orch-btn-ghost orch-m-insp-btn" data-orch-shell-action="toggleMobileInspector" aria-controls="orchInspector" aria-expanded="false">' + icons.gear + ' ' + tx('orch.toolbar.edit') + '</button>'
    +     '</div>'
    +     '<div class="orch-action-group orch-action-group-definition" role="group" aria-label="' + tx('orch.toolbar.definitionActions') + '">'
    +       '<div class="orch-tpl-wrap">'
    +         '<button type="button" class="orch-btn orch-btn-ghost" id="orchTplBtn" data-orch-shell-action="toggleTemplateMenu" aria-haspopup="menu" aria-controls="orchTplMenu" aria-expanded="false" title="' + tx('orch.toolbar.templates') + '" aria-label="' + tx('orch.toolbar.templates') + '">' + icons.wand + ' <span class="orch-btn-label-compact">' + tx('orch.toolbar.templates') + '</span> ' + icons.chevronDown + '</button>'
    +         '<div class="orch-tpl-menu" id="orchTplMenu" role="menu" aria-label="' + tx('orch.toolbar.templates') + '" style="display:none">'
    +           '<button type="button" role="menuitem" data-orch-shell-builtin="autopilot">' + icons.auto + ' ' + tx('orch.template.autopilot') + '</button>'
    +           '<button type="button" role="menuitem" data-orch-shell-builtin="fanout">' + icons.fanout + ' ' + tx('orch.template.fanout') + '</button>'
    +           '<button type="button" role="menuitem" data-orch-shell-builtin="adversarial">' + icons.shield + ' ' + tx('orch.template.adversarial') + '</button>'
    +           '<button type="button" role="menuitem" data-orch-shell-builtin="blank">' + icons.plus + ' ' + tx('orch.template.blank') + '</button>'
    +         '</div>'
    +       '</div>'
    +       '<div class="orch-tpl-wrap">'
    +         '<button type="button" class="orch-btn orch-btn-ghost" id="orchLoadBtn" data-orch-shell-action="openLoadMenu" aria-haspopup="menu" aria-controls="orchLoadMenu" aria-expanded="false" title="' + tx('orch.toolbar.open') + '" aria-label="' + tx('orch.toolbar.open') + '">' + icons.folder + ' <span class="orch-btn-label-compact">' + tx('orch.toolbar.open') + '</span> ' + icons.chevronDown + '</button>'
    +         '<div class="orch-load-menu" id="orchLoadMenu" role="menu" aria-label="' + tx('orch.toolbar.open') + '" aria-busy="false" style="display:none"></div>'
    +       '</div>'
    +       '<button type="button" class="orch-btn orch-btn-ghost" data-orch-shell-action="exportDefinition" title="' + tx('orch.toolbar.export') + '" aria-label="' + tx('orch.toolbar.export') + '">' + icons.download + ' <span class="orch-btn-label-compact">' + tx('orch.toolbar.export') + '</span></button>'
    +     '</div>'
    +     '<div class="orch-action-group orch-action-group-history" role="group" aria-label="' + tx('orch.toolbar.historyActions') + '">'
    +       '<button type="button" class="orch-btn orch-btn-ghost orch-history-btn" id="orchUndoBtn" disabled data-orch-shell-action="undo" aria-keyshortcuts="Control+Z Meta+Z" title="' + tx('orch.toolbar.undo') + '" aria-label="' + tx('orch.toolbar.undo') + '">' + icons.undo + '</button>'
    +       '<button type="button" class="orch-btn orch-btn-ghost orch-history-btn" id="orchRedoBtn" disabled data-orch-shell-action="redo" aria-keyshortcuts="Control+Shift+Z Meta+Shift+Z" title="' + tx('orch.toolbar.redo') + '" aria-label="' + tx('orch.toolbar.redo') + '">' + icons.redo + '</button>'
    +     '</div>'
    +     '<div class="orch-action-group orch-action-group-canvas" role="group" aria-label="' + tx('orch.toolbar.canvasActions') + '">'
    +       '<button type="button" class="orch-btn orch-btn-ghost" data-orch-shell-action="tidy" title="' + tx('orch.toolbar.tidyTip') + '" aria-label="' + tx('orch.toolbar.tidy') + '">' + icons.layout + ' <span class="orch-btn-label-compact">' + tx('orch.toolbar.tidy') + '</span></button>'
    +       '<div class="orch-rail-controls" role="group" aria-label="' + tx('orch.toolbar.panelLayout') + '">'
    +         '<button type="button" class="orch-btn orch-btn-ghost orch-rail-btn" id="orchPaletteRailBtn" data-orch-shell-action="togglePaletteRail" aria-controls="orchPalette" aria-expanded="true" title="' + tx('orch.toolbar.hideNodes') + '" aria-label="' + tx('orch.toolbar.hideNodes') + '">' + icons.plus + '</button>'
    +         '<button type="button" class="orch-btn orch-btn-ghost orch-focus-btn" id="orchFocusCanvasBtn" data-orch-shell-action="toggleCanvasFocus" aria-pressed="false" title="' + tx('orch.toolbar.focusCanvas') + '" aria-label="' + tx('orch.toolbar.focusCanvas') + '">' + icons.panels + '</button>'
    +         '<button type="button" class="orch-btn orch-btn-ghost orch-rail-btn" id="orchInspectorRailBtn" data-orch-shell-action="toggleInspectorRail" aria-controls="orchInspector" aria-expanded="true" title="' + tx('orch.toolbar.hideInspector') + '" aria-label="' + tx('orch.toolbar.hideInspector') + '">' + icons.gear + '</button>'
    +       '</div>'
    +     '</div>'
    +     '<div class="orch-action-group orch-action-group-work" role="group" aria-label="' + tx('orch.toolbar.workSurfaces') + '">'
    +       '<button type="button" class="orch-btn orch-btn-ghost" id="orchAiToggle" data-orch-shell-action="toggleAi" aria-controls="orchAi" aria-expanded="false" title="' + tx('orch.toolbar.aiComposer') + '" aria-label="' + tx('orch.toolbar.aiComposer') + '">' + icons.wand + ' <span class="orch-btn-label-compact">' + tx('orch.toolbar.aiComposer') + '</span></button>'
    +     '</div>'
    +   '</div>'
    +   '<div class="orch-top-actions-primary">'
    +     '<button type="button" class="orch-btn orch-btn-run" id="orchOpenRunBtn" data-orch-shell-action="openRun" aria-controls="orchRunDrawer" aria-expanded="false" title="' + tx('orch.toolbar.run') + '" aria-label="' + tx('orch.toolbar.run') + '">' + icons.rocket + ' <span class="orch-btn-label-narrow">' + tx('orch.toolbar.run') + '</span></button>'
    +     '<button type="button" class="orch-btn orch-btn-ghost" id="orchSaveBtn" data-orch-shell-action="save" aria-keyshortcuts="Control+S Meta+S" title="' + tx('orch.toolbar.save') + '" aria-label="' + tx('orch.toolbar.save') + '">' + icons.save + ' <span class="orch-btn-label-narrow">' + tx('orch.toolbar.save') + '</span></button>'
    +     '<button type="button" class="orch-btn orch-btn-primary" id="orchSaveUseBtn" data-orch-shell-action="saveAndUse" title="' + tx('orch.toolbar.saveUse') + '" aria-label="' + tx('orch.toolbar.saveUse') + '">' + icons.loop + ' <span class="orch-btn-label-narrow">' + tx('orch.toolbar.saveUse') + '</span></button>'
    +     '<span class="orch-top-sep" aria-hidden="true"></span>'
    +     '<button type="button" class="orch-btn orch-btn-close" data-orch-shell-action="close" title="' + tx('orch.tip.close') + '" aria-label="' + tx('orch.tip.close') + '">' + icons.reject + '</button>'
    +   '</div>'
    + '</div>';
}
