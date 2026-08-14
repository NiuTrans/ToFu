import { orchestrationRegistry } from './registry';
import {
  createOrchestrationEventState,
  reduceOrchestrationEvent,
  resetOrchestrationEventState,
  type OrchestrationEventState,
} from './events';
import { projectOrchestrationEventPresentation } from './event-presentation';
import { orchestrationNodeTraceSnapshot } from './trace-state';
import type { ContractSource } from './contracts';

type EventRecord = Record<string, unknown>;
export interface TaskModeEventControllerOptions {
  state?: OrchestrationEventState;
  eventContract?: ContractSource;
  traceContract?: ContractSource;
  onGraph?: (value: unknown, detail?: unknown) => unknown;
  onInspector?: (value: unknown, detail?: unknown) => unknown;
  onTimeline?: (value: unknown, detail?: unknown) => unknown;
  onGateOpened?: (value: unknown, detail?: unknown) => unknown;
  onGateClosed?: (value: unknown, detail?: unknown) => unknown;
  onLifecycle?: (value: unknown, detail?: unknown) => unknown;
}
type TaskModeEventControllerWindow = Window & {
  createTaskModeEventController?: typeof createTaskModeEventController;
};

export function createTaskModeEventController(
  options: TaskModeEventControllerOptions = {},
) {
  const state = options.state ?? createOrchestrationEventState();
  let selectedNode: string | null = null;
  let lastTraceNode: string | null = null;
  const snapshot = () => {
    const inspectId = selectedNode || state.activeNode || lastTraceNode;
    return Object.freeze({
      activeNode: state.activeNode,
      doneNodes: state.doneNodes,
      gates: state.gates,
      trace: state.trace,
      nodeTrace: orchestrationNodeTraceSnapshot(state, inspectId),
      inspectedNode: inspectId,
      selectedNode,
      completion: state.completion,
      result: state.result,
      lastError: state.lastError,
    });
  };
  const call = (
    name: keyof TaskModeEventControllerOptions,
    value: unknown,
    detail?: unknown,
  ): unknown => {
    const fn = options[name];
    return typeof fn === 'function'
      ? (fn as (value: unknown, detail?: unknown) => unknown)(value, detail)
      : undefined;
  };
  const reduce = (event: EventRecord) => reduceOrchestrationEvent(
    state, event, options.eventContract, options.traceContract);
  const presentation = (
    change: ReturnType<typeof reduceOrchestrationEvent>,
    event: EventRecord,
  ) => projectOrchestrationEventPresentation(
    change, event, options.eventContract, { selectedNode },
  );
  const rememberTraceNode = (
    effect: ReturnType<typeof projectOrchestrationEventPresentation>,
  ): void => {
    if (effect.traceNode) lastTraceNode = effect.traceNode;
  };
  const hasGate = (): boolean => Object.keys(state.gates).length > 0;
  const projectGateTransition = (
    hadGate: boolean, current: unknown, detail: unknown,
  ): boolean => {
    const gatePresent = hasGate();
    if (hadGate === gatePresent) return false;
    call(gatePresent ? 'onGateOpened' : 'onGateClosed', current, detail);
    return true;
  };
  const ingest = (eventValue: unknown) => {
    const event = eventValue && typeof eventValue === 'object'
      ? eventValue as EventRecord : {};
    const hadGate = hasGate();
    const change = reduce(event);
    const effect = presentation(change, event);
    rememberTraceNode(effect);
    const current = snapshot();
    if (effect.graph) call('onGraph', current, event);
    if (effect.inspector) call('onInspector', current, event);
    if (effect.timeline) call('onTimeline', event, change);
    projectGateTransition(hadGate, current, event);
    return change;
  };
  const replay = (pageValue: unknown) => {
    const page = pageValue && typeof pageValue === 'object'
      ? pageValue as EventRecord : {};
    const hadGate = hasGate();
    let graphChanged = false;
    let graphDetail: unknown = page;
    let inspectorWasChanged = false;
    let inspectorDetail: unknown = page;
    (Array.isArray(page.events) ? page.events : []).forEach((entry) => {
      const event = entry && typeof entry === 'object'
        ? entry as EventRecord : {};
      const change = reduce(event);
      const effect = presentation(change, event);
      rememberTraceNode(effect);
      if (effect.graph) { graphChanged = true; graphDetail = event; }
      if (effect.inspector) {
        inspectorWasChanged = true;
        inspectorDetail = event;
      }
      if (effect.timeline) call('onTimeline', event, change);
    });
    if (page.done === true && state.activeNode !== null) {
      state.activeNode = null;
      inspectorWasChanged = true;
      inspectorDetail = page;
    }
    if (page.done === true && Object.keys(state.gates).length > 0) {
      state.gates = {};
      inspectorWasChanged = true;
      inspectorDetail = page;
    }
    const current = snapshot();
    if (page.done === true) graphChanged = true;
    if (graphChanged) call('onGraph', current, graphDetail);
    if (inspectorWasChanged) call('onInspector', current, inspectorDetail);
    projectGateTransition(hadGate, current, page);
    call('onLifecycle', {
      status: page.status || '', done: page.done === true,
    }, page);
    return current;
  };
  const reset = () => {
    const hadGate = hasGate();
    resetOrchestrationEventState(state);
    selectedNode = null;
    lastTraceNode = null;
    const current = snapshot();
    projectGateTransition(hadGate, current, { reason: 'reset' });
    return current;
  };
  const selectNode = (nodeIdValue: unknown): string | null => {
    const nodeId = nodeIdValue == null ? '' : String(nodeIdValue);
    selectedNode = selectedNode === nodeId ? null : nodeId || null;
    const current = snapshot();
    call('onGraph', current, null);
    call('onInspector', current, null);
    return selectedNode;
  };
  const dismissGate = (requestId: unknown): boolean => {
    const key = String(requestId || '');
    if (!key || !state.gates[key]) return false;
    const hadGate = hasGate();
    delete state.gates[key];
    const current = snapshot();
    call('onInspector', current, null);
    projectGateTransition(hadGate, current, requestId);
    return true;
  };
  return Object.freeze({ snapshot, ingest, replay, reset, selectNode, dismissGate });
}

(orchestrationRegistry as unknown as TaskModeEventControllerWindow).createTaskModeEventController =
  createTaskModeEventController;
