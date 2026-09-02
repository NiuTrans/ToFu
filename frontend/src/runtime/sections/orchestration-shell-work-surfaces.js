/* ===== migrated source: orchestration-shell-work-surfaces.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-shell-work-surfaces.js — Studio work-surface markup

   Provides one frozen markup port for the Composer and Run surfaces. Their
   controllers retain visibility, rendering and request ownership; this module
   keeps the shared shell hierarchy and accessibility contract consistent.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationStudioWorkSurfaceMarkup(options) {
  options = options || {};
  var tx = options.tx || function (key) { return key; };
  var translate = options.translate || function (key) { return key; };
  var richCopy = options.richCopy || function (value) { return String(value); };
  var icons = options.icons || {};

  function composer() {
    return ''
      + '<aside class="orch-ai orch-work-surface" id="orchAi" aria-labelledby="orchAiTitle" aria-hidden="true" inert>'
      +   '<div class="orch-work-surface-head">'
      +     '<h2 class="orch-work-surface-title" id="orchAiTitle">' + icons.wand + ' ' + tx('orch.toolbar.aiComposer') + '</h2>'
      +     '<div class="orch-work-surface-head-actions">'
      +       '<button type="button" class="orch-icon-btn" data-orch-shell-action="aiClear" title="' + tx('orch.ai.clear') + '" aria-label="' + tx('orch.ai.clear') + '">' + icons.refresh + '</button>'
      +       '<button type="button" class="orch-icon-btn" data-orch-shell-action="toggleAi" title="' + tx('orch.tip.close') + '" aria-label="' + tx('orch.tip.close') + '">' + icons.reject + '</button>'
      +     '</div>'
      +   '</div>'
      +   '<div class="orch-work-surface-log orch-work-surface-log-composer" id="orchAiLog" role="log" aria-live="polite" aria-relevant="additions" aria-busy="false"></div>'
      +   '<div class="orch-work-surface-input orch-work-surface-input-composer">'
      +     '<textarea id="orchAiText" rows="3" placeholder="' + tx('orch.ai.placeholder') + '" aria-label="' + tx('orch.ai.placeholder') + '" data-orch-shell-key="ai"></textarea>'
      +     '<button type="button" class="orch-btn orch-btn-primary orch-ai-send" id="orchAiSend" data-orch-shell-action="aiSend">' + tx('orch.ai.send') + '</button>'
      +   '</div>'
      + '</aside>';
  }

  function runDrawer() {
    return ''
      + '<div class="orch-run-drawer orch-work-surface" id="orchRunDrawer" role="region" aria-labelledby="orchRunTitle" aria-hidden="true" aria-busy="false" inert>'
      +   '<div class="orch-work-surface-head">'
      +     '<h2 class="orch-work-surface-title" id="orchRunTitle">' + icons.rocket + ' ' + tx('orch.run.title') + '</h2>'
      +     '<div class="orch-work-surface-head-actions">'
      +       '<span class="orch-run-state" id="orchRunState" role="status" aria-live="polite" aria-atomic="true" hidden><span class="orch-run-state-dot" aria-hidden="true"></span><span id="orchRunStateLabel"></span></span>'
      +       '<button type="button" class="orch-icon-btn" data-orch-shell-action="closeRun" title="' + tx('orch.tip.close') + '" aria-label="' + tx('orch.tip.close') + '">' + icons.reject + '</button>'
      +     '</div>'
      +   '</div>'
      +   '<div class="orch-work-surface-input orch-work-surface-input-run">'
      +     '<textarea id="orchRunInput" rows="3" placeholder="' + tx('orch.run.inputPlaceholder') + '" aria-label="' + tx('orch.run.inputPlaceholder') + '" aria-describedby="orchRunHint"></textarea>'
      +     '<div class="orch-run-hint" id="orchRunHint">' + icons.eye + ' ' + richCopy(translate('orch.run.hint')) + '</div>'
      +     '<div class="orch-run-actions" role="group" aria-label="' + tx('orch.run.drawerActions') + '">'
      +       '<button type="button" class="orch-btn orch-btn-ghost" id="orchRunPlanBtn" data-orch-shell-action="plan">' + icons.eye + ' ' + tx('orch.run.previewPlan') + '</button>'
      +       '<button type="button" class="orch-btn orch-btn-run" id="orchRunBtn" data-orch-shell-action="run" title="' + tx('orch.run.testRun') + '">' + icons.auto + ' ' + tx('orch.run.testRun') + '</button>'
      +       '<button type="button" class="orch-btn orch-btn-primary" id="orchRunTaskBtn" data-orch-shell-action="runAsTask" title="' + tx('orch.run.asTask') + '">' + icons.rocket + ' ' + tx('orch.run.asTask') + '</button>'
      +       '<button type="button" class="orch-btn orch-btn-danger" id="orchRunAbort" data-orch-shell-action="abortRun" style="display:none">' + icons.stop + ' ' + tx('orch.run.stop') + '</button>'
      +     '</div>'
      +   '</div>'
      +   '<div class="orch-work-surface-log orch-work-surface-log-run" id="orchRunLog" role="log" aria-live="polite" aria-relevant="additions" aria-busy="false"></div>'
      + '</div>';
  }

  return Object.freeze({ composer: composer, runDrawer: runDrawer });
}

