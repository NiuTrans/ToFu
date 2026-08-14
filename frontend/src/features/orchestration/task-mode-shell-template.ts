import { orchestrationRegistry } from './registry';
export interface TaskModeShellMarkupOptions {
  translate?: (key: string) => unknown;
  escape?: (value: unknown) => unknown;
  icon?: (name: string) => unknown;
}

type TaskModeShellTemplateWindow = Window & {
  taskModeShellMarkup?: typeof taskModeShellMarkup;
};

/** Escaped, localized Task Mode modal contents. */
export function taskModeShellMarkup(
  options: TaskModeShellMarkupOptions = {},
): string {
  const translate = options.translate ?? ((key: string) => key);
  const escape = options.escape
    ?? ((value: unknown) => String(value == null ? '' : value));
  const icon = options.icon ?? (() => '');
  const tr = (key: string): string => String(escape(translate(key)));
  const ico = (name: string): string => String(icon(name));

  return ''
    + '<div class="tm-shell" role="dialog" aria-modal="true" tabindex="-1" aria-label="'
    + tr('tm.top.name') + '"><div class="tm-top">'
    + '<div class="tm-top-left"><span class="tm-top-glyph">' + ico('rocket')
    + '</span><span class="tm-top-name">' + tr('tm.top.name')
    + '</span><span class="tm-top-sub">' + tr('tm.top.sub')
    + '</span></div><div class="tm-top-actions" role="toolbar" aria-label="'
    + tr('tm.toolbar.actions') + '">'
    + '<span class="tm-top-state" data-tm-top-state role="status" '
    + 'aria-live="polite" aria-atomic="true" hidden><span '
    + 'class="tm-top-state-dot" aria-hidden="true"></span><span '
    + 'data-tm-top-state-label></span></span>'
    + '<button type="button" class="tm-btn" data-tm-action="open-studio" title="'
    + tr('tm.btn.studio') + '" aria-label="' + tr('tm.btn.studio') + '">'
    + ico('layout') + ' <span>' + tr('tm.btn.studio') + '</span></button>'
    + '<button type="button" class="tm-btn" data-tm-action="refresh-runs" title="'
    + tr('tm.btn.refresh') + '" aria-label="' + tr('tm.btn.refresh') + '">'
    + ico('loop') + ' <span>' + tr('tm.btn.refresh') + '</span></button>'
    + '<button type="button" class="tm-btn tm-btn-close" data-tm-action="close" title="'
    + tr('tm.tip.close') + '" aria-label="' + tr('tm.tip.close') + '">'
    + ico('reject') + '</button></div></div><nav class="tm-mobile-tabs" role="tablist" aria-label="'
    + tr('tm.panelNav') + '">'
    + '<button type="button" class="tm-mobile-tab is-active" id="tmTabRuns" '
    + 'role="tab" data-tm-panel="runs" aria-controls="tmRunRail" '
    + 'aria-selected="true" tabindex="0">' + ico('loop') + '<span>'
    + tr('tm.rail.runs') + '</span></button>'
    + '<button type="button" class="tm-mobile-tab" id="tmTabRun" role="tab" '
    + 'data-tm-panel="run" aria-controls="tmRunWorkspace" '
    + 'aria-selected="false" tabindex="-1">' + ico('layout') + '<span>'
    + tr('tm.panel.run') + '</span></button>'
    + '<button type="button" class="tm-mobile-tab" id="tmTabInspector" role="tab" '
    + 'data-tm-panel="inspector" aria-controls="tmInspector" '
    + 'aria-selected="false" tabindex="-1">' + ico('eye') + '<span>'
    + tr('tm.inspector') + '</span></button>'
    + '</nav><div class="tm-body" data-tm-active-panel="runs">'
    + '<div class="tm-rail" id="tmRunRail" role="tabpanel" '
    + 'aria-labelledby="tmTabRuns" data-tm-panel-view="runs"><div class="tm-rail-head">'
    + '<span class="tm-rail-title">' + tr('tm.rail.runs')
    + '</span><div class="tm-run-filters" id="tmRunFilters" role="group" aria-label="'
    + tr('tm.filter.label') + '">'
    + '<button type="button" data-tm-run-filter="all" aria-pressed="true">'
    + tr('tm.filter.all') + '<span data-tm-filter-count="all">0</span></button>'
    + '<button type="button" data-tm-run-filter="active" aria-pressed="false">'
    + tr('tm.filter.active') + '<span data-tm-filter-count="active">0</span></button>'
    + '<button type="button" data-tm-run-filter="finished" aria-pressed="false">'
    + tr('tm.filter.finished') + '<span data-tm-filter-count="finished">0</span></button>'
    + '</div></div><div class="tm-rail-list" id="tmRunList" '
    + 'aria-live="polite" aria-busy="false"></div></div>'
    + '<div class="tm-main" id="tmRunWorkspace" role="tabpanel" '
    + 'aria-labelledby="tmTabRun" data-tm-panel-view="run"><div class="tm-main-head" '
    + 'id="tmRunTitle" aria-busy="false"><div class="tm-empty" role="status" '
    + 'aria-live="polite" aria-atomic="true">' + ico('rocket') + ' '
    + tr('tm.select') + '</div></div>'
    + '<section class="tm-graph-section" id="tmGraphSection" style="display:none" '
    + 'aria-labelledby="tmGraphTitle"><div class="tm-graph-head"><span id="tmGraphTitle">'
    + tr('tm.stream.graph') + '</span><button type="button" class="tm-graph-locate" '
    + 'data-tm-graph-action="reveal-active" aria-controls="tmGraph" title="'
    + tr('tm.graph.locate') + '" aria-label="' + tr('tm.graph.locate') + '">'
    + ico('eye') + '<span>' + tr('tm.graph.locate') + '</span></button></div>'
    + '<div class="tm-graph" id="tmGraph" role="group" '
    + 'aria-labelledby="tmGraphTitle"></div></section><div class="tm-stream">'
    + '<div class="tm-stream-head" id="tmTimelineTitle">'
    + tr('tm.stream.timeline') + '</div><div class="tm-timeline" id="tmTimeline" '
    + 'role="log" aria-labelledby="tmTimelineTitle" aria-live="off" '
    + 'aria-relevant="additions" aria-busy="false"></div></div>'
    + '<div class="tm-final" id="tmFinal" style="display:none" role="region" aria-label="'
    + tr('tm.final.result') + '" aria-live="polite" aria-hidden="true" '
    + 'tabindex="0"></div></div><div class="tm-inspector" id="tmInspector" '
    + 'role="tabpanel" aria-labelledby="tmTabInspector" '
    + 'data-tm-panel-view="inspector"><div class="tm-insp-head">'
    + tr('tm.inspector') + '</div><div class="tm-insp-body" id="tmInspBody">'
    + '<div class="tm-insp-empty">' + ico('eye') + '<div>'
    + tr('tm.insp.empty') + '</div></div></div></div></div></div>';
}

(orchestrationRegistry as unknown as TaskModeShellTemplateWindow).taskModeShellMarkup =
  taskModeShellMarkup;
