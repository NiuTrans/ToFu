import { orchestrationRegistry } from './registry';
import {
  createTaskModeTransitionProjector,
  type TaskModeTransition,
} from './task-mode-transition-projector';
import { orchestrationNodeTraceSnapshot } from './trace-state';
import { reportOrchestrationDiagnostic } from './diagnostic-report';
import {
  createTaskModeViewRegistry,
  type TaskModeViewRegistryOptions,
} from './task-mode-view-registry';

type Port = Record<string, unknown>;

export interface TaskModeWorkspaceOptions extends Record<string, unknown> {
  viewRegistry?: ReturnType<typeof createTaskModeViewRegistry>;
  runStore: Port;
}

type TaskModeWorkspaceWindow = Window & {
  createTaskModeWorkspace?: typeof createTaskModeWorkspace;
};

const record = (value: unknown): Port => value
  && typeof value === 'object' && !Array.isArray(value) ? value as Port : {};
const invoke = (
  port: Port,
  name: string,
  ...args: unknown[]
): unknown => {
  const fn = port[name];
  return typeof fn === 'function'
    ? (fn as (...values: unknown[]) => unknown).apply(port, args)
    : undefined;
};

export function createTaskModeWorkspace(options: TaskModeWorkspaceOptions) {
  let definition: unknown = null;
  const call = (name: string, ...args: unknown[]): unknown => {
    const fn = options[name];
    return typeof fn === 'function'
      ? (fn as (...values: unknown[]) => unknown).apply(null, args)
      : undefined;
  };
  const controller = (name: string): Port => {
    const value = options[name];
    return record(typeof value === 'function'
      ? (value as () => unknown)() : value);
  };
  const report = (context: string, error: unknown): void => {
    reportOrchestrationDiagnostic(options.report, context, error);
  };
  const views = options.viewRegistry ?? createTaskModeViewRegistry({
    ...options,
    report,
    onSelect: (nodeId: unknown) => selectNode(nodeId),
  } as TaskModeViewRegistryOptions);
  const runView = (): Port => record(views.run());
  const graphView = (): Port => record(views.graph());
  const timelineView = (): Port => record(views.timeline());
  const inspectorView = (): Port => record(views.inspector());
  const renderTitle = (
    run: unknown,
    emptyKey?: string,
    state?: Record<string, unknown>,
  ): unknown => invoke(runView(), 'renderTitle', run, emptyKey, state);
  const setTimelineBusy = (value: unknown): unknown =>
    invoke(timelineView(), 'setBusy', value);
  const eventSnapshot = (): Port => record(
    invoke(controller('eventController'), 'snapshot'));
  const renderGraph = (): unknown => {
    const events = eventSnapshot();
    return invoke(graphView(), 'render', {
      definition,
      activeNode: events.activeNode,
      doneNodes: events.doneNodes,
      selectedNode: events.selectedNode,
      trace: events.trace,
    });
  };
  const line = (html: string, className?: string): unknown =>
    invoke(timelineView(), 'append', html, className);
  const renderTimelineEvent = (event: unknown): unknown =>
    invoke(timelineView(), 'appendEvent', event);
  const renderInspector = (stepEvent?: unknown): unknown => {
    const events = eventSnapshot();
    const inspectId = events.inspectedNode
      || events.selectedNode || events.activeNode;
    return invoke(inspectorView(), 'render', {
      runId: invoke(controller('runController'), 'id'),
      definition,
      activeNode: events.activeNode,
      inspectedNode: inspectId,
      selectedNode: events.selectedNode,
      gates: events.gates,
      nodeTrace: events.nodeTrace
        || orchestrationNodeTraceSnapshot(events, inspectId),
      stepEvent: stepEvent || null,
    });
  };
  function selectNode(nodeId: unknown): unknown {
    const selected = invoke(controller('eventController'), 'selectNode', nodeId);
    call('selectPanel', 'inspector');
    return selected;
  }
  const syncLifecycle = (status: unknown, terminal?: unknown): boolean => {
    if (!status) return false;
    const isTerminal = typeof terminal === 'boolean'
      ? terminal : Boolean(call('isTerminal', status));
    const runController = controller('runController');
    const runStore = options.runStore;
    const runId = invoke(runController, 'id');
    let run = record(invoke(runStore, 'find', runId));
    let openedRun = record(invoke(runStore, 'selected'));
    let current = openedRun.id === runId ? openedRun : run;
    let projectedStatus = status;
    let projectedTerminal = isTerminal;
    if (current.id && (current.status !== status
        || current.terminal !== isTerminal)) {
      if (invoke(runStore, 'updateLifecycle', runId, status, isTerminal)) {
        call('renderRunList');
      }
      openedRun = record(invoke(runStore, 'selected'));
      run = record(invoke(runStore, 'find', runId));
      current = openedRun.id === runId ? openedRun : run;
      if (current.id) {
        projectedStatus = current.status || status;
        projectedTerminal = current.terminal === true;
      }
    }
    if (openedRun.id && openedRun.id === runId) {
      if (projectedTerminal) {
        renderTitle(openedRun);
        return true;
      }
    }
    invoke(controller('runListView'), 'syncChip',
      options.titleId || 'tmRunTitle', projectedStatus);
    return true;
  };
  const adoptSnapshot = (
    runValue: unknown,
    projectionValue: Record<string, unknown> = {},
  ): boolean => {
    const run = record(runValue);
    const projection = projectionValue ?? {};
    const activeRunId = invoke(controller('runController'), 'id');
    if (!run.id || activeRunId !== run.id) return false;
    if (!invoke(options.runStore, 'adopt', run, activeRunId)) return false;
    if (run.definition) definition = run.definition;
    const filterChanged = Boolean(
      invoke(controller('runListView'), 'reveal', run));
    if (projection.renderList !== false || filterChanged) call('renderRunList');
    renderTitle(run);
    if (projection.renderGraph) renderGraph();
    if (projection.renderFinal) invoke(runView(), 'renderFinal', run);
    return true;
  };
  const clear = (transitionValue: TaskModeTransition = {}): boolean => {
    const transition = transitionValue ?? {};
    setTimelineBusy(false);
    invoke(options.runStore, 'clearSelection');
    if ((transition.reason === 'deleted' || transition.reason === 'missing')
        && transition.targetRunId) {
      invoke(options.runStore, 'discard', transition.targetRunId);
    }
    definition = null;
    invoke(controller('eventController'), 'reset');
    invoke(timelineView(), 'clear');
    invoke(runView(), 'clearFinal');
    invoke(graphView(), 'clear');
    renderInspector();
    if (transition.reason === 'switch') {
      call('renderRunList');
      call('selectPanel', 'run');
      return true;
    }
    renderTitle(null, transition.reason === 'missing'
      ? 'tm.runNotFound' : 'tm.select');
    call('renderRunList');
    call('selectPanel', 'runs');
    return true;
  };
  const transitions = createTaskModeTransitionProjector({
    clear,
    renderTitle,
    setTimelineBusy,
    adoptSnapshot,
    setTimelineLive: (live) => invoke(timelineView(), 'setLive', live),
    replay: (page) => invoke(controller('eventController'), 'replay', page),
    line,
    report,
    icon: (name) => call('icon', name),
    translate: (key) => call('translate', key),
    escape: (value) => call('escape', value),
    failureMessage: (value, fallback) =>
      call('failureMessage', value, fallback),
  });
  const projectTransition = (transition: TaskModeTransition): unknown =>
    transitions.project(transition);
  const refreshRequestLimits = (): unknown => views.refreshRequestLimits();
  const refreshContract = (): boolean => {
    refreshRequestLimits();
    call('renderRunList');
    const run = record(invoke(options.runStore, 'selected'));
    if (!run.id) return false;
    renderTitle(run);
    renderGraph();
    renderInspector();
    return true;
  };
  return Object.freeze({
    definition: () => definition,
    runView: views.run,
    graphView: views.graph,
    timelineView: views.timeline,
    inspectorView: views.inspector,
    renderTitle,
    setTimelineBusy,
    syncLifecycle,
    adoptSnapshot,
    clear,
    projectTransition,
    renderGraph,
    line,
    renderTimelineEvent,
    renderInspector,
    selectNode,
    refreshRequestLimits,
    refreshContract,
  });
}

(orchestrationRegistry as unknown as TaskModeWorkspaceWindow).createTaskModeWorkspace =
  createTaskModeWorkspace;
