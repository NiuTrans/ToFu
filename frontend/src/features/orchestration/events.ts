import { orchestrationRegistry } from './registry';
import {
  orchestrationEventGateEffect,
  orchestrationEventShouldReduce,
} from './event-policy';
import {
  createOrchestrationTraceState,
  reduceOrchestrationTraceEvent,
  type OrchestrationTraceEvent,
  type OrchestrationTraceState,
} from './trace-state';
import type { ContractSource } from './contracts';

export type OrchestrationEvent = OrchestrationTraceEvent;

export interface OrchestrationEventState extends OrchestrationTraceState {
  activeNode: unknown | null;
  doneNodes: Record<string, unknown>;
  gates: Record<string, OrchestrationEvent>;
  completion: OrchestrationEvent | null;
  result: unknown | null;
  lastError: unknown | null;
}

export interface OrchestrationEventChange {
  type: string;
  graph: boolean;
  trace: boolean;
  gates: boolean;
  terminal: boolean;
  nodeId: unknown | null;
  nodeStatus: unknown | null;
}

type EventStateWindow = Window & {
  createOrchestrationEventState?: typeof createOrchestrationEventState;
  resetOrchestrationEventState?: typeof resetOrchestrationEventState;
  reduceOrchestrationEvent?: typeof reduceOrchestrationEvent;
};

function eventKey(value: unknown): string {
  return String(value == null ? '' : value);
}

export function createOrchestrationEventState(): OrchestrationEventState {
  const traceState = createOrchestrationTraceState();
  return {
    activeNode: null,
    doneNodes: {},
    gates: {},
    ...traceState,
    completion: null,
    result: null,
    lastError: null,
  };
}

export function resetOrchestrationEventState(
  state: OrchestrationEventState,
): OrchestrationEventState {
  const fresh = createOrchestrationEventState();
  for (const key of Object.keys(fresh) as (keyof OrchestrationEventState)[]) {
    // All fields are deliberately replaceable: reset invalidates every prior
    // collection reference, matching the classic state ownership contract.
    (state as unknown as Record<string, unknown>)[key] = fresh[key];
  }
  return state;
}

export function reduceOrchestrationEvent(
  stateValue?: OrchestrationEventState | null,
  eventValue?: OrchestrationEvent | null,
  eventContract?: ContractSource,
  traceContract?: ContractSource,
): OrchestrationEventChange {
  const state = stateValue || createOrchestrationEventState();
  const event = eventValue || {};
  const type = typeof event.type === 'string' ? event.type : '';
  const nodeId = event.node_id;
  const nodeKey = eventKey(nodeId);
  const change: OrchestrationEventChange = {
    type,
    graph: false,
    trace: false,
    gates: false,
    terminal: false,
    nodeId: nodeId || null,
    nodeStatus: null,
  };
  if (!orchestrationEventShouldReduce(eventContract, event.type)) {
    return change;
  }
  const gateEffect = orchestrationEventGateEffect(eventContract, event.type);
  if (gateEffect === 'open') {
    if (nodeId) {
      state.activeNode = nodeId;
      change.graph = true;
    }
    const requestId = event.request_id;
    if (requestId) {
      state.gates[eventKey(requestId)] = event;
      change.gates = true;
    }
    return change;
  }
  if (gateEffect === 'close') {
    const requestId = event.request_id;
    const requestKey = eventKey(requestId);
    if (requestId && state.gates[requestKey]) {
      delete state.gates[requestKey];
      change.gates = true;
    }
    return change;
  }

  const traceEffect = reduceOrchestrationTraceEvent(
    state, event, eventContract, traceContract,
  );
  if (traceEffect.handled) {
    change.trace = traceEffect.trace;
    change.graph = traceEffect.graph;
    change.nodeStatus = traceEffect.nodeStatus;
    if (traceEffect.hasError) state.lastError = traceEffect.error;
    if (traceEffect.activate && nodeId) state.activeNode = nodeId;
    if (traceEffect.finish && nodeId) {
      state.doneNodes[nodeKey] = traceEffect.nodeStatus;
      if (state.activeNode === nodeId) state.activeNode = null;
    }
    return change;
  }

  const clearPendingGates = (): void => {
    if (Object.keys(state.gates || {}).length === 0) return;
    state.gates = {};
    change.gates = true;
  };

  switch (type) {
    case 'flow_start':
      state.completion = null;
      state.result = null;
      state.lastError = null;
      break;

    case 'loop_start':
    case 'loop_iteration':
    case 'no_progress':
      if (nodeId) {
        state.activeNode = nodeId;
        change.graph = true;
      }
      break;

    case 'flow_complete':
      state.completion = { ...event };
      state.activeNode = null;
      clearPendingGates();
      change.graph = true;
      change.terminal = true;
      break;

    case 'done':
      state.result = event.result || null;
      state.activeNode = null;
      clearPendingGates();
      change.graph = true;
      change.terminal = true;
      break;
  }

  return change;
}

export {
  orchestrationNodeTraceSnapshot,
  recordOrchestrationTraceAttempt,
} from './trace-state';
export type {
  OrchestrationNodeTraceSnapshot,
  OrchestrationTrace,
} from './trace-state';

const bridge = orchestrationRegistry as unknown as EventStateWindow;
bridge.createOrchestrationEventState = createOrchestrationEventState;
bridge.resetOrchestrationEventState = resetOrchestrationEventState;
bridge.reduceOrchestrationEvent = reduceOrchestrationEvent;
