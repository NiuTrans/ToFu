import { orchestrationRegistry } from './registry';
import { createTaskModeGraphView } from './task-mode-graph';
import { createTaskModeTimelineView } from './task-mode-timeline';
import { createTaskModeInspectorView } from './task-mode-inspector';
import {
  createTaskModeRunView,
  type TaskModeRunViewOptions,
} from './task-mode-run-view';
import type { OrchestrationNode } from './node-summary';
import { reportOrchestrationDiagnostic } from './diagnostic-report';

type ViewPort = Record<string, unknown>;

export interface TaskModeViewRegistryOptions extends Record<string, unknown> {
  runView?: ViewPort | null;
  graphView?: ViewPort | null;
  timelineView?: ViewPort | null;
  inspectorView?: ViewPort | null;
  document?: Document;
  contractSnapshot?: () => unknown;
  report?: (context: string, error: unknown) => unknown;
}

type TaskModeViewRegistryWindow = Window & {
  createTaskModeViewRegistry?: typeof createTaskModeViewRegistry;
};

const record = (value: unknown): Record<string, unknown> => value
  && typeof value === 'object' && !Array.isArray(value)
  ? value as Record<string, unknown> : {};

export function createTaskModeViewRegistry(
  options: TaskModeViewRegistryOptions = {},
) {
  let runView = options.runView ?? null;
  let graphView = options.graphView ?? null;
  let timelineView = options.timelineView ?? null;
  let inspectorView = options.inspectorView ?? null;
  const doc = (): Document => options.document ?? document;
  const contracts = (): Record<string, unknown> => record(
    typeof options.contractSnapshot === 'function'
      ? options.contractSnapshot() : {});
  const report = (context: string, error: unknown): void => {
    reportOrchestrationDiagnostic(options.report, context, error);
  };
  const run = (): ViewPort => {
    if (runView) return runView;
    runView = createTaskModeRunView({
      document: doc(),
      titleId: options.titleId || 'tmRunTitle',
      finalId: options.finalId || 'tmFinal',
      translate: options.translate,
      escape: options.escape,
      icon: options.icon,
      statusChip: options.statusChip,
      isTerminal: options.isTerminal,
      resultError: options.resultError,
      projectFinal: options.projectFinal,
      report,
      onEdit: options.onEdit,
      onDelete: options.onDelete,
      onAbort: options.onAbort,
      onRerun: options.onRerun,
      onRetry: options.onRetry,
    } as unknown as TaskModeRunViewOptions);
    return runView as unknown as ViewPort;
  };
  const graph = (): ViewPort => {
    if (graphView) return graphView;
    graphView = createTaskModeGraphView({
      document: doc(),
      hostId: String(options.graphHostId || 'tmGraph'),
      translate: options.translate as ((key: string) => unknown) | undefined,
      escape: options.escape as ((value: unknown) => unknown) | undefined,
      nodeAccent: options.nodeAccent as
        ((node: OrchestrationNode) => unknown) | undefined,
      nodeIconHtml: options.nodeIconHtml as
        ((node: OrchestrationNode) => unknown) | undefined,
      nodeLabel: options.nodeLabel as
        ((node: OrchestrationNode) => unknown) | undefined,
      nodeSubtitle: options.nodeSubtitle as
        ((node: OrchestrationNode) => unknown) | undefined,
      bindImageFallbacks: options.bindImageFallbacks as
        ((root: Element) => unknown) | undefined,
      onSelect: options.onSelect as ((nodeId: unknown) => unknown) | undefined,
    });
    return graphView as ViewPort;
  };
  const timeline = (): ViewPort => {
    if (timelineView) return timelineView;
    timelineView = createTaskModeTimelineView({
      document: doc(),
      hostId: String(options.timelineHostId || 'tmTimeline'),
      escape: options.escape as ((value: unknown) => unknown) | undefined,
      translate: options.translate as ((key: string) => unknown) | undefined,
      icon: options.icon as ((name: string) => unknown) | undefined,
      eventContract: () => contracts().eventContract,
      outcomeContract: () => contracts().outcomeContract,
      formatEvent: options.formatEvent as never,
    });
    return timelineView as ViewPort;
  };
  const inspector = (): ViewPort => {
    if (inspectorView) return inspectorView;
    inspectorView = createTaskModeInspectorView({
      document: doc(),
      hostId: String(options.inspectorHostId || 'tmInspBody'),
      translate: options.translate as ((key: string) => unknown) | undefined,
      escape: options.escape as ((value: unknown) => unknown) | undefined,
      icon: options.icon as ((name: string) => unknown) | undefined,
      nodeLabel: options.nodeLabel as never,
      nodeIconHtml: options.nodeIconHtml as never,
      nodeSubtitle: options.nodeSubtitle as never,
      bindImageFallbacks: options.bindImageFallbacks as never,
      onApprove: options.onApprove as never,
      onInput: options.onInput as never,
      report,
      limitPolicy: options.limitPolicy,
      traceContract: () => contracts().traceContract,
    });
    return inspectorView as ViewPort;
  };
  const refreshRequestLimits = (): unknown => {
    const refresh = inspectorView?.refreshRequestLimits;
    return typeof refresh === 'function'
      ? refresh.call(inspectorView) : false;
  };
  return Object.freeze({ run, graph, timeline, inspector, refreshRequestLimits });
}

(orchestrationRegistry as unknown as TaskModeViewRegistryWindow).createTaskModeViewRegistry =
  createTaskModeViewRegistry;
